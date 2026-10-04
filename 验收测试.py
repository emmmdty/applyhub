# -*- coding: utf-8 -*-
"""中台逻辑验收（离线，SQLite 引擎 + 临时库）：
终态吸收/冻结、URL 抽取、分类规则（技术测评→笔试/AI面试→测评）、独立键多志愿消歧、
考试安排子表（add_exam 幂等/迁移幂等/退役字段不再写）、待确认队列闭环、看板聚合、
2026-09-30 修复回归（原子写/跨进程锁/备份一致性/链接排除/营销防御/demo隔离）。
用法: python3 验收测试.py   （自动建临时 SQLite，结束自动清理）"""
import os, sys, json, datetime, importlib.util, tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# ---- SQLite 引擎 + 临时库（测试互不污染） ----
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(prefix="sqlite_acc_"), "test.sqlite")

import grist_store as g
import mail

g.create_tables()

OK, FAIL = 0, []
def check(name, cond, extra=""):
    global OK
    if cond: OK += 1; print(f"  ✓ {name}")
    else: FAIL.append(name); print(f"  ✗ {name} {extra}")

CST = mail.CST
DT = {"widgetOptions": '{"timezone":"Asia/Shanghai"}'}
def C(i, l, t, e=None):
    f = {"label": l, "type": t}; f.update(e or {}); return {"id": i, "fields": f}

try:
    # 测试前置：队列/状态一律改用临时文件（修复：曾直接清空真实 待确认队列.json，致用户队列条目丢失）
    _tmp_q = os.path.join(tempfile.mkdtemp(prefix="zq_queue_"), "queue.json")
    _real_queue_path, _real_state_path = mail.QUEUE_PATH, mail.STATE_PATH
    mail.QUEUE_PATH, mail.STATE_PATH = _tmp_q, os.path.join(os.path.dirname(_tmp_q), "state.json")
    mail.save_queue({"items": []})

    g.create_tables(
        [C("company","公司名称","Text"), C("industry","行业","Text"), C("position","岗位名称","Text"),
         C("apply_url","网申链接","Text"), C("exam_link","考试链接","Text"),
         C("status","投递状态","Choice"), C("apply_date","投递日期","DateTime",DT),
         C("written_test","笔试时间","DateTime",DT), C("written_test_end","笔试结束","DateTime",DT),
         C("interview_time","面试时间","DateTime",DT), C("assessment_deadline","测评截止","DateTime",DT),
         C("status_note","状态备注","Text"), C("notes","注意事项","Text"), C("sessions","场次安排","Text"),
         C("source","招聘渠道","Choice"), C("referrer","内推人","Text"), C("reject_reason","终止原因","Choice"),
         C("done_assessment","测评完成","Bool"), C("done_written","笔试完成","Bool"), C("done_interview","面试完成","Bool")],
        [C("company_ref","公司","Ref:Deliveries"), C("received_at","收信时间","DateTime",DT),
         C("subject","主题","Text"), C("body","正文","Text")],
        [C("company_ref","公司","Ref:Deliveries"), C("exam_type","考试类型","Choice"),
         C("start_time","开始时间","DateTime",DT), C("end_time","结束时间","DateTime",DT),
         C("exam_link","考试链接","Text"), C("done","完成","Bool"), C("source_subject","来源主题","Text")])

    def M(subj, body, uid=1, name="测试"):
        return {"uid": uid, "date": datetime.datetime(2026, 9, 19, 10, 0, tzinfo=CST),
                "from": name, "subject": subj, "body": body}

    def exams_of(公司):
        r = g.get_by_company(公司)
        return g.exams_of(r["id"]) if r else []

    def snapshot():
        mains = mail.load_main()
        companies, excludes = mail.companies_excludes(mains)
        return mains, companies, excludes

    print("== T1 终止/已通过 吸收态 ==")
    check("终止+笔试邮件 不复活", mail.plan_status("笔试", "无信息", "终止") is None)
    check("终止+面试邮件 不复活", mail.plan_status("面试", "无信息", "终止") is None)
    check("终止+Offer 不自动翻转", mail.plan_status("Offer", "无信息", "终止") is None)
    check("已通过+Offer 保持", mail.plan_status("Offer", "无信息", "已通过") is None)
    check("测评中+面试邮件 推进", mail.plan_status("面试", "无信息", "测评中") == "面试中")
    check("简历评估+感谢信 终止", mail.plan_status("感谢信", "无信息", "简历评估") == "终止")
    check("简历评估+笔试 推进", mail.plan_status("笔试", "固定场次", "简历评估") == "笔试中")

    print("== T2 extract_url 修复 ==")
    body_bad_first = "详情见 https://careers.x.com/notice/help 页面，请从 https://exam.x.com/start?token=abc 进入考试"
    check("负分在前取正分", mail.extract_url(body_bad_first, []) == "https://exam.x.com/start?token=abc")
    check("0分兜底优先于负分", mail.extract_url("看 https://x.com/notice/help 与 https://x.com/jobs/123", []) == "https://x.com/jobs/123")
    check("全为负分返回空(不写错链接)", mail.extract_url("访问 https://x.com/notice/help", []) == "")

    print("== T3 分类规则（用户定版） ==")
    check("技术测评→笔试类型", mail.extract_type("【安克】2027校招技术测评邀请", "") == "笔试")
    check("AI面试→测评类型", mail.extract_type("【安克】AI面试邀约", "") == "测评")
    check("AI面试(测评类)不推进状态", mail.plan_status("测评", "固定场次", "简历评估") is None)
    check("笔试（测评）→测评类型(九方智投)", mail.extract_type("【九方智投校招】笔试（测评）通知", "") == "测评")
    check("宣讲会邮件→其他(讯飞)", mail.extract_type(
        "现场投递简历，当天筛选！科大讯飞2027届哈工大宣讲会倒计时2天！",
        "当场确定筛选结果，免笔试测评！！国庆前即可安排线上面试。时间：9月22日 19:00") == "其他")
    check("免笔试否定语境→不判笔试", mail.extract_type("恭喜您", "您已获得免笔试资格，直接进入后续流程") == "其他")
    check("plan_exam: 其他带窗口→其他事项行", mail.plan_exam(
        {"window": ("固定场次", datetime.datetime(2026, 9, 22, 19, 0, tzinfo=mail.CST),
                    datetime.datetime(2026, 9, 22, 21, 0, tzinfo=mail.CST), "", True),
         "deadline": (None, None, None, 2)}, "其他", M("", "", 1)["date"])
        == ("其他事项", datetime.datetime(2026, 9, 22, 19, 0), datetime.datetime(2026, 9, 22, 21, 0)))
    check("不误伤: 技术测评仍→笔试", mail.extract_type("【安克】2027校招技术测评邀请", "") == "笔试")
    check("不误伤: 纯笔试仍→笔试", mail.extract_type("【百度】在线笔试邀请", "") == "笔试")

    print("== T4 独立键：多志愿消歧 ==")
    g.add_record(g.T_MAIN, {"公司名称": "华为", "投递状态": "终止", "招聘渠道": "官网"})
    g.add_record(g.T_MAIN, {"公司名称": "百度", "投递状态": "简历评估"})
    rid_ab = g.add_record(g.T_MAIN, {"公司名称": "示例集团A", "岗位名称": "后端", "投递状态": "已通过"})["id"]
    rid_ab2 = g.add_record(g.T_MAIN, {"公司名称": "示例集团A", "岗位名称": "客户端", "投递状态": "面试中"})["id"]
    rid_tx = g.add_record(g.T_MAIN, {"公司名称": "腾讯", "岗位名称": "后端", "投递状态": "笔试中"})["id"]
    rid_ty = g.add_record(g.T_MAIN, {"公司名称": "腾讯", "岗位名称": "客户端", "投递状态": "面试中"})["id"]
    check("get_active_record 非终态优先", g.get_active_record("示例集团A")["id"] == rid_ab2)
    mains, companies, excludes = snapshot()
    n, rid, s, amb = mail.match_company("【示例集团A】面试邀请", "", companies)
    check("同公司多志愿→非终态志愿", (n, rid, amb) == ("示例集团A", rid_ab2, None))
    n, rid, s, amb = mail.match_company("【腾讯】面试邀请", "", companies)
    check("双非终态→歧义原因", amb == "同公司多记录请指认岗位" and rid is None)
    ok, reason = mail.process_one(M("【腾讯】面试邀请", "请参加面试", uid=90), mains, companies, excludes, False)
    check("歧义邮件不入库", not ok and reason == "同公司多记录请指认岗位")
    q = mail.load_queue()
    mail.enqueue(q, M("【腾讯】面试邀请", "请参加面试", uid=90), reason)  # 模拟 cmd_sync 入队
    mail.save_queue(q)
    check("歧义邮件进队列", any(x.get("原因") == "同公司多记录请指认岗位" for x in mail.load_queue()["items"]))
    check("find_record 按公司+岗位定位", g.find_record("腾讯", "后端")["id"] == rid_tx)

    print("== T5 管道：终态冻结 / 笔试入库Exams / 未匹配入队 ==")
    mains, companies, excludes = snapshot()
    ok, reason = mail.process_one(M("【华为】笔试邀请", "9月25日 14:00-16:00 在线笔试，请按时参加 https://exam.huawei.com/start?token=x", uid=91), mains, companies, excludes, False)
    f = g.get_by_company("华为")["fields"]
    check("华为终态处理成功", ok)
    check("华为状态仍终止", f.get("投递状态") == "终止")
    check("华为主表笔试时间未被污染(已退役)", f.get("笔试时间") is None)
    check("华为终态不建考试行", len(exams_of("华为")) == 0)
    check("华为邮件已存档", len(g.archives_of("华为")) == 1)

    笔试 = "各位同学：9月21日 19:00-21:00 进行在线笔试，链接 https://exam.baidu.com/start?eid=1 请提前登录"
    ok, reason = mail.process_one(M("【百度】在线笔试邀请", 笔试, uid=2), mains, companies, excludes, False)
    f = g.get_by_company("百度")["fields"]
    exs = exams_of("百度")
    check("百度状态推进笔试中", f.get("投递状态") == "笔试中")
    check("百度主表笔试时间不再写(退役)", f.get("笔试时间") is None and f.get("考试链接") is None)
    check("百度考试行已建(类型笔试)", len(exs) == 1 and exs[0]["fields"].get("考试类型") == "笔试")
    check("百度考试行 09-21 19:00", exs and exs[0]["fields"]["开始时间"].strftime("%m-%d %H:%M") == "09-21 19:00")
    check("百度考试行结束21:00", exs and exs[0]["fields"].get("结束时间").strftime("%H:%M") == "21:00")
    check("百度考试行带链接", exs and exs[0]["fields"].get("考试链接") == "https://exam.baidu.com/start?eid=1")
    check("百度邮件已存档", len(g.archives_of("百度")) == 1)

    # 2026-10-01 起主题带【公司名】会自动建档；此例改为平台群发样式（无公司名可提取）→ 仍进队列
    ok, reason = mail.process_one(
        {"uid": 3, "date": datetime.datetime(2026, 9, 19, 10, 0, tzinfo=CST), "from": "noreply@mokahr.com",
         "from_display": "Moka", "subject": "米哈游AI测评邀请", "body": "请于 9月23日内 完成测评"},
        mains, companies, excludes, False)
    check("米哈游未匹配失败", not ok and reason == "公司未匹配")
    q = mail.load_queue()
    _m3 = {"uid": 3, "date": M("", "", 3)["date"], "from": "noreply@mokahr.com", "from_display": "Moka",
           "subject": "米哈游AI测评邀请", "body": "请于 9月23日内 完成测评"}
    mail.enqueue(q, _m3, reason)  # 模拟 cmd_sync 入队
    mail.save_queue(q)
    check("队列收到米哈游 1 条", any(x.get("uid") == 3 for x in mail.load_queue()["items"]))

    print("== T6 队列重试闭环（人工补建公司 / 指认岗位后重试） ==")
    g.add_record(g.T_MAIN, {"公司名称": "米哈游", "投递状态": "简历评估"})  # 用户在 Grist 里补建公司
    g.update_record(g.T_MAIN, rid_ty, {"投递状态": "终止"})  # 用户指认：关停腾讯客户端志愿 → 歧义解除
    mains, companies, excludes = snapshot()
    still = []
    for it in mail.load_queue()["items"]:
        m = {"uid": it["uid"], "date": mail.qdate(it["收信时间"]), "from": it.get("发件人", ""),
             "subject": it["主题"], "body": it["正文"]}
        ok, reason = mail.process_one(m, mains, companies, excludes, False)
        if not ok: still.append(it)
    mail.save_queue({"items": still})
    f = g.get_by_company("米哈游")["fields"]
    exs = exams_of("米哈游")
    check("米哈游匹配成功", f is not None)
    check("测评考试行已建(截止日窗口: 收信起·09-23止)", len(exs) == 1
          and exs[0]["fields"]["开始时间"].strftime("%m-%d") == "09-19"
          and exs[0]["fields"].get("结束时间") and exs[0]["fields"]["结束时间"].strftime("%m-%d %H:%M") == "09-23 23:59")
    check("测评邮件不动状态(仍简历评估)", f.get("投递状态") == "简历评估")
    check("队列清空", len(mail.load_queue()["items"]) == 0)
    check("米哈游邮件已存档", len(g.archives_of("米哈游")) == 1)

    print("== T7 add_exam 幂等 / 多轮面试独立行 ==")
    n0 = len(g.list_records(g.T_EXAM))
    ok, reason = mail.process_one(M("【百度】在线笔试邀请", 笔试, uid=4), *(snapshot()), False)  # 同邮件重投
    exs = exams_of("百度")
    check("同邮件重投不重复建行", len(exs) == 1 and len(g.list_records(g.T_EXAM)) == n0)
    _, created = g.add_exam(g.get_by_company("百度")["id"], "笔试", datetime.datetime(2026, 9, 21, 19, 0))
    check("add_exam 同键幂等(不新建)", not created and len(g.list_records(g.T_EXAM)) == n0)
    _, created = g.add_exam(g.get_by_company("百度")["id"], "笔试", datetime.datetime(2026, 9, 28, 19, 0), 来源主题="顺延")
    check("add_exam 新开始→新行(顺延)", created and len(g.list_records(g.T_EXAM)) == n0 + 1)
    g.add_record(g.T_MAIN, {"公司名称": "理想汽车", "投递状态": "面试中"})
    mains, companies, excludes = snapshot()
    for i, (day, hr) in enumerate([(25, 10), (28, 15)]):
        mail.process_one(M(f"【理想汽车】{['一面','二面'][i]}邀约", f"9月{day}日 {hr}:00 面试", uid=10 + i), mains, companies, excludes, False)
    ies = exams_of("理想汽车")
    check("多轮面试=每场独立行", len(ies) == 2 and all(x["fields"]["考试类型"] == "面试" for x in ies))

    print("== T8 存量迁移（旧字段→Exams，跨天窗口保留起止，幂等） ==")
    g.add_record(g.T_MAIN, {"公司名称": "字节跳动", "投递状态": "笔试中",
                            "笔试时间": datetime.datetime(2026, 9, 20, 14, 0),
                            "笔试结束": datetime.datetime(2026, 9, 22, 16, 0),
                            "笔试完成": True, "面试时间": datetime.datetime(2026, 9, 26, 10, 0),
                            "测评截止": datetime.datetime(2026, 9, 24), "考试链接": "https://exam.bytedance.com"})
    add1, dup1 = g.migrate_exams_from_main()
    exs = exams_of("字节跳动")
    check("迁移建 3 行(笔试/面试/测评)", add1 == 3 and len(exs) == 3)
    pen = next(x for x in exs if x["fields"]["考试类型"] == "笔试")
    check("跨天有效期窗口保留起止(09-20 14:00→09-22 16:00)", pen["fields"]["开始时间"].strftime("%m-%d %H:%M") == "09-20 14:00"
          and pen["fields"]["结束时间"].strftime("%m-%d %H:%M") == "09-22 16:00")
    check("迁移带入完成勾选", pen["fields"].get("完成") is True)
    check("迁移带链接", pen["fields"].get("考试链接") == "https://exam.bytedance.com")
    add2, dup2 = g.migrate_exams_from_main()
    check("迁移幂等(重跑 0 新增)", add2 == 0 and dup2 >= 3)

    print("== T8c plan_exam 窗口/截止规则 ==")
    ext_win = {"window": ("时间窗", datetime.datetime(2026, 9, 25, 14, 0, tzinfo=CST),
                          datetime.datetime(2026, 9, 27, 16, 0, tzinfo=CST), "", False),
               "deadline": (None, None, None, 2)}
    check("笔试有效期窗口→保留起止", mail.plan_exam(ext_win, "笔试", M("", "", 1)["date"]) ==
          ("笔试", datetime.datetime(2026, 9, 25, 14, 0), datetime.datetime(2026, 9, 27, 16, 0)))
    ext_dl = {"window": ("截止日", datetime.datetime(2026, 9, 19, 10, 0, tzinfo=CST),
                         datetime.datetime(2026, 9, 20, 23, 59, tzinfo=CST), "", False),
              "deadline": (None, None, None, 2)}
    check("笔试截止日→有效期窗口(收信→截止)", mail.plan_exam(ext_dl, "笔试", M("", "", 1)["date"]) ==
          ("笔试", datetime.datetime(2026, 9, 19, 10, 0), datetime.datetime(2026, 9, 20, 23, 59)))

    print("== T8d 其他事项（选择/预约时间类） ==")
    check("事务识别: 选择笔试时间", bool(mail.SCHED_SEL_PAT.search("请于9月20日前选择笔试时间")))
    check("事务识别: 预约面试", bool(mail.SCHED_SEL_PAT.search("请尽快预约面试")))
    check("非事务: 普通笔试邀请", not mail.SCHED_SEL_PAT.search("9月21日 19:00-21:00 在线笔试"))
    ext_sch = {"window": ("截止日", datetime.datetime(2026, 9, 19, 10, 0, tzinfo=CST),
                          datetime.datetime(2026, 9, 20, 23, 59, tzinfo=CST), "", False),
               "deadline": (None, None, None, 2), "sched": True}
    check("事务邮件→其他事项(截止式)", mail.plan_exam(ext_sch, "笔试", M("", "", 1)["date"]) ==
          ("其他事项", datetime.datetime(2026, 9, 20, 23, 59), None))

    print("== T9 看板聚合：考试数据 + 冲突检测 ==")
    spec = importlib.util.spec_from_file_location("kb", os.path.join(BASE, "中台看板.py"))
    kb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kb)
    mains = mail.load_main()
    mails = g.list_records(g.T_MAIL)
    exams = g.list_records(g.T_EXAM)
    agg = kb.aggregate(list(mains.values()), mails, exams)
    longi = next(x for x in agg["考试"] if x["公司"] == "字节跳动" and x["考试类型"] == "笔试")
    check("聚合含考试数据", len(agg["考试"]) == len(exams))
    check("考试行带岗位/状态(独立键展示)", longi["岗位"] == "" and longi["投递状态"] == "笔试中")
    check("跨天窗口起止完整(展示层只显首尾)", longi["开始时间"].startswith("2026-09-20") and longi["结束时间"].startswith("2026-09-22"))
    warns = mail.report_conflicts()
    check("冲突检测可运行(读Exams)", isinstance(warns, list))

    print("== T10 退役字段回归 ==")
    ok, reason = mail.process_one(M("【百度】二笔通知", "9月30日 19:00-21:00 笔试 https://exam.baidu.com/start?eid=2", uid=5), *(snapshot()), False)
    f = g.get_by_company("百度")["fields"]
    check("新邮件仍不写主表时间(只进Exams)", f.get("笔试时间") is None)
    check("二笔窗口升级提醒行(一场一条,2026-09-24定版)", len(exams_of("百度")) == 2 and any(
        x["fields"]["开始时间"].strftime("%m-%d") == "09-30" for x in exams_of("百度")))

    print("== T11 鲁棒性加固回归：SCHED误伤 / 链接上下文（2026-09-20 审计） ==")
    # 吉利 2026-09-20 真实邮件正文快照（多段式AI面试，正文含样板话术+Moka杂链）
    GEELY_BODY = ("邀请您参加吉利校园招聘线上测评，测评结果会作为面试关键依据，请您抽出宝贵时间认真作答\n"
                  "https://jinyuzhineng.com/su/CjXuh6V\n"
                  "1、本次测评全程摄像头监控；2、请使用Chrome浏览器；\n"
                  "3、未点击测评链接不会开始计时，您可以选择一个较为充裕的时间开始测评；\n"
                  "4、如果您在测评或者是AI面试过程中遇到技术问题可以拨打：400-096-3520；\n"
                  "如果您收到多份测评是因为为了保障您的答题体验，我们将AI面试拆为基础测评、AI面试、英语测评，"
                  "除基础测评外其他测评类型由HR根据与您沟通的情况在合适的时间进行发放，"
                  "如果您一次性收到多份测评并对此有疑问可以联系HR进行沟通\n"
                  "https://app.mokahr.com/exam-status?attendStatus=accepted&access_token=798c\n"
                  "https://app.mokahr.com/exam/reject-reason-page?access_token=798c\n"
                  "https://sctrack.sendcloud.net/track/unsubscribe2.do?p=eNpl")
    check("SCHED不误伤: 吉利样板话术(可选择充裕时间)", not mail.SCHED_SEL_PAT.search(GEELY_BODY))
    check("SCHED不误伤: 有效期内任意时间", not mail.SCHED_SEL_PAT.search("请在有效期内任意时间完成测评"))
    check("SCHED不误伤: 提前登录确认设备", not mail.SCHED_SEL_PAT.search("请您在测评当天提前15分钟登录确认设备正常"))
    check("SCHED仍识别: 请选择面试时间", bool(mail.SCHED_SEL_PAT.search("请于9月25日前选择面试时间")))
    check("SCHED仍识别: 预约笔试场次", bool(mail.SCHED_SEL_PAT.search("请尽快预约笔试场次")))
    check("SCHED不误伤: 确认是否参加(RSVP,东财)", not mail.is_sched_mail("东方财富校招测评通知", "请您确认是否参加 / Please confirm whether to p"))
    check("SCHED不误伤: 选择合适的时间进行笔试(阿里提醒)", not mail.is_sched_mail("【阿里巴巴】在线笔试提醒", "请您尽快选择合适的时间进行笔试，具体场次安排可查看校招官网"))
    check("SCHED不误伤: 确认题目的作答时间(vivo)", not mail.is_sched_mail("vivo人才测评通知", "请先在屏幕右上方确认题目的作答时间，以合理安排读题和做题的时间"))
    check("SCHED仍识别: 请预约面试时间", mail.is_sched_mail("【某公司】面试邀约", "请于明天12点前预约面试时间"))
    check("SCHED仍识别: 确认面试时间", bool(mail.SCHED_SEL_PAT.search("请于明天12点前确认面试时间")))
    check("吉利真实正文→真实测评短链(上下文胜出)", mail.extract_url(GEELY_BODY, []) == "https://jinyuzhineng.com/su/CjXuh6V")
    check("吉利类型判定仍=测评", mail.extract_type("来自吉利汽车集团的AI面试邀请", GEELY_BODY) == "测评")
    ok, reason = mail.process_one(M("来自吉利汽车集团的AI面试邀请", GEELY_BODY, uid=40), *(snapshot()), False)
    geely_ex = None
    for x in exams_of("吉利") if g.get_by_company("吉利") else []: geely_ex = x
    # 吉利不在主表则该邮件入队属正常降级；建一个再验全程
    if not geely_ex:
        g.add_record(g.T_MAIN, {"公司名称": "吉利", "投递状态": "简历评估"})
        ok, reason = mail.process_one(M("来自吉利汽车集团的AI面试邀请", GEELY_BODY, uid=40), *(snapshot()), False)
        geely_ex = (exams_of("吉利") or [None])[0]
    check("吉利全程: 邮件成功入库", ok)
    check("吉利全程: 类型=测评不再降级其他事项", geely_ex and geely_ex["fields"]["考试类型"] == "测评")
    check("吉利全程: 链接=真实测评短链", geely_ex and geely_ex["fields"].get("考试链接") == "https://jinyuzhineng.com/su/CjXuh6V")

    print("== T12 鲁棒性加固回归：同链接提醒去重 ==")
    g.add_record(g.T_MAIN, {"公司名称": "莉莉丝", "投递状态": "简历评估"})
    mains, companies, excludes = snapshot()
    LIL = "https://assess.lilith.com/p?tid=77"
    mail.process_one(M("【莉莉丝】AI面试邀请",
                       "未点击测评链接不会开始计时，您可以选择一个较为充裕的时间开始测评\n测评入口：" + LIL,
                       uid=41), mains, companies, excludes, False)
    exs = exams_of("莉莉丝")
    check("首封AI面建1行(测评+链接)", len(exs) == 1 and exs[0]["fields"]["考试类型"] == "测评"
          and exs[0]["fields"].get("考试链接") == LIL)
    check("无时限邮件带默认提醒标记", "默认提醒" in str(exs[0]["fields"].get("来源主题")))
    m2 = M("【莉莉丝】AI面试提醒", "您还未完成测评，请尽快完成\n测评入口：" + LIL, uid=42)
    m2["date"] = datetime.datetime(2026, 9, 21, 9, 0, tzinfo=CST)   # 两天后的提醒
    mail.process_one(m2, *(snapshot()), False)
    check("提醒邮件不新建行", len(exams_of("莉莉丝")) == 1)
    _, created = g.add_exam(g.get_by_company("莉莉丝")["id"], "测评", datetime.datetime(2026, 10, 1), 链接=LIL)
    check("add_exam 同记录+类型+链接幂等", not created and len(exams_of("莉莉丝")) == 1)
    _, created = g.add_exam(g.get_by_company("莉莉丝")["id"], "笔试", datetime.datetime(2026, 10, 1), 链接=LIL)
    check("同链接不同类型仍建新行", created and len(exams_of("莉莉丝")) == 2)

    print("== T13 鲁棒性加固回归：改期原地更新 ==")
    g.add_record(g.T_MAIN, {"公司名称": "蔚来", "投递状态": "面试中"})
    mains, companies, excludes = snapshot()
    IV = "9月25日 10:00-11:00 面试，房间链接 https://iv.nio.com/r?room=9 请准时参加"
    mail.process_one(M("【蔚来】面试邀约", IV, uid=43), mains, companies, excludes, False)
    exs = exams_of("蔚来")
    check("初始面试行 09-25 10:00", len(exs) == 1 and exs[0]["fields"]["开始时间"].strftime("%m-%d %H:%M") == "09-25 10:00")
    mail.process_one(M("【蔚来】面试时间调整通知",
                       "原定9月25日面试时间调整至9月26日 15:00-16:00，链接 https://iv.nio.com/r?room=9",
                       uid=44), *(snapshot()), False)
    exs = exams_of("蔚来")
    check("改期未新建行", len(exs) == 1)
    check("改期后开始 09-26 15:00", exs and exs[0]["fields"]["开始时间"].strftime("%m-%d %H:%M") == "09-26 15:00")
    check("改期后结束 16:00", exs and exs[0]["fields"].get("结束时间").strftime("%H:%M") == "16:00")
    check("来源主题带改期留痕", "改期" in str(exs[0]["fields"].get("来源主题")))
    check("状态仍面试中不回退", g.get_by_company("蔚来")["fields"].get("投递状态") == "面试中")

    print("== T14 鲁棒性加固回归：笔试相对时限/无时间假行/状态页链接（2026-09-20 真实案例） ==")
    # 蔚来 09-20 真实正文快照：72小时相对窗口（旧管道解析不出→建了收信时刻假行）
    NIO_BODY = ("笔试信息\n笔试地址：https://t.zijieimg.com/lk_EECAHlAw/\n"
                "笔试时间：作答链接有效期72小时，请务必在收到作答通知的72小时内进入本链接后续页面并完成作答\n"
                "考试过程需全程开启摄像头，请提前准备带摄像头的电脑，下载最新版Chrome浏览器。")
    w = mail.plan_window(NIO_BODY, M("", "", 1)["date"])
    check("蔚来: 72小时→相对时限(+72h)", w[0] == "相对时限" and w[2] - w[1] == datetime.timedelta(hours=72))
    recv = M("", "", 1)["date"].replace(tzinfo=None)
    ex = mail.plan_exam({"window": w, "deadline": (None, None, None, 2)}, "笔试", M("", "", 1)["date"])
    check("蔚来: 笔试行=有效期窗口(收信起·+72h止)", ex is not None and ex[0] == "笔试"
          and ex[1] == recv and ex[2] - recv == datetime.timedelta(hours=72))
    check("蔚来: 链接=笔试地址短链(上下文)", mail.extract_url(NIO_BODY, []) == "https://t.zijieimg.com/lk_EECAHlAw/")
    # 蚂蚁 09-19 真实正文快照：时间只在官网公告，邮件里无任何日期 → 不建假行
    ANT_BODY = ("考试时间：\n1）请点击任一考试入口查看具体笔试开放时间。\n"
                "2）您可在开放时段内任意时间进入系统开始AI Coding考试。\n"
                "5）场次详情请查阅 [校招官网笔试公告]。\n"
                "入口2： 直接考试入口 https://interview.antgroup.com/home/candidateExam?token=69a0")
    w = mail.plan_window(ANT_BODY, M("", "", 1)["date"])
    check("蚂蚁: 无日期→无信息", w[0] == "无信息")
    recv = M("", "", 1)["date"].replace(tzinfo=None)
    ex = mail.plan_exam({"window": w, "deadline": (None, None, None, 2)}, "笔试", M("", "", 1)["date"])
    check("蚂蚁: 无信息→每日提醒行(收信日起·无结束)", ex == ("笔试", datetime.datetime(recv.year, recv.month, recv.day), None))
    # 东财 09-20 真实正文快照：真实测评登录页 vs Moka 确认/拒绝页
    DF_BODY = ("此邮件中的链接包含思维逻辑测评及性格测评2个部分，请您提前预留充足的时间作答。点击\n"
               "https://test.zhiding.com.cn/WebTest/Login.aspx?aid=492277&pid=38135543&identify=20DC\n"
               "参加 / Yes https://app.mokahr.com/exam-status?attendStatus=accepted&access_token=82862\n"
               "不参加 / No https://app.mokahr.com/exam/reject-reason-page?access_token=82862")
    check("东财: 真实测评登录页胜出(状态页扣分)", mail.extract_url(DF_BODY, []) == "https://test.zhiding.com.cn/WebTest/Login.aspx?aid=492277&pid=38135543&identify=20DC")
    # 东财真实原文: “3天之内”(带“之”字) → 相对时限窗口, 起止都要
    DF_BODY2 = DF_BODY + "\n请您在收到此邮件的3天之内完成测评，谢谢！"
    w = mail.plan_window(DF_BODY2, M("", "", 1)["date"])
    check("东财: 3天之内→相对时限", w[0] == "相对时限" and w[2] - w[1] == datetime.timedelta(days=3))
    check("东财: 测评窗口起止(收信→+3天)", mail.plan_exam(
        {"window": w, "deadline": (None, None, None, 2), "sched": False}, "测评", M("", "", 1)["date"])
        == ("测评", M("", "", 1)["date"].replace(tzinfo=None),
            M("", "", 1)["date"].replace(tzinfo=None) + datetime.timedelta(days=3)))
    # 3天内 = 有效期窗口, 起止都要（用户定版 2026-09-20）
    w3 = ("相对时限", M("", "", 1)["date"].replace(tzinfo=None),
          M("", "", 1)["date"].replace(tzinfo=None) + datetime.timedelta(days=3), "3天内", False)
    ex3 = mail.plan_exam({"window": w3, "deadline": (None, None, None, 2)}, "测评", M("", "", 1)["date"])
    check("测评3天内→起止(收信→+3天)", ex3 == ("测评", w3[1], w3[2]))
    ex3b = mail.plan_exam({"window": w3, "deadline": (None, None, None, 2)}, "笔试", M("", "", 1)["date"])
    check("笔试3天内→起止(收信→+3天)", ex3b == ("笔试", w3[1], w3[2]))
    # 自然日变体（联想 2026-09-20 真实原文：「3个自然日内完成」）
    w4 = mail.plan_window("请在收到此笔试邀请后的3个自然日内完成笔试", M("", "", 1)["date"])
    check("联想: 3个自然日内→相对时限3天", w4[0] == "相对时限" and w4[2] - w4[1] == datetime.timedelta(days=3))
    # 生效/失效 成对窗口（阳光保险/讯飞测评 2026-09-21 真实原文，旧逻辑误编 2 小时假窗）
    SUN_BODY = ("本次测试邀请于2026年09月21日 周一 15:34生效，于2026年10月06日 周二 15:34失效。请抓紧时间完成。")
    w5 = mail.plan_window(SUN_BODY, M("", "", 1)["date"])
    check("阳光: 生效失效→时间窗15天", w5[0] == "时间窗"
          and w5[1] == datetime.datetime(2026, 9, 21, 15, 34, tzinfo=mail.CST)
          and w5[2] == datetime.datetime(2026, 10, 6, 15, 34, tzinfo=mail.CST))
    ex5 = mail.plan_exam({"window": w5, "deadline": (None, None, None, 2)}, "笔试", M("", "", 1)["date"])
    check("阳光: 笔试行=完整起止(非2小时假窗)", ex5 == ("笔试",
          datetime.datetime(2026, 9, 21, 15, 34), datetime.datetime(2026, 10, 6, 15, 34)))
    TF_BODY = ("本次测试邀请于 2026年09月21日 周一 16:11 生效，于 2026年09月28日 周一 16:11 失效。请抓紧时间完成测评。")
    w6 = mail.plan_window(TF_BODY, M("", "", 1)["date"])
    ex6 = mail.plan_exam({"window": w6, "deadline": (None, None, None, 2)}, "测评", M("", "", 1)["date"])
    check("讯飞测评: 生效失效→起止7天", ex6 == ("测评", datetime.datetime(2026, 9, 21, 16, 11),
          datetime.datetime(2026, 9, 28, 16, 11)))
    print("== T15 每日状态快照（漏斗历史） ==")
    sid, created = g.snapshot_status()
    snaps = g.list_records(g.T_SNAP)
    check("快照表自动建+写入1行", len(snaps) == 1 and created)
    f = snaps[0]["fields"]
    exp = {"s1": sum(1 for r in mail.load_main().values() if str(r["fields"].get("投递状态")) == "简历评估"),
           "s3": sum(1 for r in mail.load_main().values() if str(r["fields"].get("投递状态")) == "笔试中"),
           "total": len(mail.load_main())}
    check("快照计数与主表一致", f.get("s1") == exp["s1"] and f.get("s3") == exp["s3"] and f.get("total") == exp["total"])
    sid2, created2 = g.snapshot_status()
    check("同日重复运行=upsert不新增", not created2 and len(g.list_records(g.T_SNAP)) == 1)

    print("== T16 2026-09-30 修复回归（B1原子写/B2备份/B5链接排除/B7营销防御） ==")
    # B5: 考试链接与网申链接同域不再被误杀；网申链接本身仍排除（按 协议+域+路径 精确匹配）
    check("B5: 同域考试链接不被排除", mail.extract_url(
        "请登录 https://campus.tencent.com/exam/xxxx?token=abc 完成笔试", ["https://campus.tencent.com"])
        == "https://campus.tencent.com/exam/xxxx?token=abc")
    check("B5: 网申链接本身仍被排除", mail.extract_url(
        "进度查询 https://app.mokahr.com/xyz/apply?uid=1", ["https://app.mokahr.com/xyz/apply?uid=1"]) == "")
    check("B5: 同路径忽略query仍排除", mail.extract_url(
        "进度查询 https://app.mokahr.com/xyz/apply?uid=2", ["https://app.mokahr.com/xyz/apply"]) == "")
    # B7: 营销主题不自动挂账（进待确认队列人工判断）
    _, companies_b7, _ = snapshot()
    n, rid, s, amb = mail.match_company("【华为云】服务器5折优惠，测试体验!", "华为云大促", companies_b7)
    check("B7: 营销主题不挂账进人工确认", n is None and rid is None and amb and "营销" in amb)
    check("B7: 正常面试主题不受影响", mail.match_company("【腾讯】面试邀请", "", {"腾讯": [(1, "简历评估")]})[0] == "腾讯")
    # B1: 原子写 / 跨进程锁 / 损坏队列保留
    tmpd = tempfile.mkdtemp(prefix="zq_fix_")
    pj = os.path.join(tmpd, "t.json")
    g.atomic_write_json(pj, {"items": [1, 2, 3]})
    check("B1: 原子写读回一致", json.load(open(pj, encoding="utf-8")) == {"items": [1, 2, 3]})
    check("B1: 无临时文件残留", not [x for x in os.listdir(tmpd) if x.startswith(".tmp-")])
    try:
        with g.cross_lock(pj, timeout=0.3):
            with g.cross_lock(pj, timeout=0.3):
                check("B1: 互斥生效(应不可重入)", False)
    except TimeoutError:
        check("B1: 互斥生效(二次加锁超时)", True)
    check("B1: 释放后锁文件清理", not os.path.exists(pj + ".lock"))
    stale_p = os.path.join(tmpd, "s.json")
    os.close(os.open(stale_p + ".lock", os.O_CREAT | os.O_WRONLY))
    mt = os.stat(stale_p + ".lock").st_mtime
    os.utime(stale_p + ".lock", (mt - 3600, mt - 3600))   # 伪造 1 小时前的崩溃残留锁
    with g.cross_lock(stale_p, timeout=2):
        check("B1: 陈旧锁自动接管", True)
    _old_qp = mail.QUEUE_PATH
    try:
        badq = os.path.join(tmpd, "q.json")
        open(badq, "w", encoding="utf-8").write('{"items": [{"uid": 1')
        mail.QUEUE_PATH = badq
        q = mail.load_queue()
        check("B1: 损坏队列→按空处理且保留.corrupt", q == {"items": []} and os.path.exists(badq + ".corrupt"))
        mail.save_queue({"items": []})
        check("B1: 修复后保存完好(不再静默清空真数据)", json.load(open(badq, encoding="utf-8")) == {"items": []})
    finally:
        mail.QUEUE_PATH = _old_qp
    # B2: 备份=在线一致性快照（backup API），可读且含主表
    import sqlite3 as _sq
    bkp = os.path.join(tmpd, "sub", "backup.sqlite")
    g.download_backup(bkp)
    _c = _sq.connect(bkp)
    _n = _c.execute("SELECT COUNT(*) FROM Deliveries").fetchone()[0]
    _c.close()
    check("B2: 备份可读且含主表行", _n > 0)
    # B3: demo 聚合传合成快照→完全不触碰 SQLite（连接缓存重置后目标库文件不会被创建）
    spec2 = importlib.util.spec_from_file_location("kb2", os.path.join(BASE, "中台看板.py"))
    kb2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(kb2)
    _fresh = os.path.join(tempfile.mkdtemp(prefix="zq_demo_"), "demo.sqlite")
    _old_sp = os.environ.get("SQLITE_PATH")
    os.environ["SQLITE_PATH"] = _fresh
    g._CONN = None
    try:
        demo_agg = kb2.aggregate(*kb2._demo(), snaps=kb2._demo_snaps())
        check("B3: demo聚合不建库且快照为合成数据", not os.path.exists(_fresh) and len(demo_agg["快照"]) == 7)
    finally:
        os.environ["SQLITE_PATH"] = _old_sp
        g._CONN = None

    print("== T17 未匹配邮件自动创建志愿（2026-10-01） ==")
    def M2(subj, body, frm="hr@example.cn", disp="", uid=1):
        return {"uid": uid, "date": datetime.datetime(2026, 10, 1, 9, 0, tzinfo=CST),
                "from": frm, "from_display": disp, "subject": subj, "body": body}
    mains, companies, excludes = snapshot()
    # ① 主题括号公司名：感谢投递 → 自动建档（用户的原话场景）
    ok, reason = mail.process_one(M2("【示例图科技】感谢您投递我司岗位", "感谢您的投递，我们将尽快反馈。", uid=51),
                                  mains, companies, excludes, False)
    rec = g.find_record("示例图科技", "")
    check("① 括号公司名→自动建档", ok and rec is not None)
    check("① 默认简历评估+邮件已存档", rec and rec["fields"].get("投递状态") == "简历评估"
          and len(g.archives_of("示例图科技")) == 1)
    # ② 无括号但私有域名 → 从域名建档（并联动建考试行）
    ok, reason = mail.process_one(M2("您有新的面试安排", "请于10月9日 14:00 参加面试", frm="hr@novabook.cn", uid=52),
                                  *(snapshot()), False)
    rec2 = g.find_record("novabook", "")
    check("② 私有域名→自动建档", ok and rec2 is not None)
    check("② 联动：状态推进+考试行", rec2 and rec2["fields"].get("投递状态") == "面试中"
          and len(g.exams_of(rec2["id"])) == 1)
    # ③ 同公司第二封 → 挂已有志愿，不重复建档
    n0 = len(g.list_records(g.T_MAIN))
    ok, reason = mail.process_one(M2("【示例图科技】在线测评邀请", "请完成测评", uid=53), *(snapshot()), False)
    check("③ 二封邮件不重复建档", ok and len(g.list_records(g.T_MAIN)) == n0)
    # ④ 平台域名（Moka）不建 → 进队列
    ok, reason = mail.process_one(M2("测评邀请，请尽快完成", "测评入口见邮件", frm="noreply@mokahr.com", disp="Moka", uid=54),
                                  *(snapshot()), False)
    check("④ 平台域名不自动建(进队列)", not ok and reason == "公司未匹配")
    # ⑤ 开关关闭 → 不建档
    _orig_gc = g.get_config
    g.get_config = lambda: {**_orig_gc(), "自动建志愿": False}
    try:
        ok, reason = mail.process_one(M2("【新桥证券】感谢您投递我司", "感谢投递", uid=55), *(snapshot()), False)
    finally:
        g.get_config = _orig_gc
    check("⑤ 开关关闭→不建档进队列", not ok and reason == "公司未匹配" and g.find_record("新桥证券", "") is None)
    # ⑥ 营销邮件不建（B7 防御优先于自动创建）
    ok, reason = mail.process_one(M2("【华为云】服务器5折优惠，测试体验!", "华为云大促", uid=56), *(snapshot()), False)
    check("⑥ 营销邮件不自动建", not ok and g.find_record("华为云", "") is None)
    # ⑦ 宣讲会/招聘会邮件不自动建档——宣讲会不算投递（2026-10-01 用户定版），进队列给用户选
    ok, reason = mail.process_one(M2("【新芽科技】2027 校招宣讲会邀请", "9月20日 19:00 线上宣讲会", uid=57),
                                  *(snapshot()), False)
    check("⑦ 宣讲会不自动建档(即使有括号公司名)", not ok and "宣讲会" in str(reason)
          and g.find_record("新芽科技", "") is None)
    ok, reason = mail.process_one(M2("秋季招聘会邀请函，众多企业参加", "线下招聘会", uid=58),
                                  *(snapshot()), False)
    check("⑦ 招聘会同样不自动建", not ok and "宣讲会" in str(reason))

    print("== T18 横幅一键新建志愿（queue_create 语义，2026-10-01） ==")
    # 用户在横幅确认公司名 → 建档 → 邮件按正常管道处理（等价于 /api/queue_create 的核心路径）
    g.add_record(g.T_MAIN, {"公司名称": "新芽科技", "投递状态": "简历评估"})
    mains, companies, excludes = snapshot()
    ok, reason = mail.process_one(M2("【新芽科技】2027 校招宣讲会邀请", "9月20日 19:00 线上宣讲会", uid=57),
                                  mains, companies, excludes, False)
    check("建好志愿后宣讲会邮件正常处理", ok)
    check("宣讲会入档不推状态", g.get_by_company("新芽科技")["fields"].get("投递状态") == "简历评估"
          and len(g.archives_of("新芽科技")) == 1)

    print("== T19 拒信不误判 Offer（2026-10-02） ==")
    # 反例：拒绝句式含「录用/录取」子串，曾被 :202 先命中判成 Offer → 推进吸收态「已通过」
    check("不予录用→感谢信", mail.extract_type("【某公司】不予录用通知", "") == "感谢信")
    check("未能被录用→感谢信", mail.extract_type("很遗憾，您未能被录用", "") == "感谢信")
    check("录用评估未通过→感谢信", mail.extract_type("您未能通过本次录用评估", "") == "感谢信")
    check("拒信推进为终止而非已通过",
          mail.plan_status(mail.extract_type("很遗憾，您未能被录用", ""), "无信息", "面试中") == "终止")
    # 正例：真 Offer 不受影响
    check("录用通知仍→Offer", mail.extract_type("【某公司】录用通知", "") == "Offer")
    check("offer letter 仍→Offer", mail.extract_type("Offer Letter - 某公司", "") == "Offer")
    check("恭喜录取仍→Offer", mail.extract_type("恭喜您被我校录取", "") == "Offer")

    print("== T20 数字区间不误判为日期（2026-10-02） ==")
    _today = datetime.date(2026, 10, 2)
    _recv = datetime.datetime(2026, 10, 2, 9, 0, tzinfo=CST)
    check("3-5天内 不再是3月5日", mail._date_cands("请在3-5天内完成测评", _today) == [])
    check("12-15K 不是12月15日", mail._date_cands("月薪12-15K，欢迎投递", _today) == [])
    check("9-10月 不是9月10日", mail._date_cands("我们将于9-10月开展校招", _today) == [])
    check("3-15 真日期仍可解析", [c for c in mail._date_cands("测评需在 3-15 前完成", _today)
                                  if c[2].month == 3 and c[2].day == 15])
    w = mail.plan_window("请在3-5天内完成测评", _recv)
    check("3-5天内 走相对时限5天", w[0] == "相对时限" and (w[2] - _recv).days == 5, str(w))

    print("== T21 URL 抽取不吃中文标点（2026-10-02） ==")
    check("句号不粘链接尾部", mail.extract_url("请点击 https://exam.x.com/abc。如有问题请联系", []) == "https://exam.x.com/abc")
    check("感叹号不粘链接尾部", mail.extract_url("入口 https://exam.x.com/def！速去", []) == "https://exam.x.com/def")
    check("冒号不粘链接尾部", mail.extract_url("链接 https://exam.x.com/ghi：请完成", []) == "https://exam.x.com/ghi")

    print("== T22 别名仅按主题匹配（2026-10-02） ==")
    _alias_bak = dict(mail.COMPANY_ALIAS)
    mail.COMPANY_ALIAS = {"字节": "字节跳动", "阿里": "阿里巴巴"}
    try:
        _comps = {"字节跳动": [(9001, "简历评估")], "阿里巴巴": [(9002, "简历评估")]}
        n, rid, s, amb = mail.match_company("转发：互联网周报", "本周比肩阿里的大事件，另一家对标腾讯", _comps)
        check("正文提及别名不再挂账", n is None and rid is None, str((n, rid, s)))
        n, rid, s, amb = mail.match_company("字节 秋招提前批启动", "", _comps)
        check("主题别名仍生效", (n, rid) == ("字节跳动", 9001), str((n, rid)))
    finally:
        mail.COMPANY_ALIAS = _alias_bak

    print("== T23 自动建档停用词（学校/城市/教育域名/英文泛词，2026-10-02） ==")
    check("学校名不建档", mail.extract_company_guess("【清华大学】在线笔试通知", "", "")[0] is None)
    check("城市名不建档", mail.extract_company_guess("您有新的面试安排", "hr@szrc.com", "深圳招聘")[0] != "深圳")
    check("教育域名不建档", mail.extract_company_guess("笔试通知", "hr@tsinghua.edu.cn", "")[0] is None)
    check("英文泛词不建档", mail.extract_company_guess("【Notice】You have a new update", "", "")[0] is None)
    check("正常括号公司名仍建档", mail.extract_company_guess("【示例图科技】笔试通知", "", "")[0] == "示例图科技")

    print("== T24 关键词命中 subject_hit（含英文，2026-10-02） ==")
    check("中文关键词命中", hasattr(mail, "subject_hit") and mail.subject_hit("【百度】在线笔试邀请") is True)
    check("英文测评邀约命中", hasattr(mail, "subject_hit") and mail.subject_hit("Online Assessment Invitation") is True)
    check("英文面试邀约命中", hasattr(mail, "subject_hit") and mail.subject_hit("Interview Invitation - XX Corp") is True)
    check("无关邮件不命中", hasattr(mail, "subject_hit") and mail.subject_hit("关于国庆放假安排的通知") is False)
    check("营销邮件不命中", hasattr(mail, "subject_hit") and mail.subject_hit("双十一全场五折") is False)

    print("== T25 mail_state 损坏不卡死同步 + 路径可环境变量覆盖（2026-10-02） ==")
    import subprocess as _sp
    _raw = (b"From: HR <hr@ex.com>\r\n"
            b"Subject: Online Assessment Invitation\r\n"
            b"Date: Mon, 01 Oct 2026 09:00:00 +0800\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + "请在3天内完成测评".encode("utf-8"))

    class _FakeIMAP:
        def __init__(self, raws): self._raws = raws
        def uid(self, cmd, *a):
            if cmd == "search":
                return "OK", [b" ".join(str(u).encode() for u in sorted(self._raws))]
            u = int(a[0])
            return "OK", [(b"1 (RFC822 {%d}" % len(self._raws[u]), self._raws[u]), b")"]
        def logout(self): pass

    _conn_bak = mail.imap_connect
    mail.imap_connect = lambda select="INBOX": _FakeIMAP({11: _raw})
    try:
        with open(mail.STATE_PATH, "w", encoding="utf-8") as _fp:
            _fp.write("{oops 不是合法JSON")
        try:
            mails, newmax = mail.fetch_mails()
            _err = None
        except Exception as _e:
            mails, newmax, _err = [], 0, _e
        check("状态文件损坏不炸同步", _err is None, str(_err))
        check("损坏回退后仍抓到邮件", len(mails) == 1 and newmax == 11 and mails[0]["uid"] == 11)
    finally:
        mail.imap_connect = _conn_bak
    _env = dict(os.environ, MAIL_QUEUE_PATH=os.path.join(tempfile.mkdtemp(prefix="envq_"), "q.json"))
    _out = _sp.run([sys.executable, "-c",
                    f"import sys; sys.path.insert(0, {BASE!r}); import mail; print(mail.QUEUE_PATH)"],
                   capture_output=True, text=True, env=_env, timeout=60)
    check("QUEUE_PATH 可用环境变量覆盖", _out.stdout.strip().endswith("q.json"),
          (_out.stdout + _out.stderr)[:200])

    print("== T26 队列自动重试上限（2026-10-02） ==")
    _it = {"uid": 7, "原因": "公司未匹配"}
    if hasattr(mail, "mark_retry_failure"):
        mail.mark_retry_failure(_it, "公司未匹配")
        mail.mark_retry_failure(_it, "公司未匹配")
    check("重试2次未挂起", _it.get("挂起") is None and _it.get("重试次数") == 2, str(_it))
    if hasattr(mail, "mark_retry_failure"):
        mail.mark_retry_failure(_it, "公司未匹配")
    check("重试3次挂起+原因说明", _it.get("挂起") is True and "挂起" in str(_it.get("原因")), str(_it))
    check("挂起条目不再自动重试", hasattr(mail, "auto_retry_ok") and mail.auto_retry_ok(_it) is False)
    check("普通条目仍自动重试", hasattr(mail, "auto_retry_ok") and mail.auto_retry_ok({"uid": 8}) is True)

    print("== T27 cross_lock 接管后旧持有者不误删新锁（2026-10-02） ==")
    _lp = os.path.join(tempfile.mkdtemp(prefix="lock_"), "q.json")
    _ctx_a = g.cross_lock(_lp, timeout=1, stale=3600)
    _ctx_a.__enter__()
    try:
        with g.cross_lock(_lp, timeout=1, stale=0):   # 模拟 A 卡死超 stale 被 B 原子接管
            _ctx_a.__exit__(None, None, None)          # A（旧持有者）此刻释放
            check("旧持有者释放不删新锁", os.path.exists(_lp + ".lock"))
        check("新持有者释放后锁消失", not os.path.exists(_lp + ".lock"))
    finally:
        try:
            _ctx_a.__exit__(None, None, None)
        except Exception:
            pass
    with g.cross_lock(_lp, timeout=1, stale=3600):
        check("锁可再次正常获取", True)

    print("== T28 旧库缺列自动迁移（2026-10-02） ==")
    import sqlite3 as _sq2
    _old = os.path.join(tempfile.mkdtemp(prefix="oldschema_"), "old.sqlite")
    _con = _sq2.connect(_old)
    _con.execute('CREATE TABLE "Deliveries" ("id" INTEGER PRIMARY KEY AUTOINCREMENT,'
                 ' "公司名称" TEXT, "投递状态" TEXT)')
    _con.execute('INSERT INTO "Deliveries" ("公司名称","投递状态") VALUES (?,?)', ("老库公司", "简历评估"))
    _con.commit()
    _con.close()
    _old_sp, _old_conn = g.SQLITE_PATH, g._CONN
    g.SQLITE_PATH, g._CONN = _old, None
    try:
        try:
            _rows = g.list_records(g.T_MAIN)
            _mig_err = None
        except Exception as _e:
            _rows, _mig_err = [], _e
        check("旧库缺列不崩", _mig_err is None and bool(_rows), str(_mig_err))
        check("补列后字段齐全", bool(_rows) and all(k in _rows[0]["fields"] for k in ("公司名称", "招聘渠道", "内推人")))
        check("旧行数据保留", any(r["fields"].get("公司名称") == "老库公司" for r in _rows))
    finally:
        g.SQLITE_PATH, g._CONN = _old_sp, _old_conn

    print("== T29 自动备份 backup_now + 轮转（2026-10-02） ==")
    _bdir = os.path.join(tempfile.mkdtemp(prefix="bk_"), "backups")
    _orig_gc2 = g.get_config
    g.get_config = lambda: {**_orig_gc2(), "备份目录": _bdir}
    try:
        if not hasattr(g, "backup_now"):
            check("备份文件生成", False, "缺少 backup_now")
            check("备份含主表数据", False, "缺少 backup_now")
            check("轮转保留≤30份", False, "缺少 backup_now")
        else:
            _p1 = g.backup_now()
            check("备份文件生成", os.path.exists(_p1) and os.path.getsize(_p1) > 0)
            _con3 = _sq2.connect(_p1)
            _n3 = _con3.execute('SELECT COUNT(*) FROM "Deliveries"').fetchone()[0]
            _con3.close()
            check("备份含主表数据", _n3 >= 1, f"rows={_n3}")
            for _i in range(32):
                open(os.path.join(_bdir, f"中台-2026010{_i % 10}-0000-{_i:02d}.sqlite"), "w").close()
            g.backup_now()
            _left = [f for f in os.listdir(_bdir) if f.endswith(".sqlite")]
            check("轮转保留≤30份", len(_left) <= 30, f"left={len(_left)}")
    finally:
        g.get_config = _orig_gc2

    print("== T30 密码提示不回显真实字符（2026-10-02） ==")
    _spec_b = importlib.util.spec_from_file_location("board", os.path.join(BASE, "中台看板.py"))
    _board = importlib.util.module_from_spec(_spec_b)
    sys.modules["board"] = _board
    _spec_b.loader.exec_module(_board)
    check("有密码→固定提示", hasattr(_board, "_mask_hint")
          and _board._mask_hint("abcde") == "已配置" and _board._mask_hint("x" * 32) == "已配置")
    check("无密码→空提示", hasattr(_board, "_mask_hint") and _board._mask_hint("") == "")

    print("== T31 外观自定义：logo 校验/存储/恢复 + 站点名（2026-10-04） ==")
    _bdir31 = tempfile.mkdtemp(prefix="brand_")
    _cfg31 = os.path.join(_bdir31, "config.json")
    os.environ["BRANDING_DIR"] = _bdir31
    _spec_br = importlib.util.spec_from_file_location("board_br", os.path.join(BASE, "中台看板.py"))
    _br = importlib.util.module_from_spec(_spec_br)
    sys.modules["board_br"] = _br
    _spec_br.loader.exec_module(_br)
    _orig_cfg31 = g.CONFIG_PATH
    g.CONFIG_PATH = _cfg31
    try:
        _png = (b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        _jpg = b"\xff\xd8\xff" + b"0" * 32
        _webp = b"RIFF" + b"0" * 4 + b"WEBP" + b"0" * 16
        _bad = b"GIF89a" + b"0" * 16
        # 校验器：魔数与扩展名必须匹配
        for _ext, _blob in (("png", _png), ("jpg", _jpg), ("jpeg", _jpg), ("webp", _webp)):
            try:
                _br._brand_validate(_ext, _blob)
                check(f"魔数校验通过 {_ext}", True)
            except ValueError as _e:
                check(f"魔数校验通过 {_ext}", False, str(_e))
        try:
            _br._brand_validate("png", _bad)
            check("改名 GIF 冒充 PNG 被拒", False, "未拒绝")
        except ValueError:
            check("改名 GIF 冒充 PNG 被拒", True)
        try:
            _br._brand_validate("exe", _png)
            check("扩展名白名单外被拒", False, "未拒绝")
        except ValueError:
            check("扩展名白名单外被拒", True)
        try:
            _br._brand_validate("png", b"\x89PNG\r\n\x1a\n" + b"0" * (_br.BRAND_MAX))
            check("超过 2MB 被拒", False, "未拒绝")
        except ValueError:
            check("超过 2MB 被拒", True)
        # 站点名清洗：控制字符剔除、超长拒绝
        check("站点名清洗", _br._brand_clean_name(" 我的中台\n") == "我的中台")
        try:
            _br._brand_clean_name("x" * 31)
            check("站点名超长被拒", False, "未拒绝")
        except ValueError:
            check("站点名超长被拒", True)
        # 存储：写 png → 换 jpg（旧 png 清掉）→ 恢复默认（文件与配置都干净）
        os.makedirs(_bdir31, exist_ok=True)
        _cfg = g.get_config()
        _cfg["品牌Logo"], _cfg["品牌Logo时间"] = "logo.png", 111
        g.save_config(_cfg)
        _br._brand_clear_logo()
        open(os.path.join(_bdir31, "logo.png"), "wb").write(_png)
        _files = _br._brand_logo_files()
        check("logo 文件可发现", len(_files) == 1 and _files[0].endswith("logo.png"))
        open(os.path.join(_bdir31, "logo.jpg"), "wb").write(_jpg)
        _br._brand_clear_logo()
        check("换扩展名时旧 logo 清干净", _br._brand_logo_files() == [])
        _cfg2 = g.get_config()
        _cfg2["品牌Logo"], _cfg2["品牌站点名"] = "logo.png", "我的中台"
        g.save_config(_cfg2)
        check("配置持久化", g.get_config().get("品牌站点名") == "我的中台")
        check("默认配置含品牌键", all(k in _br._grist().get_config()
              for k in ("品牌站点名", "品牌Logo", "品牌Logo时间")))
    finally:
        g.CONFIG_PATH = _orig_cfg31
        os.environ.pop("BRANDING_DIR", None)

finally:
    # 恢复真实路径（测试全程未触碰真实队列/状态文件）
    mail.QUEUE_PATH, mail.STATE_PATH = _real_queue_path, _real_state_path

print()
print(f"验收结果: 通过 {OK} 项" + (f"，失败 {len(FAIL)} 项: {FAIL}" if FAIL else "，全部通过 ✅"))
sys.exit(1 if FAIL else 0)
