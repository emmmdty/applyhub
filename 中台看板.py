#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""秋招中台 · 实时看板服务（纯 stdlib，零第三方依赖）

用法：
    python3 中台看板.py [--demo] [--open] [端口]   # 默认端口取 config.json（无则 8790）
    --open   服务就绪后自动打开默认浏览器（双击启动器用；未配置邮箱 → 直达「设置」页）
    --demo   不连 SQLite，用内置样例数据走同一聚合路径（验收/演示用，不触碰真实库）

路由：
    GET  /          返回同目录 看板.html
    GET  /ok        探活，200 "ok"
    GET  /api/data  聚合数据 JSON：
                    {ok, updated, 记录:[...], 考试:[...], 存档数:{公司:条数}, 最近存档:[最新10条]}
                    数据源异常时降级为 {ok:false, error, 记录:[], 考试:[], 存档数:{}, 最近存档:[]}（不 500）
    GET  /api/queue 待确认队列 JSON：{ok, items:[{uid,主题,发件人,收信时间,原因,入队}]}（不含正文）
    GET  /api/queue_detail?uid=…  单条详情：{ok, item:{…, 正文预览≤2000字, 链接[≤8条]}}（点开才返回正文）
    GET  /api/settings   邮箱凭据与 config.json 运行参数（密码不回传，只回掩码提示）
    POST /api/settings       保存邮箱凭据/端口/目录/别名/关键词（密码留空=不修改）
    POST /api/settings_test  用当前凭据试连 IMAP，返回连接是否成功
    POST /api/queue_retry  body {uid}：对该邮件走 mail.process_one 重试，
                    成功→删除队列条目并标记已见 {"ok":true}；失败→保留并更新原因 {"ok":false,error}
    POST /api/queue_drop   body {uid}：忽略该条（移出队列并标记已见，不再自动重试/再次入队）
    POST /api/add_record  body {公司名称*, 岗位名称, 行业, 网申链接, 投递状态}：新增投递记录（志愿）
                    独立键查重：同「公司+岗位」已存在 → 400；公司名称空 → 400
    POST /api/set_status  body {id:记录行id, 值:7态之一}：手动改投递状态
    POST /api/update_record body {id, 岗位名称/行业/网申链接/状态备注/注意事项(可选多个)}：行内编辑志愿
    POST /api/add_exam  body {记录:id, 考试类型:测评|笔试|面试, 开始时间, 结束时间?, 考试链接?}
                    手动添加考试安排（电话邀约不走邮件的场次），走 add_exam 幂等去重
    POST /api/update_exam body {id, 开始时间?/结束时间?/考试链接?}：考试改期/改链接
    POST /api/toggle 两种形态：
                    {考试:id, 值:bool}        → 勾选 Exams.完成（考试安排子表）
                    {公司, 字段, 值:bool}     → 旧版主表完成字段，白名单 {笔试完成, 测评完成, 面试完成}
                    → {"ok":true,"值":bool}；不存在 → {"ok":false,"error":...}

安全约定：只绑定 127.0.0.1（仅本机访问，2026-09-30 起不再绑 0.0.0.0）；
          /api/data 与 /api/queue 只回白名单字段——不含任何邮件正文，存档只含 公司/收信时间/主题。
并发约定：待确认队列/mail_state 的读-改-写一律走 grist_store.cross_lock（与 mail.py 定时任务互斥）+ 原子写。
"""
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "看板.html")
DEFAULT_PORT = 8790

# ---------------- 外观自定义（2026-10-04：logo / 站点名称，纯本地） ----------------
BRANDING_DIR = os.environ.get("BRANDING_DIR") or os.path.join(BASE_DIR, "branding")
BRAND_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "webp": "image/webp", "svg": "image/svg+xml"}
BRAND_MAX = 2 * 1024 * 1024          # 图片解码后上限 2MB
BRAND_NAME_MAX = 30
BRAND_DEFAULT_NAME = "秋招中台"       # 站点名留空时的回落值


def _brand_validate(ext, data):
    """扩展名白名单 + 魔数校验：改名文件冒充图片直接拒绝。不合法抛 ValueError。"""
    if ext not in BRAND_TYPES:
        raise ValueError("仅支持 png / jpg / webp / svg 图片")
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > BRAND_MAX:
        raise ValueError(f"图片超过 {BRAND_MAX // 1024 // 1024}MB 上限，请压缩后再传")
    if ext == "png":
        ok = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in ("jpg", "jpeg"):
        ok = data.startswith(b"\xff\xd8\xff")
    elif ext == "webp":
        ok = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    else:   # svg：文本格式；看板以 <img> 引用（img 上下文不执行脚本），仍做基本内容校验
        ok = "<svg" in data[:4096].decode("utf-8", "ignore").lower()
    if not ok:
        raise ValueError("文件内容与扩展名不符（改名的文件不能当图片上传）")


def _brand_clean_name(s):
    """站点名称：去控制字符、单行、限长；空串 = 恢复默认名。"""
    s = "".join(ch for ch in str(s or "") if ord(ch) >= 32).strip()
    if len(s) > BRAND_NAME_MAX:
        raise ValueError(f"站点名称最长 {BRAND_NAME_MAX} 字")
    return s


def _brand_logo_files():
    """branding/ 目录下当前存在的 logo 文件（任意受支持扩展名）。"""
    if not os.path.isdir(BRANDING_DIR):
        return []
    return [os.path.join(BRANDING_DIR, f) for f in os.listdir(BRANDING_DIR)
            if f.startswith("logo.") and f.rsplit(".", 1)[-1].lower() in BRAND_TYPES]


def _brand_clear_logo():
    """换图 / 恢复默认前清掉旧 logo（换扩展名时不能留孤儿文件）。"""
    for p in _brand_logo_files():
        try:
            os.unlink(p)
        except OSError:
            pass

RECORD_FIELDS = ["公司名称", "行业", "岗位名称", "网申链接", "投递状态", "投递日期",
                 "笔试时间", "笔试结束", "面试时间", "测评截止", "考试链接", "注意事项",
                 "场次安排", "状态备注", "终止原因", "测评完成", "笔试完成", "面试完成"]
DATE_FIELDS = {"投递日期", "笔试时间", "笔试结束", "面试时间", "测评截止"}
STATUSES = ["未投递", "简历评估", "测评中", "笔试中", "面试中", "已通过", "终止"]
TOGGLE_FIELDS = {"笔试完成", "测评完成", "面试完成"}
EXAM_FIELDS = ["考试类型", "开始时间", "结束时间", "考试链接", "完成", "完成时间", "放弃", "来源主题"]
EXAM_DATE_FIELDS = {"开始时间", "结束时间", "完成时间"}

QUEUE_LOCK = threading.Lock()  # 待确认队列文件读写/重试串行化
SYNC_STATE = {"proc": None, "started": None, "finished": None, "ok": None, "输出": ""}

EXAM_TYPES = ("测评", "笔试", "面试", "其他事项")
EDIT_RECORD_FIELDS = ("公司名称", "岗位名称", "行业", "网申链接", "状态备注", "注意事项", "投递日期",
                      "终止原因")
CST = timezone(timedelta(hours=8))  # 东八区（手动输入时间按本地口径归一）

# 公司名称会用作公司笔记文件名（<笔记库>/09-秋招投递/<公司名>.md）——
# 2026-09-30 加固：禁路径分隔符/保留名/控制字符，防 sync.py 崩溃与越目录写
_BAD_NAME_CHARS = set('/\\:*?"<>|')


def _valid_name(s):
    return bool(s) and len(s) <= 100 and s not in (".", "..") \
        and not any(c in _BAD_NAME_CHARS for c in s) and not any(ord(c) < 32 for c in s)


def _mask_hint(pwd):
    """密码提示只回「已配置」，不回显任何真实字符（2026-10-02 安全加固：
    原实现回首尾各 2 字符，5 位授权码暴露 4/5，显著降低暴力破解空间）。"""
    return "已配置" if str(pwd or "").strip() else ""


def _parse_dt(s):
    """datetime-local / ISO 字符串 → CST datetime；空返回 None；非法抛 ValueError。"""
    s = str(s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"时间格式不对: {s!r}（应为 YYYY-MM-DDTHH:MM）")
    return dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)

DEMO_PAYLOAD = None  # --demo 时启动时算好，请求间日期稳定


# ---------------------------------------------------------------- 聚合（真实 / demo 共用）
_PURE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iso_or_none(v):
    """datetime/date → ISO 字符串；字符串原样；其余 → None。"""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat() + "T00:00:00"   # Date 列无时刻，补本地 0 点便于前端解析
    return v if isinstance(v, str) else None


def _cell(key, v, date_fields=None):
    if key in (date_fields or DATE_FIELDS):
        s = iso_or_none(v)
        if isinstance(s, str) and _PURE_DATE.match(s):
            return s + "T00:00:00"   # 兼容纯日期串：测评截止/投递日期 现为 Date 列，回 "YYYY-MM-DD"
        return s
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return iso_or_none(v)  # 兜底：意外类型（如漏网 datetime）也转成 ISO，保证可 JSON 化


def aggregate(mains, mails, exams=None, snaps=None):
    """入参为 grist_store.list_records 的原始形状 [{id, fields}]。
    snaps=None 时读真实快照表；--demo 传合成快照，避免触碰真实 SQLite（2026-09-30 修）。"""

    # 邮件存档/考试安排的「公司」是指向投递记录的 Ref（行 id），需解析回公司名等展示字段
    refinfo = {m["id"]: {"公司": m["fields"].get("公司名称"),
                         "岗位": m["fields"].get("岗位名称") or "",
                         "行业": m["fields"].get("行业") or "",
                         "投递状态": m["fields"].get("投递状态") or ""}
               for m in mains if m.get("fields", {}).get("公司名称")}

    counts, feed, first_mail = {}, [], {}
    for x in mails:
        f = x.get("fields", {})
        comp = f.get("公司")
        rid_ref = comp if isinstance(comp, int) else None
        if isinstance(comp, int) and comp in refinfo:
            comp = refinfo[comp]["公司"]
        comp = str(comp) if comp else "未知"
        counts[comp] = counts.get(comp, 0) + 1
        rt = iso_or_none(f.get("收信时间"))
        if rid_ref is not None and (first_mail.get(rid_ref) is None or (rt or "") < first_mail[rid_ref]):
            first_mail[rid_ref] = rt                  # 每志愿最早一封邮件=响应时间
        feed.append({"公司": comp,
                     "收信时间": rt,
                     "主题": str(f.get("主题") or "")})
    feed.sort(key=lambda x: x["收信时间"] or "", reverse=True)
    records = [{"id": m["id"], "首邮": first_mail.get(m["id"]),
                **{k: _cell(k, m.get("fields", {}).get(k)) for k in RECORD_FIELDS}}
               for m in mains]

    exam_rows = []
    for x in exams or []:
        f = x.get("fields", {})
        info = refinfo.get(f.get("公司"), {}) if isinstance(f.get("公司"), int) else {}
        row = {"id": x["id"], "记录": f.get("公司") if isinstance(f.get("公司"), int) else None,
               "公司": info.get("公司") or "未知", "岗位": info.get("岗位") or "",
               "行业": info.get("行业") or "", "投递状态": info.get("投递状态") or ""}
        row.update({k: _cell(k, f.get(k), EXAM_DATE_FIELDS) for k in EXAM_FIELDS})
        exam_rows.append(row)
    exam_rows.sort(key=lambda x: x["开始时间"] or "", reverse=True)

    if snaps is None:
        try:
            gmod = _grist()
            snaps = sorted(gmod.list_records(gmod.T_SNAP), key=lambda x: str(x.get("fields", {}).get("d") or ""))
        except Exception:
            snaps = []
    snap_fmt = _grist().snap_date_str   # 纯函数（模块导入不连库），demo 也可安全使用

    return {"ok": True,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "记录": records,
            "考试": exam_rows,
            "存档数": counts,
            "最近存档": feed[:10],
            "快照": [{"日期": snap_fmt(x.get("fields", {}).get("d")), 
                      **{k: x.get("fields", {}).get(k) or 0 for k in ("total", "s1", "s2", "s3", "s4", "s5", "s6")}}
                     for x in snaps]}


def _grist():
    """懒加载存储层（不触发连接，仅供真实模式使用）。"""
    sys.path.insert(0, BASE_DIR)
    import grist_store
    return grist_store


def _mail():
    """懒加载邮件管道（读待确认队列 / 重试时用；demo 模式不会走到）。"""
    sys.path.insert(0, BASE_DIR)
    import mail
    return mail


def _vault_dir():
    """笔记库根目录：环境变量 > config.json 笔记库目录 > 默认（脚本上两级）。与 mail.py/sync.py 同源。"""
    v = os.environ.get("SYNC_VAULT_DIR") or _grist().get_config().get("笔记库目录") or ""
    return os.path.abspath(os.path.expanduser(v)) if str(v).strip() else os.path.dirname(os.path.dirname(BASE_DIR))


def fetch_real():
    """连本地 Grist 取三张表（Exams 未建时降级为空列表，主面板仍可用）。"""
    g = _grist()
    mains = g.list_records(g.T_MAIN)   # 投递记录
    mails = g.list_records(g.T_MAIL)   # 邮件存档
    try:
        exams = g.list_records(g.T_EXAM)   # 考试安排
    except Exception:
        exams = []
    return mains, mails, exams


# ---------------------------------------------------------------- demo 样例数据
def _demo():
    """18 条记录（覆盖全部 7 状态、7 行业、2 条已逾期、未来 3 天 3 条安排、2 条卡住）+ 5 封存档。
    日期全部相对“今天”生成；带 调试字段/正文/邮件原文 以验证白名单剥离。"""
    def d(days, h=12, m=0):
        return (datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                + timedelta(days=days))

    rows = [
        # 面试中：今天 19:00 三面（未来 3 天安排 #1）
        {"公司名称": "字节跳动", "行业": "互联网", "岗位名称": "后端开发工程师（电商）",
         "投递状态": "面试中", "投递日期": d(-12, 10), "面试时间": d(0, 19),
         "状态备注": "二面通过，等三面通知", "招聘渠道": "内推", "内推人": "李学长",
         "网申链接": "https://jobs.bytedance.com", "调试字段_不应外泄": True},
        # 笔试中：明天上午（未来 3 天安排 #2）
        {"公司名称": "腾讯", "行业": "互联网", "岗位名称": "客户端开发工程师",
         "投递状态": "笔试中", "投递日期": d(-8, 14), "笔试时间": d(1, 10), "笔试结束": d(1, 11, 30),
         "考试链接": "https://campus.tencent.com/exam/xxxx", "招聘渠道": "官网"},
        # 测评中：后天截止（未来 3 天安排 #3）
        {"公司名称": "宁德时代", "行业": "新能源", "岗位名称": "电池研发工程师",
         "投递状态": "测评中", "投递日期": d(-6, 9), "测评截止": d(2, 23, 59),
         "考试链接": "https://assessment.catl.com/xxxx", "注意事项": "行测+性格测评，约 40 分钟"},
        # 已逾期 3 天：测评截止已过，仍停在测评中
        {"公司名称": "华泰证券", "行业": "金融", "岗位名称": "行业研究员（TMT）",
         "投递状态": "测评中", "投递日期": d(-15, 10), "测评截止": d(-3, 18),
         "状态备注": "测评链接打开报错，待 HR 补发"},
        # 已逾期 2 天：笔试已结束，仍停在笔试中
        {"公司名称": "美的集团", "行业": "制造", "岗位名称": "供应链管理专员",
         "投递状态": "笔试中", "投递日期": d(-20, 15), "笔试时间": d(-2, 10), "笔试结束": d(-2, 12)},
        {"公司名称": "华为", "行业": "通信", "岗位名称": "嵌入式软件开发工程师",
         "投递状态": "已通过", "投递日期": d(-30, 10), "面试时间": d(-10, 14),
         "状态备注": "意向书已发，等三方", "招聘渠道": "官网"},
        {"公司名称": "网易游戏", "行业": "游戏", "岗位名称": "游戏策划（数值向）",
         "投递状态": "已通过", "投递日期": d(-25, 11),
         "招聘渠道": "内推", "内推人": "陈学姐", "状态备注": "offer 沟通中"},
        {"公司名称": "招商银行", "行业": "金融", "岗位名称": "金融科技培训生",
         "投递状态": "终止", "投递日期": d(-18, 9), "终止原因": "面试挂",
         "状态备注": "终面被刷，问基础偏少"},
        {"公司名称": "联合利华", "行业": "快消", "岗位名称": "市场部管培生",
         "投递状态": "终止", "投递日期": d(-22, 13), "终止原因": "简历筛", "招聘渠道": "官网"},
        # 卡住 21 天：简历评估无进展
        {"公司名称": "京东集团", "行业": "互联网", "岗位名称": "数据分析师",
         "投递状态": "简历评估", "投递日期": d(-21, 16), "网申链接": "https://campus.jd.com"},
        # 未投递：内推待投
        {"公司名称": "比亚迪", "行业": "新能源", "岗位名称": "产品经理（车机）",
         "投递状态": "未投递", "场次安排": "内推批次 9 月底截止",
         "招聘渠道": "内推", "内推人": "王工"},
        # 卡住 18 天：简历评估无进展
        {"公司名称": "拼多多", "行业": "互联网", "岗位名称": "服务端开发工程师",
         "投递状态": "简历评估", "投递日期": d(-18, 10), "招聘渠道": "牛客"},
        {"公司名称": "米哈游", "行业": "游戏", "岗位名称": "技术美术 TA",
         "投递状态": "未投递", "注意事项": "需作品集，先补渲染 demo", "场次安排": "计划下周投递"},
        # 笔试中：6 天后（近期安排）
        {"公司名称": "隆基绿能", "行业": "新能源", "岗位名称": "光伏工艺工程师",
         "投递状态": "笔试中", "投递日期": d(-5, 9), "笔试时间": d(6, 14), "笔试结束": d(6, 16),
         "招聘渠道": "官网"},
        # 测评中：4 天后截止（近期安排）
        {"公司名称": "汇川技术", "行业": "制造", "岗位名称": "电机控制算法工程师",
         "投递状态": "测评中", "投递日期": d(-4, 10), "测评截止": d(4, 23, 59),
         "招聘渠道": "内推", "内推人": "赵师兄"},
        # 面试中：5 天后二面（近期安排）
        {"公司名称": "理想汽车", "行业": "新能源", "岗位名称": "智能驾驶软件工程师",
         "投递状态": "面试中", "投递日期": d(-9, 11), "面试时间": d(5, 9, 30),
         "状态备注": "一面通过，二面已约", "招聘渠道": "牛客"},
        {"公司名称": "蔚来", "行业": "新能源", "岗位名称": "座舱软件开发工程师",
         "投递状态": "简历评估", "投递日期": d(-1, 15), "招聘渠道": "官网"},
        # 刚投 2 天（行业字段缺失，验证前端“未知”兜底）
        {"公司名称": "京东方", "岗位名称": "显示算法工程师",
         "投递状态": "测评中", "投递日期": d(-2, 14), "状态备注": "测评邮件未收到，已联系 HR"},
    ]
    mails = [
        {"id": 1, "fields": {"公司": 1, "收信时间": datetime.now() - timedelta(hours=2),
                             "主题": "【字节跳动】2027 届校园招聘—三面邀请函",
                             "正文": "正文是不可信输入，绝不能出现在 /api/data 返回里",
                             "邮件原文": "raw html body —— 同样不能外泄"}},
        {"id": 2, "fields": {"公司": 2, "收信时间": d(-1, 16, 40),
                             "主题": "腾讯 2027 校园招聘在线笔试通知（附考试链接）", "正文": "x" * 500}},
        {"id": 3, "fields": {"公司": 3, "收信时间": d(-2, 10, 5),
                             "主题": "CATL 校园招聘测评提醒：请于截止日前完成", "正文": ""}},
        {"id": 4, "fields": {"公司": 4, "收信时间": d(-3, 9, 30),
                             "主题": "【重要】华泰证券校招在线测评链接（48 小时内有效）", "正文": ""}},
        {"id": 5, "fields": {"公司": 7, "收信时间": d(-5, 18, 12),
                             "主题": "恭喜！您已通过网易游戏 2027 校招最终面试", "正文": ""}},
    ]
    # 考试安排（Exams 子表）：覆盖 类型×时段×完成态；隆基=跨天拆行样例；字节两条=多轮面试独立行
    exams = [
        {"公司": 1, "考试类型": "面试", "开始时间": d(-3, 15), "结束时间": d(-3, 15, 45),
         "考试链接": "https://meeting.example/bd-1", "完成": True, "来源主题": "【字节跳动】一面邀请函"},
        {"公司": 1, "考试类型": "面试", "开始时间": d(0, 19), "结束时间": d(0, 20),
         "考试链接": "https://meeting.example/bd-3", "完成": False, "来源主题": "【字节跳动】三面邀请函"},
        {"公司": 2, "考试类型": "笔试", "开始时间": d(1, 10), "结束时间": d(1, 11, 30),
         "考试链接": "https://campus.tencent.com/exam/xxxx", "完成": False,
         "来源主题": "腾讯 2027 校园招聘在线笔试通知"},
        {"公司": 3, "考试类型": "测评", "开始时间": d(2, 23, 59),
         "考试链接": "https://assessment.catl.com/xxxx", "完成": False,
         "来源主题": "CATL 校园招聘测评提醒"},
        {"公司": 4, "考试类型": "测评", "开始时间": d(-3, 18),
         "考试链接": "https://assessment.htsc.com/xxxx", "完成": False,
         "来源主题": "华泰证券校招在线测评链接"},
        {"公司": 5, "考试类型": "笔试", "开始时间": d(-2, 10), "结束时间": d(-2, 12),
         "考试链接": "https://campus.midea.com/exam", "完成": False, "来源主题": "美的集团在线笔试通知"},
        {"公司": 14, "考试类型": "笔试", "开始时间": d(0, 14), "结束时间": d(2, 16),
         "考试链接": "https://campus.longi.com/exam", "完成": False,
         "来源主题": "隆基绿能笔试（任选 72 小时窗口）"},
        {"公司": 15, "考试类型": "测评", "开始时间": d(4, 23, 59), "完成": False,
         "来源主题": "汇川技术测评邀请"},
        {"公司": 16, "考试类型": "面试", "开始时间": d(5, 9, 30), "结束时间": d(5, 10, 15),
         "完成": False, "来源主题": "【理想汽车】二面邀约"},
        {"公司": 6, "考试类型": "笔试", "开始时间": d(-10, 14), "结束时间": d(-10, 16),
         "完成": True, "来源主题": "华为笔试通知"},
        {"公司": 6, "考试类型": "面试", "开始时间": d(1, 15), "结束时间": d(1, 15, 40),
         "完成": False, "来源主题": "【华为】Offer 沟通面（已通过，状态推断自动完成）"},
        {"公司": 18, "考试类型": "测评", "开始时间": d(-1, 23, 59), "完成": False,
         "来源主题": "京东方测评邀请"},
        {"公司": 18, "考试类型": "其他事项", "开始时间": d(2, 23, 59), "完成": False,
         "来源主题": "京东方：请在截止前选择笔试时间"},
    ]
    exams = [{"id": i + 1, "fields": f} for i, f in enumerate(exams)]
    return [{"id": i + 1, "fields": f} for i, f in enumerate(rows)], mails, exams


def _demo_snaps():
    """合成 7 天漏斗历史（--demo 用，避免读真实 SQLite 快照表，2026-09-30 修）。"""
    out = []
    for i in range(7):
        day = (datetime.now(CST) - timedelta(days=6 - i)).strftime("%Y-%m-%d")
        out.append({"id": i + 1, "fields": {"d": day, "total": 18 + i, "s1": 7 - i % 3,
                                            "s2": 4, "s3": 3, "s4": 2 + i % 2, "s5": 0, "s6": 2}})
    return out


# demo 模式的待确认队列样例（真实模式读 待确认队列.json）
DEMO_QUEUE = {"ok": True, "items": [
    {"uid": 9001, "主题": "【示例科技】2027 届校招技术测评邀请（内推）",
     "收信时间": str(datetime.now().replace(microsecond=0) - timedelta(hours=5)),
     "原因": "公司未匹配", "入队": str(datetime.now().replace(microsecond=0) - timedelta(hours=4))},
    {"uid": 9002, "主题": "转发：某大厂 AI 测评通知，请查收",
     "收信时间": str(datetime.now().replace(microsecond=0) - timedelta(hours=26)),
     "原因": "同公司多记录请指认岗位",
     "入队": str(datetime.now().replace(microsecond=0) - timedelta(hours=25))},
]}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if not self._guard():
            return
        from urllib.parse import unquote
        path = unquote(self.path.split("?", 1)[0])   # unquote：中文文件名路由（如 /说明书.pdf）
        if path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as fp:
                    self._send(200, fp.read(), "text/html; charset=utf-8")
            except OSError as e:
                self._send(500, f"看板.html 读取失败: {e}", "text/plain; charset=utf-8")
        elif path in ("/说明书.pdf", "/help.pdf"):
            fp = os.path.join(BASE_DIR, "使用说明书.pdf")
            try:
                with open(fp, "rb") as fh:
                    self._send(200, fh.read(), "application/pdf")
            except OSError:
                self._send(404, "说明书文件缺失（使用说明书.pdf）", "text/plain; charset=utf-8")
        elif path.startswith("/assets/"):
            name = os.path.basename(path)          # 只取文件名，防目录穿越
            fp = os.path.join(BASE_DIR, "assets", name)
            ctype = ("image/png" if name.endswith(".png")
                     else "image/svg+xml" if name.endswith(".svg")
                     else "image/x-icon" if name.endswith(".ico") else None)
            try:
                with open(fp, "rb") as fh:
                    self._send(200, fh.read(), ctype or "application/octet-stream")
            except OSError:
                self._send(404, "not found", "text/plain; charset=utf-8")
        elif path == "/ok":
            self._send(200, "ok", "text/plain; charset=utf-8")
        elif path == "/api/sync_status":
            self._handle_sync_status()
        elif path == "/api/data":
            if DEMO_PAYLOAD is not None:
                self._json(DEMO_PAYLOAD)
                return
            try:
                self._json(aggregate(*fetch_real()))
            except Exception as e:  # 任何异常都降级 200 + ok:false，前端出横幅而不是 500
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}",
                            "记录": [], "考试": [], "存档数": {}, "最近存档": []})
        elif path == "/api/queue":
            self._handle_queue_get()
        elif path.startswith("/api/queue_detail"):
            self._handle_queue_detail()
        elif path == "/api/settings":
            self._handle_settings_get()
        elif path == "/api/branding/logo":
            self._handle_branding_logo()
        elif path == "/api/branding":
            self._handle_branding_get()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _guard(self):
        """同源防护（2026-10-02）：虽只绑 127.0.0.1，但同机浏览器里的任意网页原可跨站
        调用写接口（CSRF——text/plain 即 simple request 不触发预检，服务端照常执行），
        或经 DNS rebinding 伪装同源读写全部接口。统一校验 Host；POST 再校验 Origin 与
        Content-Type。本机 curl/urllib 不带 Origin 不受影响，双击启动器探活也不受影响。"""
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"):
            self._json({"ok": False,
                        "error": f"拒绝访问：Host 异常（{host or '缺失'}）。请通过 http://127.0.0.1:{port} 访问"}, 403)
            return False
        if self.command == "POST":
            org = (self.headers.get("Origin") or "").strip().rstrip("/").lower()
            if org and org not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self._json({"ok": False, "error": "拒绝访问：跨站请求（Origin 与本机看板不符）"}, 403)
                return False
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.headers.get("Content-Length") and ct != "application/json":
                self._json({"ok": False, "error": "仅接受 application/json 请求体"}, 415)
                return False
        return True

    def _handle_sync_status(self):
        p = SYNC_STATE.get("proc")
        running = p is not None and p.poll() is None
        self._json({"ok": True, "running": running, "started": SYNC_STATE.get("started"),
                    "finished": SYNC_STATE.get("finished"), "ok_last": SYNC_STATE.get("ok"),
                    "输出": SYNC_STATE.get("输出") or ""})

    def _handle_sync_now(self, req):
        """立即拉取邮件（2026-10-02）：后台子进程跑 mail.py --sync（同一 SQLite/队列/状态文件，
        与 2 小时定时链互不冲突）；前端轮询 /api/sync_status 展示进度。"""
        if DEMO_PAYLOAD is not None:
            self._json({"ok": False, "error": "demo 模式没有真实邮箱，无需同步"}, 400)
            return
        p = SYNC_STATE.get("proc")
        if p is not None and p.poll() is None:
            self._json({"ok": False, "error": "已在同步中，请稍候（可看「邮件动态」页）"}, 409)
            return
        import subprocess as _sp
        try:
            proc = _sp.Popen([sys.executable, os.path.join(BASE_DIR, "mail.py"), "--sync"],
                             cwd=BASE_DIR, env=os.environ.copy(),
                             stdout=_sp.PIPE, stderr=_sp.STDOUT)
        except Exception as e:
            self._json({"ok": False, "error": f"启动同步失败: {e}"}, 500)
            return
        SYNC_STATE.update(proc=proc, started=datetime.now(CST).isoformat(timespec="seconds"),
                          finished=None, ok=None, 输出="")

        def _wait():
            try:
                out, _ = proc.communicate(timeout=1800)
            except Exception:
                proc.kill()
                out = b""
            SYNC_STATE.update(finished=datetime.now(CST).isoformat(timespec="seconds"),
                              ok=(proc.returncode == 0),
                              输出=(out or b"").decode("utf-8", "ignore")[-800:])
        threading.Thread(target=_wait, daemon=True).start()
        self._json({"ok": True, "started": True, "提示": "已开始拉取邮件，完成后自动刷新"})

    def _handle_queue_get(self):
        if DEMO_PAYLOAD is not None:
            self._json(DEMO_QUEUE)
            return
        try:
            m = _mail()
            with QUEUE_LOCK:
                q = m.load_queue()
            items = [{"uid": it.get("uid"), "主题": str(it.get("主题") or ""),
                      "发件人": str(it.get("发件人") or ""),   # 2026-09-30：域名常能看出是哪家（如 ibeisen.com）
                      "收信时间": str(it.get("收信时间") or ""),
                      "原因": str(it.get("原因") or ""), "入队": str(it.get("入队") or "")}
                     for it in q.get("items", [])]
            self._json({"ok": True, "items": items})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}", "items": []})

    def _handle_queue_detail(self):
        """单条待确认邮件详情（点开才返回正文预览与链接；列表接口仍不含正文，维持白名单约定）。"""
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "item": {
                "uid": 9001, "主题": "【示例科技】2027 届校招技术测评邀请（内推）",
                "发件人": "hr@example-tech.com", "收信时间": DEMO_QUEUE["items"][0]["收信时间"],
                "原因": "公司未匹配", "入队": DEMO_QUEUE["items"][0]["入队"],
                "正文预览": "（demo 样例正文）请于 3 天内完成在线测评：https://assess.example-tech.com/start?token=abc",
                "链接": ["https://assess.example-tech.com/start?token=abc"]}})
            return
        from urllib.parse import urlparse, parse_qs
        uid = (parse_qs(urlparse(self.path).query).get("uid") or [""])[0]
        try:
            m = _mail()
            with QUEUE_LOCK:
                q = m.load_queue()
            item = next((x for x in q.get("items", []) if str(x.get("uid")) == str(uid)), None)
            if item is None:
                self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
                return
            body = str(item.get("正文") or "")
            links = []
            for mm in re.finditer(r"https?://[^\s\)）\]】》，,;；\"'<>。！？：、》]+", body):
                u = mm.group(0).rstrip(".")
                low = u.lower()
                if u not in links and not any(x in low for x in ("unsubscribe", "beacon", "track", ".png", ".jpg", "mail.163.")):
                    links.append(u)
            gname, _conf = _mail().extract_company_guess(str(item.get("主题") or ""),
                                                         str(item.get("发件人") or ""),
                                                         str(item.get("发件人名") or ""))
            self._json({"ok": True, "item": {
                "uid": item.get("uid"), "主题": str(item.get("主题") or ""),
                "发件人": str(item.get("发件人") or ""), "收信时间": str(item.get("收信时间") or ""),
                "原因": str(item.get("原因") or ""), "入队": str(item.get("入队") or ""),
                "建议公司名": gname or "",
                "正文预览": body[:2000], "链接": links[:8]}})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_queue_retry(self, req):
        uid = req.get("uid")
        if DEMO_PAYLOAD is not None:   # demo：内存里删掉即算成功（重启还原）
            n0 = len(DEMO_QUEUE["items"])
            DEMO_QUEUE["items"] = [x for x in DEMO_QUEUE["items"] if str(x.get("uid")) != str(uid)]
            if len(DEMO_QUEUE["items"]) < n0:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
            return
        try:
            m = _mail()
            g = _grist()
            ok = reason = None
            # QUEUE_LOCK=线程互斥；cross_lock=与 mail.py 定时任务跨进程互斥（2026-09-30 并发加固）
            with QUEUE_LOCK, g.cross_lock(m.QUEUE_PATH, timeout=30, stale=1800):
                q = m.load_queue()
                item = next((x for x in q["items"] if str(x.get("uid")) == str(uid)), None)
                if item is None:
                    self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
                    return
                mm = {"uid": item.get("uid"), "date": m.qdate(item.get("收信时间")),
                      "from": item.get("发件人", ""), "from_display": item.get("发件人名", ""),
                      "subject": item.get("主题", ""),
                      "body": item.get("正文", "")}
                mains = m.load_main()
                companies, excludes = m.companies_excludes(mains)
                ok, reason = m.process_one(mm, mains, companies, excludes, False)
                if ok:
                    # 顺序敏感（2026-10-01 修）：先标记已见、再移出队列——若先删后标，
                    # 中途失败会让邮件「队列里没了、已见里也没有」，下轮同步复活，表现为"忽略无效"
                    self._mark_seen(m, mm["uid"])
                    q["items"] = [x for x in q["items"] if str(x.get("uid")) != str(uid)]   # 按 uid：同 uid 残留一并清除
                    m.save_queue(q)
                    self._audit("retry_ok", mm["uid"], m["subject"][:40] if isinstance(m, dict) else "")
                else:
                    item["原因"] = reason
                    m.save_queue(q)
                    self._audit("retry_fail", mm["uid"], str(reason)[:60])
            if ok:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": reason or "处理失败"})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_queue_drop(self, req):
        """忽略队列条目：移出待确认队列并标记已见，防止下轮自动重试/再次入队（2026-09-25）。"""
        uid = req.get("uid")
        if DEMO_PAYLOAD is not None:   # demo：内存里删掉即算成功（重启还原）
            n0 = len(DEMO_QUEUE["items"])
            DEMO_QUEUE["items"] = [x for x in DEMO_QUEUE["items"] if str(x.get("uid")) != str(uid)]
            if len(DEMO_QUEUE["items"]) < n0:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
            return
        try:
            m = _mail()
            g = _grist()
            with QUEUE_LOCK, g.cross_lock(m.QUEUE_PATH, timeout=30, stale=1800):
                q = m.load_queue()
                item = next((x for x in q["items"] if str(x.get("uid")) == str(uid)), None)
                if item is None:
                    self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
                    return
                q["items"] = [x for x in q["items"] if str(x.get("uid")) != str(uid)]
                self._mark_seen(m, item.get("uid"))   # 先标已见再落盘（同 retry，2026-10-01 修）
                m.save_queue(q)
                self._audit("drop", item.get("uid"), str(item.get("主题") or "")[:40])
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    @staticmethod
    def _audit(action, uid, extra=""):
        """队列变更加审计日志（追溯"条目去哪了"：忽略/重试/丢失排查用）。"""
        try:
            import datetime as _dt
            line = f"{_dt.datetime.now():%F %T} {action} uid={uid} pid={os.getpid()} {extra}\n"
            with open(os.path.join(BASE_DIR, "logs", "queue_audit.log"), "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            pass

    @staticmethod
    def _mark_seen(m, uid):
        """重试成功后把 uid 写进 mail_state.json 的已见集合（须已在 cross_lock 内调用）。
        状态文件不存在时跳过：补写会丢 highest_uid，令下轮同步全量重抓。"""
        import os as _os
        if not _os.path.exists(m.STATE_PATH):
            return
        st = m._load_state()
        uids = set(st.get("uids", []))
        if uid is not None:
            uids.add(uid)
        st["uids"] = sorted(uids)
        m._save_state(st)   # 原子写（2026-09-30：原裸 json.dump 可被定时任务写坏/覆盖）

    def do_POST(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        routes = {"/api/toggle", "/api/queue_retry", "/api/queue_drop", "/api/queue_create", "/api/add_record",
                  "/api/set_status", "/api/update_record", "/api/add_exam", "/api/update_exam", "/api/delete_exam",
                  "/api/delete_record", "/api/sync_now",
                  "/api/settings", "/api/settings_test",
                  "/api/branding", "/api/branding/reset"}
        if path not in routes:
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # 上限 1MB 防超大包入库；logo 上传走 base64（2MB 图 ≈ 2.7MB 文本）放宽到 4MB（2026-10-04）
            cap = 4_000_000 if path == "/api/branding" else 1_000_000
            if n > cap:
                self._json({"ok": False, "error": f"请求体过大（上限 {cap // 1_000_000}MB）"}, 413)
                return
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
        except (ValueError, UnicodeDecodeError) as e:
            self._json({"ok": False, "error": f"请求体解析失败: {e}"}, 400)
            return
        handlers = {"/api/queue_retry": self._handle_queue_retry,
                    "/api/queue_drop": self._handle_queue_drop,
                    "/api/queue_create": self._handle_queue_create,
                    "/api/add_record": self._handle_add_record,
                    "/api/set_status": self._handle_set_status,
                    "/api/update_record": self._handle_update_record,
                    "/api/add_exam": self._handle_add_exam,
                    "/api/update_exam": self._handle_update_exam,
                    "/api/delete_exam": self._handle_delete_exam,
                    "/api/delete_record": self._handle_delete_record,
                    "/api/sync_now": self._handle_sync_now,
                    "/api/settings": self._handle_settings_post,
                    "/api/settings_test": self._handle_settings_test,
                    "/api/branding": self._handle_branding_post,
                    "/api/branding/reset": self._handle_branding_reset}
        if path == "/api/toggle":
            self._do_toggle(req)
        else:
            handlers[path](req)

    # ---------------- 设置（邮箱凭据 + 运行参数，网页可视化编辑，2026-09-30） ----------------
    def _handle_queue_create(self, req):
        """横幅条目一键新建志愿（2026-10-01）：用户确认公司名 → 建档 → 按正常管道处理该邮件。
        公司名称可由前端预填建议值后编辑；同公司空岗位已有志愿则直接挂靠不重复建。"""
        uid = req.get("uid")
        if DEMO_PAYLOAD is not None:
            n0 = len(DEMO_QUEUE["items"])
            DEMO_QUEUE["items"] = [x for x in DEMO_QUEUE["items"] if str(x.get("uid")) != str(uid)]
            self._json({"ok": len(DEMO_QUEUE["items"]) < n0, "id": None})
            return
        try:
            m = _mail()
            g = _grist()
            with QUEUE_LOCK, g.cross_lock(m.QUEUE_PATH, timeout=30, stale=1800):
                q = m.load_queue()
                item = next((x for x in q["items"] if str(x.get("uid")) == str(uid)), None)
                if item is None:
                    self._json({"ok": False, "error": f"队列中无此 uid: {uid!r}"}, 404)
                    return
                name = str(req.get("公司名称") or "").strip()
                if not name:
                    name = m.extract_company_guess(str(item.get("主题") or ""), str(item.get("发件人") or ""),
                                                   str(item.get("发件人名") or ""))[0] or ""
                if not name:
                    self._json({"ok": False, "error": "无法识别公司名，请手动填写公司名称"}, 400)
                    return
                if not _valid_name(name):
                    self._json({"ok": False, "error": "公司名称含非法字符（禁止 / \\ : * ? \" < > | 及控制字符，最长 100 字）"}, 400)
                    return
                exist = g.find_record(name, "") or g.get_active_record(name)
                if exist:
                    rid = exist["id"]   # 公司已有志愿 → 直接挂靠，不另建空岗位行（避免多志愿歧义）
                else:
                    recv = m.qdate(item.get("收信时间"))
                    rec = g.add_record(g.T_MAIN, {
                        "公司名称": name, "投递状态": "简历评估",
                        "投递日期": f"{recv:%Y-%m-%d}",
                        "状态备注": "（横幅确认创建：来自邮件）"})
                    rid = rec["id"]
                mm = {"uid": item.get("uid"), "date": m.qdate(item.get("收信时间")),
                      "from": item.get("发件人", ""), "from_display": item.get("发件人名", ""),
                      "subject": item.get("主题", ""), "body": item.get("正文", "")}
                mains = m.load_main()
                companies, excludes = m.companies_excludes(mains)
                ok, reason = m.process_one(mm, mains, companies, excludes, False)
                if ok:
                    self._mark_seen(m, mm["uid"])   # 先标已见再移出（同 retry，2026-10-01 修）
                    q["items"] = [x for x in q["items"] if str(x.get("uid")) != str(uid)]
                    m.save_queue(q)
                    self._audit("create", mm["uid"], f"{name} · {str(item.get('主题') or '')[:40]}")
                else:
                    item["原因"] = reason
                    m.save_queue(q)
                    self._audit("create_fail", mm["uid"], str(reason)[:60])
            if ok:
                self._json({"ok": True, "公司名称": name})
            else:
                self._json({"ok": False, "error": reason or "处理失败"})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_settings_get(self):
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "demo": True, "mail_host": "imap.163.com", "mail_user": "demo@example.com",
                        "has_password": True, "密码提示": "已配置", "已配置": True,
                        "配置": {"端口": DEFAULT_PORT, "笔记库目录": "", "备份目录": "", "公司别名": {}, "关键词": []},
                        "路径": {"数据库": "(demo 内存样例)", "脚本目录": BASE_DIR}})
            return
        try:
            m, g = _mail(), _grist()
            creds = m.load_creds()
            pwd = str(creds.get("mail_password") or "")
            self._json({"ok": True,
                        "mail_host": creds.get("mail_host", "imap.163.com"),
                        "mail_user": creds.get("mail_user", ""),
                        "has_password": bool(pwd),
                        "密码提示": _mask_hint(pwd),
                        "已配置": bool(creds.get("mail_user") and pwd),
                        "配置": g.get_config(),
                        "路径": {"数据库": g.SQLITE_PATH, "脚本目录": BASE_DIR}})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_settings_post(self, req):
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "demo": True, "提示": "demo 模式不落盘"})
            return
        try:
            m, g = _mail(), _grist()
            cred_upd = {}
            for k in ("mail_host", "mail_user"):
                if k in req:
                    v = str(req.get(k) or "").strip()
                    if not v:
                        self._json({"ok": False, "error": "IMAP 服务器 / 邮箱账号不能为空"}, 400)
                        return
                    cred_upd[k] = v
            if "mail_password" in req:
                v = str(req.get("mail_password") or "").strip()
                if v:
                    cred_upd["mail_password"] = v      # 留空 = 不修改
            if cred_upd:
                m.save_creds(cred_upd)
            cfg = dict(g.get_config())
            if "端口" in req:
                p = int(req.get("端口"))
                if not (1024 <= p <= 65535):
                    raise ValueError("端口需在 1024-65535 之间")
                cfg["端口"] = p
            for k in ("笔记库目录", "备份目录"):
                if k in req:
                    cfg[k] = str(req.get(k) or "").strip()
            if "自动建志愿" in req:
                cfg["自动建志愿"] = bool(req.get("自动建志愿"))
            if "公司别名" in req:
                a = req.get("公司别名")
                if not isinstance(a, dict):
                    raise ValueError("公司别名需为对象 {称呼: 主表公司名}")
                cfg["公司别名"] = {str(k).strip(): str(v).strip()
                                   for k, v in a.items() if str(k).strip() and str(v).strip()}
            if "关键词" in req:
                w = req.get("关键词")
                if not isinstance(w, list):
                    raise ValueError("关键词需为字符串数组")
                cfg["关键词"] = [str(x).strip() for x in w if str(x).strip()]
            g.save_config(cfg)
            self._json({"ok": True, "提示": "已保存。邮箱配置下次同步生效；端口重启看板后生效"})
        except (ValueError, TypeError) as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_settings_test(self, req):
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "提示": "demo 模式：模拟连接成功"})
            return
        try:
            m = _mail()
            M = m.imap_connect()   # 未配置会抛 RuntimeError，落到 except
            try:
                M.logout()
            except Exception:
                pass
            self._json({"ok": True, "提示": "连接成功，授权码有效"})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    # ---------------- 外观（自定义 logo / 站点名称，2026-10-04） ----------------
    def _handle_branding_get(self):
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "demo": True, "站点名称": BRAND_DEFAULT_NAME, "自定义名": False,
                        "logo": "", "更新时间": 0, "上限": BRAND_MAX})
            return
        try:
            cfg = _grist().get_config()
            logo = str(cfg.get("品牌Logo") or "")
            p = os.path.join(BRANDING_DIR, logo) if logo else ""
            if logo and not os.path.isfile(p):
                logo = ""   # 文件被手动删了 → 界面回落默认，顺手修复配置
                cfg["品牌Logo"], cfg["品牌Logo时间"] = "", 0
                _grist().save_config(cfg)
            self._json({"ok": True,
                        "站点名称": str(cfg.get("品牌站点名") or "") or BRAND_DEFAULT_NAME,
                        "自定义名": bool(str(cfg.get("品牌站点名") or "").strip()),
                        "logo": logo, "更新时间": int(cfg.get("品牌Logo时间") or 0),
                        "上限": BRAND_MAX})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_branding_logo(self):
        """自定义 logo 文件下发（<img> / favicon 引用）。无自定义 → 404，前端回落默认资源。"""
        if DEMO_PAYLOAD is None:
            try:
                logo = str(_grist().get_config().get("品牌Logo") or "")
            except Exception:
                logo = ""
            p = os.path.join(BRANDING_DIR, logo) if logo else ""
            if logo and os.path.isfile(p):
                try:
                    with open(p, "rb") as fh:
                        data = fh.read()
                    ext = logo.rsplit(".", 1)[-1].lower()
                    self.send_response(200)
                    self.send_header("Content-Type", BRAND_TYPES.get(ext, "application/octet-stream"))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError as e:
                    self._send(500, f"logo 读取失败: {e}", "text/plain; charset=utf-8")
                return
        self._send(404, "no custom logo", "text/plain; charset=utf-8")

    def _handle_branding_post(self, req):
        """上传 logo（base64）与/或保存站点名称。只传名称=仅改名；恢复默认走 /api/branding/reset。"""
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "demo": True, "提示": "demo 模式不落盘"})
            return
        try:
            g = _grist()
            cfg = g.get_config()
            # 站点名：只在请求带了「站点名称」键时才改（2026-10-04 E2E 修：
            # 原来无条件覆盖，先改名再传 logo 会把已存的站名清空）
            name = _brand_clean_name(req.get("站点名称")) if "站点名称" in req else None
            data_b64 = str(req.get("数据") or "").strip()
            if data_b64.startswith("data:"):          # 容忍 data URL 形式
                data_b64 = data_b64.split(",", 1)[-1]
            if data_b64:
                fname = str(req.get("文件名") or "")
                ext = os.path.splitext(fname)[1].lower().lstrip(".")
                try:
                    data = base64.b64decode(data_b64, validate=True)
                except Exception:
                    raise ValueError("图片数据不是合法的 base64")
                _brand_validate(ext, data)
                os.makedirs(BRANDING_DIR, exist_ok=True)
                _brand_clear_logo()
                target = os.path.join(BRANDING_DIR, f"logo.{ext}")
                fd, tmp = tempfile.mkstemp(dir=BRANDING_DIR, prefix=".logo-")
                try:
                    with os.fdopen(fd, "wb") as fp:
                        fp.write(data)
                    os.replace(tmp, target)           # 原子替换：中途失败不留半个文件
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                cfg["品牌Logo"] = f"logo.{ext}"
                cfg["品牌Logo时间"] = int(time.time())
            if name is not None:
                cfg["品牌站点名"] = name
            g.save_config(cfg)
            self._json({"ok": True,
                        "站点名称": str(cfg.get("品牌站点名") or "") or BRAND_DEFAULT_NAME,
                        "自定义名": bool(str(cfg.get("品牌站点名") or "").strip()),
                        "logo": str(cfg.get("品牌Logo") or ""),
                        "更新时间": int(cfg.get("品牌Logo时间") or 0), "提示": "已保存，界面即时生效"})
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_branding_reset(self, req):
        """恢复默认外观：删自定义 logo 文件 + 清空站点名。"""
        if DEMO_PAYLOAD is not None:
            self._json({"ok": True, "demo": True, "提示": "demo 模式不落盘"})
            return
        try:
            _brand_clear_logo()
            g = _grist()
            cfg = g.get_config()
            cfg["品牌Logo"], cfg["品牌Logo时间"], cfg["品牌站点名"] = "", 0, ""
            g.save_config(cfg)
            self._json({"ok": True, "站点名称": BRAND_DEFAULT_NAME, "自定义名": False,
                        "logo": "", "提示": "已恢复默认外观"})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_update_record(self, req):
        """行内编辑志愿字段（白名单，传了才改，空串=清空）。"""
        rid = req.get("id")
        updates = {k: str(req[k]).strip() if k == "公司名称" else str(req[k])
                   for k in EDIT_RECORD_FIELDS if k in req}
        if "公司名称" in updates:
            if not updates["公司名称"]:
                self._json({"ok": False, "error": "公司名称不能为空"}, 400)
                return
            if not _valid_name(updates["公司名称"]):
                self._json({"ok": False, "error": "公司名称含非法字符（禁止 / \\ : * ? \" < > | 及控制字符，最长 100 字）"}, 400)
                return
        if not updates:
            self._json({"ok": False, "error": f"没有可更新字段（白名单: {list(EDIT_RECORD_FIELDS)}）"}, 400)
            return
        cur = None
        if DEMO_PAYLOAD is None and ("公司名称" in updates or "岗位名称" in updates):
            try:
                g0 = _grist()
                cur = g0.list_records(g0.T_MAIN, filter={"id": [req.get("id")]})
                if cur:
                    cf = cur[0]["fields"]
                    new_co = updates.get("公司名称", str(cf.get("公司名称") or "")).strip()
                    new_po = str(updates.get("岗位名称", cf.get("岗位名称") or "")).strip()
                    clash = [r for r in g0.list_records(g0.T_MAIN)
                             if r["id"] != req.get("id")
                             and str(r["fields"].get("公司名称") or "").strip() == new_co
                             and str(r["fields"].get("岗位名称") or "").strip() == new_po]
                    if clash:
                        self._json({"ok": False, "error": f"独立键冲突：{new_co}·{new_po or '（无岗位）'} 已存在"}, 400)
                        return
            except Exception:
                pass
        if DEMO_PAYLOAD is not None:
            row = next((x for x in DEMO_PAYLOAD["记录"] if x.get("id") == rid), None)
            if row is None:
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            row.update(updates)
            self._json({"ok": True})
            return
        try:
            g = _grist()
            if not g.list_records(g.T_MAIN, filter={"id": [rid]}):
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            g.update_record(g.T_MAIN, rid, updates)
            old_co = str(cur[0]["fields"].get("公司名称") or "") if cur else ""
            if "公司名称" in updates and old_co and old_co != updates["公司名称"]:
                try:   # 公司笔记文件跟随改名（路径与 mail.py/sync.py 同源：env > config > 默认上两级）
                    import os as _os
                    vp = _vault_dir()
                    sub = str(g.get_config().get("笔记子目录") or "09-秋招投递")
                    old_p, new_p = os.path.join(vp, sub, old_co + ".md"), \
                                   os.path.join(vp, sub, updates["公司名称"] + ".md")
                    if _os.path.exists(old_p) and not _os.path.exists(new_p):
                        _os.rename(old_p, new_p)
                except Exception:
                    pass
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_delete_record(self, req):
        """删除志愿（2026-10-02）：连同名下考试安排与邮件存档级联删除（不可恢复，前端有确认框）。
        修复：公司名建错的记录此前无删除入口，只能改「终止」，永远留在列表与漏斗里。"""
        rid = req.get("id")
        if DEMO_PAYLOAD is not None:
            n0 = len(DEMO_PAYLOAD["记录"])
            DEMO_PAYLOAD["记录"] = [x for x in DEMO_PAYLOAD["记录"] if x.get("id") != rid]
            DEMO_PAYLOAD["考试"] = [x for x in DEMO_PAYLOAD["考试"] if x.get("记录") != rid]
            if len(DEMO_PAYLOAD["记录"]) < n0:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
            return
        try:
            g = _grist()
            if not g.list_records(g.T_MAIN, filter={"id": [rid]}):
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            exams = g.list_records(g.T_EXAM, filter={"公司": [rid]})
            mails = g.list_records(g.T_MAIL, filter={"公司": [rid]})
            for x in exams:
                g.delete_record(g.T_EXAM, x["id"])
            for x in mails:
                g.delete_record(g.T_MAIL, x["id"])
            g.delete_record(g.T_MAIN, rid)
            self._json({"ok": True, "删除考试": len(exams), "删除存档": len(mails)})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_add_exam(self, req):
        """手动添加考试安排（电话邀约等不走邮件的场次），走 add_exam 幂等。"""
        rid = req.get("记录")
        typ = str(req.get("考试类型") or "")
        if typ not in EXAM_TYPES:
            self._json({"ok": False, "error": f"非法考试类型: {typ!r}（可选: {'、'.join(EXAM_TYPES)}）"}, 400)
            return
        try:
            start = _parse_dt(req.get("开始时间"))
            end = _parse_dt(req.get("结束时间"))
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return
        if start is None:
            self._json({"ok": False, "error": "开始时间必填"}, 400)
            return
        if end is not None and end <= start:
            self._json({"ok": False, "error": "结束时间需晚于开始时间"}, 400)
            return
        link = str(req.get("考试链接") or "").strip()
        if DEMO_PAYLOAD is not None:
            rec = next((x for x in DEMO_PAYLOAD["记录"] if x.get("id") == rid), None)
            if rec is None:
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            if any(x.get("记录") == rid and x.get("考试类型") == typ and (x.get("开始时间") or "") == start.isoformat()
                   for x in DEMO_PAYLOAD["考试"]):
                self._json({"ok": False, "error": "同志愿已有同类型同开始的考试，未重复添加"}, 400)
                return
            nid = max((x.get("id") or 0 for x in DEMO_PAYLOAD["考试"]), default=0) + 1
            DEMO_PAYLOAD["考试"].append({
                "id": nid, "记录": rid, "公司": rec.get("公司名称") or "未知",
                "岗位": rec.get("岗位名称") or "", "行业": rec.get("行业") or "",
                "投递状态": rec.get("投递状态") or "", "考试类型": typ,
                "开始时间": start.isoformat(), "结束时间": end.isoformat() if end else None,
                "考试链接": link, "完成": False, "来源主题": "手动添加"})
            self._json({"ok": True, "id": nid})
            return
        try:
            g = _grist()
            if not g.list_records(g.T_MAIN, filter={"id": [rid]}):
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            _, created = g.add_exam(rid, typ, start, 结束=end, 链接=link,
                                    来源主题="手动添加", 按链去重=False)
            if not created:
                self._json({"ok": False, "error": "同志愿已有同类型同开始的考试，未重复添加"}, 400)
                return
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_update_exam(self, req):
        """考试改期/改类型/改链接（白名单，传了才改）。"""
        eid = req.get("id")
        updates = {}
        try:
            if "开始时间" in req:
                s = _parse_dt(req.get("开始时间"))
                if s is None:
                    self._json({"ok": False, "error": "开始时间不能清空"}, 400)
                    return
                updates["开始时间"] = s
            if "结束时间" in req:
                updates["结束时间"] = _parse_dt(req.get("结束时间"))
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return
        if "放弃" in req:
            updates["放弃"] = bool(req.get("放弃"))
            if updates["放弃"]: updates["完成"] = False   # 放弃与完成互斥
        if "完成" in req:
            updates["完成"] = bool(req.get("完成"))
            if updates["完成"]: updates["放弃"] = False
        if "考试类型" in req:
            t = str(req.get("考试类型") or "")
            if t not in EXAM_TYPES:
                self._json({"ok": False, "error": f"非法考试类型: {t!r}（可选: {'、'.join(EXAM_TYPES)}）"}, 400)
                return
            updates["考试类型"] = t
        if "考试链接" in req:
            updates["考试链接"] = str(req.get("考试链接") or "").strip()
        if not updates:
            self._json({"ok": False, "error": "没有可更新字段（白名单: 开始时间/结束时间/考试链接）"}, 400)
            return
        if updates.get("开始时间") and updates.get("结束时间") and updates["结束时间"] <= updates["开始时间"]:
            self._json({"ok": False, "error": "结束时间需晚于开始时间"}, 400)
            return
        if DEMO_PAYLOAD is not None:
            row = next((x for x in DEMO_PAYLOAD["考试"] if x.get("id") == eid), None)
            if row is None:
                self._json({"ok": False, "error": f"考试不存在: {eid!r}"}, 404)
                return
            for k, v in updates.items():
                row[k] = v.isoformat() if isinstance(v, datetime) else v
            self._json({"ok": True})
            return
        try:
            g = _grist()
            if not g.list_records(g.T_EXAM, filter={"id": [eid]}):
                self._json({"ok": False, "error": f"考试不存在: {eid!r}"}, 404)
                return
            # clear_none=True：显式传「结束时间: null/空」= 清空该字段（2026-09-30 修：此前无法清空）
            g.update_record(g.T_EXAM, eid, updates, clear_none=True)
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_delete_exam(self, req):
        """删除考试安排行（误建/重复行，前端 ✎ 编辑器入口）。"""
        eid = req.get("id")
        if DEMO_PAYLOAD is not None:
            n0 = len(DEMO_PAYLOAD["考试"])
            DEMO_PAYLOAD["考试"] = [x for x in DEMO_PAYLOAD["考试"] if x.get("id") != eid]
            if len(DEMO_PAYLOAD["考试"]) < n0:
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": f"考试不存在: {eid!r}"}, 404)
            return
        try:
            g = _grist()
            if not g.list_records(g.T_EXAM, filter={"id": [eid]}):
                self._json({"ok": False, "error": f"考试不存在: {eid!r}"}, 404)
                return
            g.delete_record(g.T_EXAM, eid)
            self._json({"ok": True})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_add_record(self, req):
        """网页新增投递记录（志愿）。独立键=公司+岗位，重复即拒绝。"""
        name = str(req.get("公司名称") or "").strip()
        if not name:
            self._json({"ok": False, "error": "公司名称必填"}, 400)
            return
        if not _valid_name(name):
            self._json({"ok": False, "error": "公司名称含非法字符（禁止 / \\ : * ? \" < > | 及控制字符，最长 100 字）"}, 400)
            return
        pos = str(req.get("岗位名称") or "").strip()
        status = str(req.get("投递状态") or "简历评估")  # 新增投递默认=简历评估
        if status not in STATUSES:
            self._json({"ok": False, "error": f"非法状态: {status!r}（可选: {'、'.join(STATUSES)}）"}, 400)
            return
        fields = {"公司名称": name, "投递状态": status}
        for k in ("岗位名称", "行业", "网申链接"):
            v = str(req.get(k) or "").strip()
            if v: fields[k] = v
        dd = str(req.get("投递日期") or "").strip()      # 不传=默认当天（2026-09-20）
        fields["投递日期"] = dd[:10] if dd else datetime.now().astimezone().strftime("%Y-%m-%d")
        if DEMO_PAYLOAD is not None:
            if any(r.get("公司名称") == name and str(r.get("岗位名称") or "") == pos
                   for r in DEMO_PAYLOAD["记录"]):
                self._json({"ok": False, "error": f"该公司+岗位已存在: {name}·{pos or '（无岗位）'}"}, 400)
                return
            nid = max((r.get("id") or 0 for r in DEMO_PAYLOAD["记录"]), default=0) + 1
            rec = {"id": nid, **fields}
            DEMO_PAYLOAD["记录"].append(rec)
            self._json({"ok": True, "id": nid})
            return
        try:
            g = _grist()
            if g.find_record(name, pos) is not None:
                self._json({"ok": False, "error": f"该公司+岗位已存在: {name}·{pos or '（无岗位）'}"}, 400)
                return
            rec = g.add_record(g.T_MAIN, fields)
            self._json({"ok": True, "id": rec.get("id")})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_set_status(self, req):
        """手动改投递状态（按记录行 id，独立键友好）。"""
        rid, value = req.get("id"), req.get("值")
        if value not in STATUSES:
            self._json({"ok": False, "error": f"非法状态: {value!r}（可选: {'、'.join(STATUSES)}）"}, 400)
            return
        if DEMO_PAYLOAD is not None:
            for r in DEMO_PAYLOAD["记录"]:
                if r.get("id") == rid:
                    r["投递状态"] = value
                    self._json({"ok": True, "值": value})
                    return
            self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
            return
        try:
            g = _grist()
            rows = g.list_records(g.T_MAIN, filter={"id": [rid]})
            if not rows:
                self._json({"ok": False, "error": f"记录不存在: {rid!r}"}, 404)
                return
            g.update_record(g.T_MAIN, rid, {"投递状态": value})
            self._json({"ok": True, "值": value})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _do_toggle(self, req):
        # ---- /api/toggle ----
        exam_id, value = req.get("考试"), req.get("值")
        company, field = req.get("公司"), req.get("字段")
        if exam_id is not None:
            self._toggle_exam(exam_id, bool(value))
            return
        if field not in TOGGLE_FIELDS:
            self._json({"ok": False, "error": f"非法字段: {field!r}（白名单: {sorted(TOGGLE_FIELDS)} 或传 考试:id）"}, 400)
            return
        value = bool(value)
        if not isinstance(company, str) or not company:
            self._json({"ok": False, "error": "缺少公司名"}, 400)
            return
        if DEMO_PAYLOAD is not None:   # demo：改内存样例（同一返回形状，重启后还原）
            for rec in DEMO_PAYLOAD["记录"]:
                if rec.get("公司名称") == company:
                    rec[field] = value
                    self._json({"ok": True, "值": value})
                    return
            self._json({"ok": False, "error": f"公司不存在: {company}"}, 404)
            return
        try:
            g = _grist()
            row = g.get_by_company(company)
            if not row:
                self._json({"ok": False, "error": f"公司不存在: {company}"}, 404)
                return
            g.update_record(g.T_MAIN, row["id"], {field: value})
            self._json({"ok": True, "值": value})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _toggle_exam(self, exam_id, value):
        """勾选 Exams.完成（考试安排子表行）；勾选时记 完成时间，取消时清空。"""
        if DEMO_PAYLOAD is not None:
            for x in DEMO_PAYLOAD.get("考试", []):
                if x.get("id") == exam_id:
                    x["完成"] = value
                    x["完成时间"] = datetime.now(CST).isoformat(timespec="seconds") if value else None
                    self._json({"ok": True, "值": value})
                    return
            self._json({"ok": False, "error": f"考试不存在: {exam_id!r}"}, 404)
            return
        try:
            g = _grist()
            rows = g.list_records(g.T_EXAM, filter={"id": [exam_id]})
            if not rows:
                self._json({"ok": False, "error": f"考试不存在: {exam_id!r}"}, 404)
                return
            upd = {"完成": value, "完成时间": datetime.now(CST) if value else None}
            if value: upd["放弃"] = False        # 完成与放弃互斥（2026-09-24）
            g.update_record(g.T_EXAM, exam_id, upd, clear_none=True)
            if value:   # 勾选联动：同志愿同类型、已过截止的兄弟行（重复提醒建出的行）一并完成
                row = rows[0]["fields"]
                now = datetime.now(CST)
                for x in g.list_records(g.T_EXAM, filter={"公司": [row.get("公司")]}):
                    # 字段名是中文（list_records 返回 {id,fields:{完成,放弃,...}}）——
                    # 2026-09-27 修：原写 done/waived 恒为 None，已完成/已放弃的兄弟行不会被跳过，
                    # 完成时间会被改写复活到"今天"（携程 id34 案例）
                    if x["id"] == exam_id or x["fields"].get("完成") or x["fields"].get("放弃") \
                            or str(x["fields"].get("考试类型") or "") != str(row.get("考试类型")):
                        continue
                    ref = x["fields"].get("结束时间") or x["fields"].get("开始时间")
                    if ref is not None and ref < now:
                        g.update_record(g.T_EXAM, x["id"],
                                        {"完成": True, "完成时间": now, "放弃": False}, clear_none=True)
            self._json({"ok": True, "值": value})
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{datetime.now():%H:%M:%S}] {fmt % args}\n")


def _needs_setup():
    """首次使用检测：无凭据.json 或未填账号/授权码 → --open 时直达设置页。"""
    try:
        with open(os.path.join(BASE_DIR, "凭据.json"), encoding="utf-8") as fp:
            c = json.load(fp)
        return not (c.get("mail_user") and c.get("mail_password"))
    except Exception:
        return True


def main():
    global DEMO_PAYLOAD
    argv = sys.argv[1:]
    demo = "--demo" in argv
    port, explicit = 0, False
    for a in argv:
        if a in ("--demo", "--open"):
            continue
        try:
            port = int(a)
            explicit = True
        except ValueError:
            pass
    if not port:
        if demo:
            port = 8799   # demo 默认换端口：8790 是生产看板位，--demo 曾撞端口甩英文 traceback（2026-10-02）
        else:
            try:
                port = int(_grist().get_config().get("端口") or DEFAULT_PORT)
            except Exception:
                port = DEFAULT_PORT
    if demo:
        DEMO_PAYLOAD = aggregate(*_demo(), snaps=_demo_snaps())
        n_mail = sum(DEMO_PAYLOAD["存档数"].values())
        print(f"[demo] 样例数据：{len(DEMO_PAYLOAD['记录'])} 条记录 / {len(DEMO_PAYLOAD['考试'])} 场考试 / "
              f"{n_mail} 封存档 / 队列 {len(DEMO_QUEUE['items'])} 条（未连接 SQLite）", flush=True)
    # 只绑 127.0.0.1：看板含邮件元数据与全部写接口，不向局域网暴露（2026-09-30 起）
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        # 端口占用等启动失败给中文指引（2026-10-02 修：原来甩英文 traceback，双击用户无从下手）
        print(f"✗ 看板启动失败（{type(e).__name__}: {e}）")
        print(f"  多半是端口 {port} 被占用，按顺序试：")
        print(f"  ① 若看板其实已在运行：直接打开 http://127.0.0.1:{port} 即可")
        print(f"  ② 若被其他程序占用：用记事本打开本目录 config.json，把 \"端口\": {port} 改成别的数字（如 8791）再双击启动")
        sys.exit(1)
    import grist_store as _gprint
    if os.environ.get("SQLITE_PATH"):
        print(f"[沙盒模式] 数据库: {_gprint.SQLITE_PATH}", flush=True)
    print(f"秋招中台看板 → http://127.0.0.1:{port} （仅本机访问；改端口：设置页或 config.json）  Ctrl+C 退出", flush=True)
    if "--open" in argv:   # 双击启动器：就绪后自动开浏览器（2026-09-30，配合 启动中台.bat/.sh）
        import webbrowser
        url = f"http://127.0.0.1:{port}/" + ("#settings" if _needs_setup() else "")
        _t = threading.Timer(1.2, lambda: webbrowser.open(url))
        _t.daemon = True
        _t.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
