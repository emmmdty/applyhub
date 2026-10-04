# -*- coding: utf-8 -*-
"""SQLite 版（8791）全功能测试：API 全 CRUD + 联动 + 队列 + 管道 + 备份 + 与 Grist 版读一致。"""
import os, sys, json, importlib.util, datetime, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("QA_API") or "http://127.0.0.1:8790"   # 沙盒跑法：QA_API=http://127.0.0.1:8794
SANDBOX_ENV = {"SQLITE_PATH", "MAIL_STATE_PATH", "MAIL_QUEUE_PATH", "MAIL_CRED_PATH"}
CST = datetime.timezone(datetime.timedelta(hours=8))

spec = importlib.util.spec_from_file_location("gl", os.path.join(HERE, "grist_store.py"))
gL = importlib.util.module_from_spec(spec); sys.modules["gl"] = gL; spec.loader.exec_module(gL)
import atexit
def _cleanup():
    try:
        # 队列兜底清理：测试入队的 999001 若因重试失败滞留真实队列，强制移除并标记已见
        sys.path.insert(0, HERE)
        import mail as _m
        _q = _m.load_queue()
        _n = len(_q["items"])
        _q["items"] = [x for x in _q["items"] if x.get("uid") != 999001]
        if len(_q["items"]) != _n:
            _m.save_queue(_q)
        for x in gL.list_records(gL.T_MAIL):
            if "测试公司X" in str(x["fields"].get("主题", "")):
                gL.delete_record(gL.T_MAIL, x["id"])
        r = gL.get_by_company("测试公司X")
        if r:
            for x in gL.exams_of(r["id"]): gL.delete_record(gL.T_EXAM, x["id"])
            gL.delete_record(gL.T_MAIN, r["id"])
    except Exception: pass
atexit.register(_cleanup)

OK, FAIL = 0, []
def check(name, cond, extra=""):
    global OK
    if cond: OK += 1; print(f"  ✓ {name}")
    else: FAIL.append(name); print(f"  ✗ {name} {extra}")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def api(path, body=None, method=None):
    req = urllib.request.Request(API + "/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"})
    try:
        return json.loads(_OPENER.open(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "_status": e.code, "error": e.read().decode()[:120]}

# ---- 沙盒强制断言（2026-10-02 事故加固）：本套件会写入/删除数据，必须确认 API 服务端
# 与本进程直连的是同一个非默认数据库。曾因环境变量名不合法被 bash 静默丢弃，
# 套件静默落到 8790 生产实例，误写 2 行（已修复）——此检查保证同类事故直接拒绝启动。
_srv_settings = api("api/settings")
_srv_db = str(((_srv_settings or {}).get("路径") or {}).get("数据库") or "")
_direct_db = os.path.abspath(gL.SQLITE_PATH)
_is_sandbox_env = bool(SANDBOX_ENV & set(os.environ))
if os.path.abspath(_srv_db) != _direct_db or not _is_sandbox_env:
    print("✗ 拒绝运行：本测试会写库，只允许对沙盒实例跑。")
    print(f"  API 服务端数据库: {_srv_db or '(拿不到/服务未启动)'}")
    print(f"  本进程直连数据库: {_direct_db}")
    print(f"  沙盒环境变量: {'已设置' if _is_sandbox_env else '未设置'}")
    print("  正确姿势（端口与路径自定，三处保持一致）：")
    print("    SQLITE_PATH=/tmp/t.sqlite MAIL_STATE_PATH=/tmp/s.json MAIL_QUEUE_PATH=/tmp/q.json \\")
    print("    MAIL_CRED_PATH=/tmp/c.json python3 中台看板.py 8794")
    print("    SQLITE_PATH=/tmp/t.sqlite MAIL_STATE_PATH=/tmp/s.json MAIL_QUEUE_PATH=/tmp/q.json \\")
    print("    MAIL_CRED_PATH=/tmp/c.json QA_API=http://127.0.0.1:8794 python3 功能测试.py")
    sys.exit(2)
print(f"== 沙盒断言通过（数据库: {_direct_db}） ==")

print("== A. 静态与读 ==")
check("首页 HTML", urllib.request.urlopen(API + "/", timeout=5).status == 200)
check("favicon", urllib.request.urlopen(API + "/assets/favicon-96.png", timeout=5).status == 200)
d = api("api/data")
check("/api/data ok", d.get("ok") is True)
check("聚合键齐全", all(k in d for k in ("记录", "考试", "存档数", "最近存档", "快照")))
check("/api/queue", api("api/queue").get("ok") is True)
PRE_COUNT = len(d["记录"])

print("== A2. 设置接口（邮箱凭据/运行参数可视化） ==")
s = api("api/settings")
check("/api/settings ok", s.get("ok") is True)
check("设置字段齐全", all(k in s for k in ("mail_host", "mail_user", "has_password", "密码提示", "已配置", "配置", "路径")))
check("密码不回传明文", "mail_password" not in s)
r2 = api("api/settings", {"公司别名": (s.get("配置") or {}).get("公司别名") or {},
                          "关键词": (s.get("配置") or {}).get("关键词") or []})
check("设置原值回写(no-op)", r2.get("ok") is True)
check("非法端口→400", api("api/settings", {"端口": 80}).get("_status") == 400)
check("别名非对象→400", api("api/settings", {"公司别名": "abc"}).get("_status") == 400)
check("自动建志愿开关往返", api("api/settings", {"自动建志愿": False}).get("ok") is True
      and api("api/settings", {"自动建志愿": True}).get("ok") is True
      and (api("api/settings").get("配置") or {}).get("自动建志愿") is True)

print("== A3. 安全：同源防护（CSRF/DNS rebinding，2026-10-02） ==")
def api_raw(path, body=None, headers=None):
    req = urllib.request.Request(API + "/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        return json.loads(_OPENER.open(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "_status": e.code, "error": e.read().decode()[:120]}
check("恶意 Origin 的写接口→403",
      api_raw("api/set_status", {"id": 1, "值": "终止"}, {"Origin": "http://evil.example"}).get("_status") == 403)
check("恶意 Host 的 GET→403", api_raw("api/data", headers={"Host": "evil.example"}).get("_status") == 403)
check("恶意 Host 的 POST→403",
      api_raw("api/add_record", {"公司名称": "X", "岗位名称": "y"}, {"Host": "evil.example"}).get("_status") == 403)
check("恶意 Content-Type→415",
      api_raw("api/set_status", {"id": 1, "值": "简历评估"}, {"Content-Type": "text/plain"}).get("_status") == 415)
check("正常请求不受影响", api_raw("api/data").get("ok") is True)
check("密码提示不回显真实字符", api("api/settings").get("密码提示") in ("已配置", ""))

print("== A4. 立即同步（/api/sync_now + /api/sync_status，2026-10-02） ==")
import time as _time
sd = api("api/sync_now", {})
check("sync_now 可触发", sd.get("ok") is True and sd.get("started") is True, str(sd))
sd2 = api("api/sync_now", {})
check("同步中重复触发→拒绝", sd2.get("ok") is False, str(sd2))
ss = api("api/sync_status")
check("sync_status 字段齐全", ss.get("ok") is True and ss.get("running") in (True, False)
      and "started" in ss and "输出" in ss, str(ss)[:150])
_ss_last = None
for _ in range(90):   # 沙盒无凭据 → 子进程快速退出（真实环境同步耗时不设上限，这里只验证闭环）
    _ss_last = api("api/sync_status")
    if not _ss_last.get("running"):
        break
    _time.sleep(1)
check("同步子进程闭环结束", _ss_last is not None and _ss_last.get("running") is False, str(_ss_last)[:150])
check("子进程输出可见", bool(str((_ss_last or {}).get("输出") or "").strip()), str(_ss_last)[:150])

print("== B. 志愿 CRUD ==")
r = api("api/add_record", {"公司名称": "测试公司X", "岗位名称": "测试岗", "投递状态": "简历评估", "投递日期": "2026-09-24"})
check("新增志愿", r.get("ok") is True and r.get("id"))
rid = r["id"]
check("独立键查重(重复→400)", api("api/add_record", {"公司名称": "测试公司X", "岗位名称": "测试岗"}).get("_status") == 400)
check("空公司名→400", api("api/add_record", {"公司名称": "  "}).get("_status") == 400)
check("改状态", api("api/set_status", {"id": rid, "值": "笔试中"}).get("ok") is True)
check("改后生效", any(x["公司名称"] == "测试公司X" and x["投递状态"] == "笔试中" for x in api("api/data")["记录"]))
check("行内编辑", api("api/update_record", {"id": rid, "状态备注": "联调测试"}).get("ok") is True)
check("公司名空→400", api("api/update_record", {"id": rid, "公司名称": ""}).get("_status") == 400)
check("公司名带斜杠→400(路径注入防御)", api("api/add_record", {"公司名称": "a/b", "岗位名称": "x"}).get("_status") == 400)
check("公司名带..→400", api("api/add_record", {"公司名称": "..", "岗位名称": "x"}).get("_status") == 400)
check("公司名带反斜杠→400", api("api/add_record", {"公司名称": "a\\b", "岗位名称": "x"}).get("_status") == 400)
check("公司名超100字→400", api("api/add_record", {"公司名称": "长" * 101, "岗位名称": "x"}).get("_status") == 400)
try:    # 服务端读 header 即拒收 413，客户端可能 EPIPE——两种表现都算"被拒"
    _r413 = api("api/add_record", {"公司名称": "超" * 600000, "岗位名称": "x"}).get("_status")
except (urllib.error.URLError, BrokenPipeError, ConnectionResetError):
    _r413 = 413
check("超大请求体→413", _r413 == 413)
db_row = gL.get_by_company("测试公司X")
check("落库核验(状态+备注)", db_row["fields"]["投递状态"] == "笔试中" and db_row["fields"]["状态备注"] == "联调测试")

print("== C. 考试行 CRUD + 幂等 + 联动 ==")
past = datetime.datetime(2026, 9, 20, 10, 0, tzinfo=CST)
past2 = datetime.datetime(2026, 9, 21, 10, 0, tzinfo=CST)
e1 = api("api/add_exam", {"记录": rid, "考试类型": "笔试", "开始时间": "2026-09-20T10:00", "结束时间": "2026-09-20T12:00"})
check("建考试行", e1.get("ok") is True)
e1["id"] = next(x["id"] for x in api("api/data")["考试"]
                if x["记录"] == rid and x["考试类型"] == "笔试" and "09-20" in x["开始时间"])
check("同键幂等→400", api("api/add_exam", {"记录": rid, "考试类型": "笔试", "开始时间": "2026-09-20T10:00"}).get("_status") == 400)
check("非法类型→400", api("api/add_exam", {"记录": rid, "考试类型": "OC", "开始时间": "2026-09-20T10:00"}).get("_status") == 400)
check("结束≤开始→400", api("api/add_exam", {"记录": rid, "考试类型": "面试", "开始时间": "2026-09-20T10:00", "结束时间": "2026-09-20T09:00"}).get("_status") == 400)
e2 = api("api/add_exam", {"记录": rid, "考试类型": "笔试", "开始时间": "2026-09-21T10:00"})
check("第二行(过期提醒行)", e2.get("ok") is True)
e2["id"] = next(x["id"] for x in api("api/data")["考试"]
                if x["记录"] == rid and x["考试类型"] == "笔试" and "09-21" in x["开始时间"])
check("改期", api("api/update_exam", {"id": e2["id"], "开始时间": "2026-09-21T11:00", "结束时间": "2026-09-21T12:30"}).get("ok") is True)
check("改类型", api("api/update_exam", {"id": e2["id"], "考试类型": "其他事项"}).get("ok") is True)
back = api("api/data")["考试"]
row2 = next(x for x in back if x["id"] == e2["id"])
check("改后生效(类型+时间)", row2["考试类型"] == "其他事项" and "11:00" in row2["开始时间"])
check("清空结束时间(空串=清空,2026-09-30修)", api("api/update_exam", {"id": e2["id"], "结束时间": ""}).get("ok") is True)
check("结束时间确已清空", next(x for x in api("api/data")["考试"] if x["id"] == e2["id"])["结束时间"] is None)
# 勾选联动：e1(过期) 与 e2 同类型已改其他事项→先改回笔试再联动
api("api/update_exam", {"id": e2["id"], "考试类型": "笔试"})
t = api("api/toggle", {"考试": e1["id"], "值": True})
check("勾选完成", t.get("ok") is True)
exs = gL.exams_of(rid)
done_flags = {x["id"]: x["fields"]["完成"] for x in exs}
check("兄弟行联动(过期同行同类型一并完成)", done_flags.get(e2["id"]) is True, str(done_flags))
check("完成时间已记录", all(x["fields"].get("完成时间") for x in exs if x["fields"]["完成"]))
check("取消勾选", api("api/toggle", {"考试": e1["id"], "值": False}).get("ok") is True)
check("删除考试行", api("api/delete_exam", {"id": e2["id"]}).get("ok") is True)
check("删除后消失", all(x["id"] != e2["id"] for x in api("api/data")["考试"]))

print("== D. 队列闭环 ==")
sys.path.insert(0, HERE)
import mail as _mail   # 队列读写走 load_queue/save_queue（跨进程锁+原子写，避免与定时任务互踩）
q = _mail.load_queue()
q["items"].append({"uid": 999001, "收信时间": "2026-09-24 18:00:00", "主题": "【测试公司X】笔试邀请",
                   "发件人": "t@test.com", "正文": "9月30日 19:00-21:00 笔试 https://t.x.com/1", "原因": "测试入队", "入队": "2026-09-24 18:00:00"})
_mail.save_queue(q)
qd = api("api/queue_detail?uid=999001")
check("队列详情(正文预览+链接)", qd.get("ok") is True and "正文预览" in (qd.get("item") or {})
      and any("t.x.com" in u for u in (qd.get("item") or {}).get("链接", [])), str(qd)[:120])
check("队列详情发件人回传", (qd.get("item") or {}).get("发件人") == "t@test.com")
rq = api("api/queue_retry", {"uid": 999001})
check("队列重试(匹配测试公司X并建行)", rq.get("ok") is True, str(rq))
check("队列条目出队", all(i.get("uid") != 999001 for i in _mail.load_queue()["items"]))
exs2 = gL.exams_of(rid)
check("重试建考试行(09-30 19:00)", any(x["fields"].get("开始时间") and "09-30 19:00" in x["fields"]["开始时间"].strftime("%m-%d %H:%M") for x in exs2))

print("== D2. 横幅一键新建志愿（/api/queue_create） ==")
sys.path.insert(0, HERE)
import mail as _m2
_q2 = _m2.load_queue()
_q2["items"] = [x for x in _q2["items"] if x.get("uid") not in (999002, 999003)]   # 防上次运行残留重复
_q2["items"].append({"uid": 999002, "收信时间": "2026-09-30 09:00:00", "主题": "【测试公司X】感谢您投递我司岗位",
                     "发件人": "hr@testx.com", "发件人名": "测试公司X招聘", "正文": "感谢您的投递", "原因": "公司未匹配",
                     "入队": "2026-09-30 09:00:00"})
_q2["items"].append({"uid": 999003, "收信时间": "2026-09-30 10:00:00", "主题": "【测试公司Y】感谢您投递我司岗位",
                     "发件人": "hr@testy.com", "发件人名": "", "正文": "感谢您的投递", "原因": "公司未匹配",
                     "入队": "2026-09-30 10:00:00"})
_m2.save_queue(_q2)
rc1 = api("api/queue_create", {"uid": 999002, "公司名称": "测试公司X"})
check("queue_create 挂靠已有志愿", rc1.get("ok") is True and rc1.get("公司名称") == "测试公司X", str(rc1)[:100])
check("条目出队", all(i.get("uid") != 999002 for i in _m2.load_queue()["items"]))
rc2 = api("api/queue_create", {"uid": 999003})
check("queue_create 按建议名自动建档", rc2.get("ok") is True and rc2.get("公司名称") == "测试公司Y", str(rc2)[:100])
qd2 = gL.find_record("测试公司Y", "")
check("测试公司Y已建档", qd2 is not None and qd2["fields"].get("投递状态") == "简历评估")
check("queue_detail 建议公司名", api("api/queue_detail?uid=999002").get("_status") == 404)  # 已出队
import atexit as _ax
def _cleanup_y():
    try:
        r = gL.get_by_company("测试公司Y")
        if r:
            for x in gL.exams_of(r["id"]): gL.delete_record(gL.T_EXAM, x["id"])
            gL.delete_record(gL.T_MAIN, r["id"])
    except Exception: pass
_ax.register(_cleanup_y)

print("== D3. 删除志愿（级联考试/存档，2026-10-02） ==")
r = api("api/add_record", {"公司名称": "删除测试公司Z", "岗位名称": "临时岗", "投递状态": "简历评估"})
check("待删志愿已建", r.get("ok") is True and r.get("id"))
rid_z = r["id"]
api("api/add_exam", {"记录": rid_z, "考试类型": "笔试",
                     "开始时间": "2026-11-01T10:00", "结束时间": "2026-11-01T12:00"})
gL.add_archive("删除测试公司Z", datetime.datetime(2026, 10, 2, 9, 0, tzinfo=CST),
               "【删除测试公司Z】感谢信", "感谢您的投递")
check("删除前存档在", len(gL.archives_of("删除测试公司Z")) == 1)
rd = api("api/delete_record", {"id": rid_z})
check("删除志愿成功", rd.get("ok") is True, str(rd))
check("志愿已消失", gL.get_by_company("删除测试公司Z") is None)
check("考试行级联删除", all(x["fields"].get("公司") != rid_z for x in gL.list_records(gL.T_EXAM)))
check("存档级联删除", len(gL.archives_of("删除测试公司Z")) == 0)
check("删除不存在→404", api("api/delete_record", {"id": 99999999}).get("_status") == 404)
def _cleanup_z():
    try:
        r = gL.get_by_company("删除测试公司Z")
        if r:
            for x in gL.exams_of(r["id"]): gL.delete_record(gL.T_EXAM, x["id"])
            gL.delete_record(gL.T_MAIN, r["id"])
    except Exception: pass
_ax.register(_cleanup_z)

print("== E. 邮件管道 + 沙盒 vault ==")
import subprocess
dry = subprocess.run([sys.executable, os.path.join(HERE, "mail.py"), "--sync", "--dry-run"],
                     capture_output=True, text=True, cwd=HERE, timeout=120)
check("管道 dry-run 可运行(或友好失败)", dry.returncode == 0 or "✗" in (dry.stdout + dry.stderr),
      (dry.stderr or dry.stdout)[:100])
sandbox = os.path.join(HERE, "vault-sandbox", "09-秋招投递")
real_vault = os.path.abspath(os.path.join(HERE, "..", "..", "09-秋招投递"))
real_before = sorted(os.listdir(real_vault)) if os.path.isdir(real_vault) else []
os.makedirs(os.path.join(HERE, "vault-sandbox", "09-秋招投递"), exist_ok=True)
real_sync = subprocess.run([sys.executable, os.path.join(HERE, "sync.py")], capture_output=True, text=True,
                           cwd=HERE, timeout=120, env={**os.environ, "SYNC_VAULT_DIR": os.path.join(HERE, "vault-sandbox")})
check("sync.py(沙盒vault)可运行", real_sync.returncode == 0, (real_sync.stderr or "")[:100])
real_after = sorted(os.listdir(real_vault)) if os.path.isdir(real_vault) else []
check("真实 vault 未被触碰", real_before == real_after)
import shutil
if os.path.isdir(os.path.join(HERE, "vault-sandbox")):
    shutil.rmtree(os.path.join(HERE, "vault-sandbox"))

print("== F. 快照 + 备份 ==")
sid, created = gL.snapshot_status()
check("快照写入", sid and (created or not created))
bk = os.path.join(HERE, "backup-test.sqlite")
gL.download_backup(bk)
check("备份文件生成", os.path.exists(bk) and os.path.getsize(bk) > 10000)
os.remove(bk)

print("== G. 自洽校验（单引擎）==")
d1 = api("api/data")
check("行数未异常暴增", len(d1["记录"]) <= PRE_COUNT + 2, f"测试前={PRE_COUNT} 清理后={len(d1['记录'])}（实时系统允许管道新增）")

print("== H. 清理测试数据 ==")
for x in gL.exams_of(rid):
    gL.delete_record(gL.T_EXAM, x["id"])
gL.delete_record(gL.T_MAIN, rid)
check("测试志愿+考试行已清理", gL.get_by_company("测试公司X") is None)


print()
print(f"功能测试: 通过 {OK} 项" + (f"，失败 {len(FAIL)} 项: {FAIL}" if FAIL else "，全部通过 ✅"))
sys.exit(1 if FAIL else 0)
