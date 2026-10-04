# -*- coding: utf-8 -*-
"""IMAP 邮箱 -> 本地 SQLite 中台 全自动同步 v3（规则抽取，零人工确认）
支持任意 IMAP 服务商：163/QQ/腾讯企业邮实测可用（登录前发 ID 指令，兼容 QQ 要求）；
Gmail 需应用专用密码且网络可达；Outlook 个人版已禁用基础认证（不可用）。
用法:
  python3 mail.py [N]           读最近 N 封邮件标题（默认15），只读老用法
  python3 mail.py --sync [--dry-run] [--all]
                                收新邮件 -> 规则抽取 -> 投递记录只填空更新
                                -> 考试安排 Exams 新增考试行（新邮件=新考试行）
                                -> 邮件存档无条件存档 -> 公司笔记空时间线行
                                公司未匹配/多志愿歧义/写库失败的邮件进本地 待确认队列.json，下轮自动重试
  python3 mail.py --merge [--dry-run]
                                仅重试 待确认队列（旧命令名兼容保留）
红线：主表只填空、永不覆盖；已通过/终止为吸收态，后续任何邮件不得复活状态；--dry-run 只看不写。
凭据.json 由看板「设置」页可视化维护（缺失时本模块优雅降级，未配置即报友好错误）；
config.json 提供端口/笔记库目录/公司别名/关键词等个性化配置；
队列与 mail_state 的读写均走 grist_store 原子写 + 跨进程文件锁（与看板并发安全）。
存储层一律走 grist_store.py，零第三方依赖。"""
import json, os, sys, re, time, email, imaplib, difflib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import datetime

import grist_store

BASE = os.path.dirname(os.path.abspath(__file__))
# 路径可用环境变量覆盖（沙盒/测试与生产隔离；生产不设这些变量，行为不变）
CRED_PATH = os.environ.get("MAIL_CRED_PATH") or os.path.join(BASE, "凭据.json")
STATE_PATH = os.environ.get("MAIL_STATE_PATH") or os.path.join(BASE, "mail_state.json")
QUEUE_PATH = os.environ.get("MAIL_QUEUE_PATH") or os.path.join(BASE, "待确认队列.json")

# ---------------- 凭据（凭据.json；网页「设置」页可视化编辑，缺失时优雅降级） ----------------
def load_creds():
    """读凭据（缺失/损坏返回 {}，新用户未配置也能启动看板/测试）。"""
    try:
        with open(CRED_PATH, encoding="utf-8") as _fp:
            c = json.load(_fp)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}

def save_creds(upd):
    """合并保存凭据（原子写 + 刷新模块级 USER/PWD，保存后可立即测试连接）。"""
    global cred, USER, PWD
    c = load_creds()
    for k, v in (upd or {}).items():
        if v is not None:
            c[k] = v
    grist_store.atomic_write_json(CRED_PATH, c)
    cred, USER, PWD = c, c.get("mail_user", ""), c.get("mail_password", "")

def mail_configured():
    return bool(USER and PWD)

cred = load_creds()
USER, PWD = cred.get("mail_user", ""), cred.get("mail_password", "")

def _vault_dir():
    """笔记库根目录：环境变量 > config.json 笔记库目录 > 默认（脚本上两级，Obsidian 库根）。"""
    v = os.environ.get("SYNC_VAULT_DIR") or grist_store.get_config().get("笔记库目录") or ""
    return os.path.abspath(os.path.expanduser(v)) if str(v).strip() else os.path.dirname(os.path.dirname(BASE))

VAULT = _vault_dir()   # 并行期可用环境变量指向沙盒，避免触碰真实库
NOTES_SUBDIR = str(grist_store.get_config().get("笔记子目录") or "09-秋招投递")
OUT = os.path.join(VAULT, NOTES_SUBDIR)

_DEFAULT_KEYWORDS = ["笔试", "面试", "测评", "测试", "offer", "Offer", "OFFER", "录取", "感谢信", "简历", "评估", "考试", "邀约", "问卷", "登记表", "邀请函", "反馈", "宣讲会",
                     "assessment", "interview", "hiring", "application", "written test", "online test"]   # 英文外企邮件（2026-10-02：原纯中文关键词致英文邮件被静默跳过）
KEYWORDS = list(grist_store.get_config().get("关键词") or []) or list(_DEFAULT_KEYWORDS)


def subject_hit(subj):
    """主题是否命中解析关键词（大小写不敏感，中文关键词不受影响，兼容英文邮件）。"""
    s = str(subj or "").casefold()
    return any(k.casefold() in s for k in KEYWORDS)

CST = datetime.timezone(datetime.timedelta(hours=8))
CANON = ("未投递", "简历评估", "测评", "笔试", "面试中", "已通过", "终止")
DONE = "已完成"

# ---------------- HTTP ----------------
def txt(v):
    return "".join(x.get("text", "") for x in v if isinstance(x, dict)) if isinstance(v, list) else (v or "")

# ---------------- IMAP / 邮件解析 ----------------
def imap_connect(select="INBOX"):
    if not mail_configured():
        raise RuntimeError("邮箱未配置：请打开看板「设置」页填写 IMAP 服务器 / 账号 / 授权码")
    # ID 指令允许在登录前发（NONAUTH 态）：QQ 邮箱要求登录前发 ID，163/Gmail 兼容（2026-10-01）
    imaplib.Commands["ID"] = ("AUTH", "NONAUTH")
    M = imaplib.IMAP4_SSL(cred.get("mail_host", "imap.163.com"), 993, timeout=15)   # 带超时：防不可达服务器挂死整条同步链
    try:
        M._simple_command("ID", '("name" "qiuzhao-sync" "version" "2.0")')
    except Exception:
        pass   # 极少数服务器不支持 ID，忽略即可
    M.login(USER, PWD)
    M.select(select, readonly=True)
    return M

def dec(v):
    if not v: return ""
    out = ""
    for s, enc in decode_header(v):
        out += s.decode(enc or "utf-8", errors="ignore") if isinstance(s, bytes) else s
    return re.sub(r"\s+", " ", out).strip()

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"): self.skip += 1
        if tag in ("br", "p", "div", "tr", "td", "li"): self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip: self.skip -= 1
    def handle_data(self, d):
        if not self.skip: self.parts.append(d)

def html_to_text(s):
    p = _Stripper(); p.feed(s or "")
    t = "".join(p.parts)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()

def body_text(msg):
    plain, html, hrefs = None, None, []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        raw = part.get_payload(decode=True)
        if raw is None: continue
        cs = part.get_content_charset() or "utf-8"
        s = raw.decode(cs, errors="ignore")
        if part.get_content_subtype() == "html":
            html = html or s
            pairs = re.findall(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', s, re.I | re.S)
            have = {u.replace("&amp;", "&") for u, _ in pairs}
            loose = [u for u in re.findall(r'href="(http[^"]+)"', s, re.I)
                     if u.replace("&amp;", "&") not in have]
            hrefs += pairs + [(u, "") for u in loose]
        elif part.get_content_subtype() == "plain":
            s2 = s.strip()
            # 坏邮件防御（2026-09-23，4399案例）：text/plain 部件实为字面量“text/html”、
            # 真正文全在 HTML 部件 → 过短或形如内容类型字符串的 plain 弃用，落到 HTML
            if plain is None and len(s2) >= 20 and not re.fullmatch(r"[a-z]+/[a-z0-9.+-]+", s2, re.I):
                plain = s
    t = plain or (html_to_text(html) if html else "")
    if hrefs:  # HTML 锚文本里的链接不能丢：追加到尾部（去重，保留锚文本供上下文评分）
        extra = []
        for u, at in hrefs:
            u = u.replace("&amp;", "&").strip()
            at = re.sub(r"<[^>]+>", "", at or "")
            at = re.sub(r"\s+", " ", at).strip()[:30]
            if u.startswith("http") and u not in t and all(u not in x for x in extra):
                extra.append((at + " " + u).strip() if at else u)
        if extra: t = t + "\n" + "\n".join(extra[:20])
    return t.strip()

def fetch_mails(all_=False):
    M = imap_connect()
    try:
        _, allu = M.uid("search", None, "ALL")
        uids = [int(x) for x in allu[0].split()]
        first = not os.path.exists(STATE_PATH)
        st = {} if first else _load_state()   # 容错读（2026-10-02 修：原裸 json.load，状态文件损坏会让每轮同步永久卡死）
        last = st.get("highest_uid", 0)
        seen = set(st.get("uids", []))
        new_uids = uids if all_ else [u for u in uids if u > last]
        if first and not all_:
            new_uids = new_uids[-30:]
        elif not all_:
            # 自愈回看（2026-09-23）：最近 60 个 uid 里“已见集合没有”的也重抓。
            # 修复：首处理失败+队列丢失的邮件曾因 uid<highest_uid 被永久跳过（蓝色光标案例）。
            # 重复处理安全：存档按 主题+收信分钟 去重、add_exam 幂等、主表只填空。
            lookback = {u for u in uids[-60:] if u not in seen}
            new_uids = sorted(set(new_uids) | lookback)
        mails = []
        for u in new_uids:
            _, data = M.uid("fetch", str(u), "(RFC822)")
            if not data or not data[0]: continue
            msg = email.message_from_bytes(data[0][1])
            subj = dec(msg.get("Subject", ""))
            raw_from = dec(msg.get("From", ""))
            m = re.search(r"<(.+?)>", raw_from)
            frm = m.group(1) if m else raw_from
            disp = re.sub(r"\s*<.*$", "", raw_from).strip().strip('"“”')   # 发件人显示名（自动建志愿用）
            try: dt = parsedate_to_datetime(msg.get("Date", ""))
            except Exception: dt = datetime.datetime.now(CST)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=CST)
            mails.append({"uid": u, "date": dt.astimezone(CST), "from": frm, "from_display": disp,
                          "subject": subj, "body": body_text(msg)[:8000]})
        return mails, (max(uids) if uids else last)
    finally:
        M.logout()

# ---------------- 规则抽取 ----------------
def extract_type(subject, body):
    s = subject or ""
    if re.search(r"AI面|AI测评|AI测试", s): return "测评"   # AI面试→测评类型（不推进状态）
    if re.search(r"笔试\s*[（(]?\s*测评|测评\s*[（(]?\s*笔试", s): return "测评"  # 「笔试（测评）」实为测评（九方智投，用户定版 2026-09-21）
    if re.search(r"技术测评", s): return "笔试"             # 技术测评→笔试类型（用户定版）
    if re.search(r"问卷|登记表", s): return "问卷"          # 问卷/登记表→要填写的事务（用户定版 2026-09-23）
    if re.search(r"感谢[你您]?(?:投递|应聘)|简历已收到|流程指引|应聘反馈|笔试反馈|面试反馈", s): return "通知"  # 纯通知：只存档不建行不推状态（恒生案例 2026-09-23）
    if re.search(r"笔试|测评|评测|测试|在线考试|考试邀请|考试通知|面试邀约|面试邀请|面试通知", s):
        if re.search(r"面试邀约|面试邀请|面试通知|一面|二面", s): return "面试"
        if re.search(r"笔试|在线考试|考试邀请|考试通知", s): return "笔试"
        return "测评"
    # 拒信判定必须先于 Offer（2026-10-02 修）：「不予录用/未能被录用/录用评估未通过」含「录用」
    # 子串，曾被下一行先命中判成 Offer → 推进吸收态「已通过」，把拒信当 Offer 庆祝且不可逆
    if re.search(r"感谢信|很遗憾|遗憾|未能通过|不予录用|抱歉", s): return "感谢信"
    if re.search(r"offer|录用|录取|welcome aboard", s, re.I): return "Offer"
    if re.search(r"宣讲会|招聘会|双选会", s): return "其他"   # 宣讲会/招聘会活动≠考试（科大讯飞案例 2026-09-21）
    if re.search(r"面试", s): return "面试"
    # 「免笔试/免测评」是否定语境，从正文分类文本剔除，防止误判成笔试（2026-09-21）
    b = re.sub(r"免\s*(?:线上)?(?:笔试|测评|机考)[^，。；\n]{0,6}", "", body or "")[:600]
    if re.search(r"在线考试|笔试", b): return "笔试"
    if re.search(r"测评", b): return "测评"
    if re.search(r"面试", b): return "面试"
    return "其他"

STATUS_ORDER = {"未投递": 0, "简历评估": 1, "测评中": 2, "笔试中": 3, "面试中": 4, "已通过": 5, "终止": 6}
def plan_status(mtype, wmode, cur):
    """状态推进（用户规则 2026-09-19 定版）：笔试邮件→笔试中；面试→面试中；Offer→已通过；
    感谢信→终止；测评（含 AI面试/技术测评场次邀约）一律不动状态；
    只进不退；已通过/终止为吸收态——终止后的群发笔试/面试邮件与 Offer 邮件都不得复活/翻转。"""
    cur = cur or "未投递"
    if cur in ("已通过", "终止"):  # 吸收态：终态只进不出
        return None
    if mtype == "Offer":
        return "已通过" if cur != "已通过" else None
    if mtype == "感谢信":
        return "终止" if cur not in ("已通过", "终止") else None
    if mtype == "笔试":
        return "笔试中" if STATUS_ORDER.get(cur, 0) < STATUS_ORDER["笔试中"] else None
    if mtype == "面试":
        return "面试中" if STATUS_ORDER.get(cur, 0) < STATUS_ORDER["面试中"] else None
    return None  # 测评/其他：不推进

DATE_PATS = [
    re.compile(r"(?P<y>\d{4})\s*[-/\.年]\s*(?P<m>\d{1,2})\s*[-/\.月]\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?<![\d年])(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"),
    # 数字区间排除前瞻（2026-10-02 修）："3-5天内/12-15K/9-10月/5-6万人"是数量不是日期，
    # 曾被解析成 3月5日/12月15日/9月10日，命中截止语境时写出几个月后的幽灵考试行。
    # 注意：不排除「日」——"3-5日"仍按日期区间理解。
    re.compile(r"(?<!第)(?<![\d./-])(?P<m>\d{1,2})\s*[-/.](?P<d>\d{1,2})(?![\d./-])"
               r"(?!\s*(?:部分|分钟|秒|个?\s*(?:工作日|[天月])|万|人|元|[Kk次名年]))"),
]
TIME_PAT = re.compile(r"(?P<h>\d{1,2})\s*(?:[:：]\s*(?P<mi>\d{2})|点\s*(?P<mi2>\d{1,2})?\s*分?)")
DEADLINE_NEAR = re.compile(r"截止|之前|以前|前完成|前点击|前登录|前选择|前预约|失效|过期|deadline|完成测评|完成考试", re.I)
SESSION_NEAR = re.compile(r"考试时间|测评时间|面试时间|场次|时间[:：]|开始|举行|进行")

def find_deadline(text, today):
    """测评用：只认截止语境（类别0）的日期。返回 (date, time_str, 原文, 类别)。"""
    cands = _date_cands(text, today)
    cands = [c for c in cands if c[0] == 0]
    if not cands: return None, None, None, 2
    cands.sort(key=lambda x: (x[0], x[1]))
    cls, _, d, tstr, raw = cands[0]
    return d, tstr, raw, cls

def plan_window(text, recv, default_dur=120):
    """通用考试窗口规划：返回 (模式, 开始dt, 结束dt或None, 依据, 结束是否邮件明示时刻)。
    模式优先级：生效失效对 > 固定场次 > 时间窗 > 场次+时长 > 截止日 > 相对时限 > 无信息"""
    today = recv.date()
    cs = _date_cands(text, today)
    # 0) 生效/失效 成对窗口（SHL 等测评商标准句式：于A生效，于B失效，2026-09-21 补）
    starts = [c for c in cs if "生效" in text[c[1] + len(c[4]): c[1] + len(c[4]) + 14]]   # 间隔可能含「周一 15:34」
    ends = [c for c in cs if "失效" in text[c[1] + len(c[4]): c[1] + len(c[4]) + 14]]
    if starts and ends:
        s = min(starts, key=lambda x: x[1])
        e = max(ends, key=lambda x: x[1])
        if e[2] > s[2] or (e[2] == s[2] and (pt_time(e[3]) if e[3] else (23, 59)) > (pt_time(s[3]) if s[3] else (0, 0))):
            t1 = pt_time(s[3]) if s[3] else (0, 0)
            t2 = pt_time(e[3]) if e[3] else (23, 59)
            return ("时间窗", datetime.datetime(s[2].year, s[2].month, s[2].day, *t1, tzinfo=CST),
                    datetime.datetime(e[2].year, e[2].month, e[2].day, *t2, tzinfo=CST),
                    s[4] + "生效~" + e[4] + "失效", True)
    # 1) 固定场次：日期紧跟 HH:MM-HH:MM
    for c in sorted(cs, key=lambda x: (0 if x[0] == 2 else 1, x[1])):
        if not c[3] or c[0] == 0: continue
        after = text[c[1]:c[1] + 100]
        rm = re.search(r"(\d{1,2}:\d{2})\s*[-—~至到]\s*(\d{1,2}:\d{2})", after)
        if not rm: continue
        h, m_ = pt_time(c[3])
        h2, m2 = pt_time(rm.group(2))
        if pt_time(rm.group(1)) != (h, m_) or not (0 <= h2 <= 23 and 0 <= m2 <= 59):
            continue   # 起止时刻非法（如 25:30）→ 放弃该候选（2026-09-30 防御）
        s = datetime.datetime(c[2].year, c[2].month, c[2].day, h, m_, tzinfo=CST)
        e = s.replace(hour=h2, minute=m2)
        if e > s: return ("固定场次", s, e, c[4] + " " + rm.group(0), True)
    # 2) 明示时间窗：两个日期相距<70字符且中间有连接符
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            a, b = cs[i], cs[j]
            if b[1] - a[1] > 70 or not re.search(r"[-—~～至到]", text[a[1]:b[1]]): continue
            t1 = pt_time(a[3]) if a[3] else (0, 0)
            t2 = pt_time(b[3]) if b[3] else (23, 59)
            s = datetime.datetime(a[2].year, a[2].month, a[2].day, *t1, tzinfo=CST)
            e = datetime.datetime(b[2].year, b[2].month, b[2].day, *t2, tzinfo=CST)
            if e > s:
                return ("时间窗", s, e, text[a[1]:b[1] + len(b[4])][:55], bool(b[3]))
    # 3) 场次+时长：日期带时刻，无区间结束
    sess = [c for c in cs if c[3] and c[0] in (1, 2)]
    if sess:
        c = sorted(sess, key=lambda x: (0 if x[0] == 2 else 1, x[1]))[0]
        h, m_ = pt_time(c[3])
        s = datetime.datetime(c[2].year, c[2].month, c[2].day, h, m_, tzinfo=CST)
        dm = re.search(r"(\d{1,3})\s*分钟", text)
        dur = int(dm.group(1)) if dm and 30 <= int(dm.group(1)) <= 240 else default_dur
        return ("场次+时长", s, s + datetime.timedelta(minutes=dur), c[4], False)
    # 4) 截止日：[收信, 截止时刻]
    dl = [c for c in cs if c[0] == 0]
    if dl:
        c = sorted(dl, key=lambda x: x[1])[0]
        t2 = pt_time(c[3]) if c[3] else (23, 59)
        e = datetime.datetime(c[2].year, c[2].month, c[2].day, *t2, tzinfo=CST)
        return ("截止日", recv, e, c[4], bool(c[3]))
    # 5) 相对时限：统一走 parse_relative_window（共性抽取，2026-09-20）
    rel = parse_relative_window(text)
    if rel:
        return ("相对时限", recv, recv + rel[1], rel[0], False)
    return ("无信息", recv, None, "-", False)

def pt_time(s):
    h, m = s.split(":")
    return int(h), int(m)

def _date_cands(text, today):
    cands = []
    for pat in DATE_PATS:
        for m in pat.finditer(text):
            mo, da = int(m.group("m")), int(m.group("d"))
            if not (1 <= mo <= 12 and 1 <= da <= 31): continue
            has_y = bool(pat.groupindex.get("y") and m.groupdict().get("y"))
            y = int(m.group("y")) if has_y else today.year
            try: d = datetime.date(y, mo, da)
            except ValueError: continue
            # 过去日期顺延一年仅限“邮件未写年份”的场合；显式年份（如“2026年09月21日”）绝不顺延
            # （反例：提醒邮件 09-22 收到，生效日 09-21 已过 → 曾被错顺延成 2027，2026-09-23 修）
            if d < today and not has_y: d = datetime.date(y + 1, mo, da)
            tm = TIME_PAT.search(text, m.end(), min(m.end() + 14, len(text)))
            tstr = None
            if tm:
                hh, mm = int(tm.group("h")), int(tm.group("mi") or tm.group("mi2") or 0)
                # 畸形时刻防御（2026-09-30）：如「编号25:30」会把 hour=25 带进 datetime 直接崩全链
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    tstr = f"{hh:02d}:{mm:02d}"
            win = text[max(0, m.start() - 12): min(m.end() + 14, len(text))]
            if re.search(r"免责|声明|此邮件|系统邮件|退订|©|Copyright", win, re.I): cls = 3
            elif DEADLINE_NEAR.search(win): cls = 0
            elif SESSION_NEAR.search(win): cls = 2
            else: cls = 1
            cands.append((cls, m.start(), d, tstr, m.group(0)))
    return cands

URL_EXC = ("mail.163.", "unsubscribe", "beacon", "track", ".png", ".jpg")
# 上下文加分：URL 前后紧邻「xx链接/点击进入/开始作答」等 → 真实考试入口（常为短链）胜过带参数的杂链
URL_CTX_PAT = re.compile(
    r"(?:测评|考试|笔试|面试|作答)(?:链接|地址)|链接\s*[:：]|点击[^。；\n]{0,12}(?:进入|开始|参加|链接|访问)"
    r"|开始[^。；\n]{0,6}(?:测评|作答|考试|笔试|面试)|请[^。；\n]{0,10}(?:进入|访问|点击)"
    r"|(?:进入|前往|访问)[^。；\n]{0,10}(?:测评|考试|笔试|面试|页面|链接)", re.I)

def _url_feature_score(low):
    s = 0
    if re.search(r"exam|test|assess|ceping|invit|interview|examid|token|paper|start", low): s += 3
    if re.search(r"login|cand|elink|signup|register", low): s += 2
    if "?" in low or "=" in low: s += 2
    if re.search(r"notice|help|faq|about|index|home", low): s -= 2
    if re.search(r"campus|career|recruit|zhaopin|xuan|hr-", low) and "?" not in low and len(low) < 60: s -= 2
    # 确认/拒绝/状态页不是考试入口（Moka attendStatus、reject-reason 等，2026-09-20 东财案例）
    if re.search(r"reject|attend-?status|exam-status|/cancel|unsubscribe", low): s -= 5
    return s

def _url_excluded(u, exclude):
    """网申链接排除：按「协议+域名+路径」精确匹配（忽略 query/大小写/尾部斜杠）。
    修复：原实现用子串匹配，考试链接与网申链接同域即被误杀（如网申 campus.tencent.com
    会连带排除 campus.tencent.com/exam/xxx，导致考试链接丢失，2026-09-30 修）。"""
    def norm(x):
        x = str(x or "").lower().rstrip("/")
        return x.split("?", 1)[0].split("#", 1)[0]
    un = norm(u)
    return any(un and un == norm(e) for e in (exclude or []) if e)


def extract_url(body, exclude):
    """抽取考试/面试链接：全量收集候选并评分排序。
    评分 = URL 字符串特征 + 上下文分（URL 前后 60/40 字符命中 URL_CTX_PAT +6；
    2026-09-20 起：短链真实入口靠上下文胜出，带 token 的状态/杂链不再靠参数躺赢）。
    最佳分>0 取最佳，否则取第一个 0 分兜底；无候选返回空串。"""
    body = body or ""
    best, order = {}, []
    # 排除类含中文标点（2026-10-02 修）：原会抓到尾部带「。！？」的链接，点开 404 且破坏按链去重
    for m in re.finditer(r"https?://[^\s\)）\]】》，,;；\"'<>。！？：、》！]+", body):
        u = m.group(0).rstrip(".")
        low = u.lower()
        if u in best or any(x in low for x in URL_EXC): continue
        if _url_excluded(u, exclude): continue
        s = _url_feature_score(low)
        if URL_CTX_PAT.search(body[max(0, m.start() - 60): m.end() + 40]): s += 6
        best[u] = s; order.append(u)
    if not best: return ""
    top = max(best.values())
    if top > 0: return max(order, key=best.get)
    zero = [u for u in order if best[u] == 0]
    return zero[0] if zero else ""  # 全都不带特征时，取第一个未被排除的链接兜底

def _cut(s, n):
    if len(s) <= n: return s
    seg = s[:n]
    for i in range(len(seg) - 1, max(0, n - 30), -1):
        if seg[i] in "；。，;,":
            return seg[:i + 1]
    return seg

NOTE_KEYS = re.compile(r"摄像头|监控|时长|分钟|仅?一次|机会|不可回退|不能回退|可回退|密码|身份证|顺延|迟到|作弊|双机|第二设备|手机支架|浏览器|插件|提前|账号|考前")
# 事务型邮件：让你“选择/预约”笔试/面试/测评的时间（而非考试本身）→ 归「其他事项」。
# 2026-09-20 收紧：必须带“请”或直接“预约/确认+考试名词”，防止吉利类样板话术误伤
#（“您可以选择一个较为充裕的时间开始测评”是提示语，不是预约请求）。
SCHED_SEL_PAT = re.compile(
    r"请[^。；\n]{0,10}(?:选择|预约|预定|确认)[^。；\n]{0,12}(?:时间|时段|场次)"
    r"|(?:预约|预定|确认)\s*(?:笔试|面试|测评|考核|考试)"
    r"|(?:笔试|面试|测评|考核|考试|场次)[^。；\n]{0,8}(?:预约|预定)"
    r"|反馈[^。；\n]{0,14}(?:笔试|面试|测评|场次)"
    r"|(?:笔试|面试|测评|考核|考试|场次)[^。；\n]{0,8}(?:预约|预定)"
    r"|反馈[^。；\n]{0,14}(?:笔试|面试|测评|场次)"
    r"|(?:选择|预约)[^。；\n]{0,4}(?:场次|时段)")
# 事务判定否定特征（同族误伤汇总，2026-09-20）：这些命中的是「自助作答指引/考试界面提示」，不是预约事务
# ①「选择…的时间进行/开始/完成笔试测评」（阿里提醒/吉利样板）②「确认题目的作答时间/设备/环境」（vivo）
# ③「确认是否参加」RSVP（东财，已从正则移除）
SCHED_NEG_PAT = re.compile(
    r"作答时间|设备|环境|屏幕|任意时间|较为充裕|合适的时间"
    r"|(?:时间|时段|场次)[^。；\n]{0,10}(?:开始|进行|完成|作答|进入|登录)")

def is_sched_mail(subject, body):
    """事务型(选/约时间)邮件判定：SCHED_SEL_PAT 命中 且 不含否定特征。"""
    text = (subject or "") + "\n" + (body or "")
    m = SCHED_SEL_PAT.search(text)
    if not m: return False
    return not SCHED_NEG_PAT.search(text[max(0, m.start() - 20): m.end() + 20])
# 改期类邮件（原地更新已有考试行，不再新建）：主题或正文前 200 字命中即可
RESCHED_PAT = re.compile(r"改期|改至|延期|时间调整|调整至|改到|变更[^。；\n]{0,6}(?:时间|安排|场次)")

def extract_notes(body):
    out = []
    for ln in (body or "").splitlines():
        ln = ln.strip()
        if 8 < len(ln) < 120 and NOTE_KEYS.search(ln) and ln not in out:
            out.append(ln)
    return _cut("；".join(out), 400)

def extract_sessions(body):
    out = []
    for ln in (body or "").splitlines():
        ln = ln.strip()
        if 6 < len(ln) < 100 and re.search(r"场次|每周|周[一二三四五六日天]|顺延|预约|场次安排", ln) and ln not in out:
            out.append(ln)
    return _cut("；".join(out), 250)

# ---------------- 公司名启发式提取（未匹配邮件自动建志愿用，2026-10-01） ----------------
# 平台/服务商域名：这些域名是招聘系统或邮箱服务商，不是公司本身 → 不可作为公司名
_PLATFORM_DOMAINS = ("mokahr", "beisen", "zhaopin", "51job", "liepin", "shixiseng", "nowcoder",
                     "bosszhipin", "kanzhun", "dajie", "maimai", "yingjiesheng", "gaoxiaobang",
                     "163.com", "126.com", "qq.com", "foxmail", "gmail.com", "sina", "sohu",
                     "outlook", "hotmail", "sendcloud", "aliyun", "exmail", "wecom", "shmail",
                     "saashr", "jobmd", "offercome", "beenger", "huaweicloud", "dingtalk")
_COMPANY_STOPWORDS = re.compile(
    r"通知|邀请|提醒|招聘|校招|校园|人才|人力|中心|系统|平台|官方|内推|网申|感谢信|结果|反馈"
    r"|届|秋招|春招|实习|offer|简历|笔试|面试|测评|考试|no.?reply|noreply|mailer|daemon|postmaster"
    r"|大学|学院|学校|中学|小学|研究院|研究所"   # 学校不是志愿公司（2026-10-02：「【清华大学】笔试」曾建出「清华大学」志愿）
    r"|notice|notification|newsletter|recruiting|recruitment", re.I)
# 直辖市/主要城市名：发件人显示名剥后缀后剩下的地名不建志愿（2026-10-02：「深圳招聘」曾建出「深圳」）
_CITY_NAMES = {"北京", "上海", "天津", "重庆", "广州", "深圳", "杭州", "南京", "成都", "武汉",
               "西安", "苏州", "长沙", "郑州", "厦门", "福州", "合肥", "济南", "青岛", "大连",
               "宁波", "无锡", "佛山", "东莞", "昆明", "沈阳", "哈尔滨", "石家庄", "南昌", "贵阳",
               "太原", "南宁", "兰州", "珠海", "常州", "嘉兴", "绍兴", "中国"}
# 平台产品名（发件人显示名/主题括号里出现 → 是招聘系统不是公司）
_PLATFORM_TOKENS = ("moka", "北森", "牛客", "实习僧", "智联", "前程无忧", "猎聘", "boss直聘",
                    "e成", "saashr", "用友", "金现代", "中智", "外企德科", "fesco")


def _platform_hit(word):
    w = word.lower()
    return any(p in w for p in _PLATFORM_DOMAINS) or any(t.lower() in w for t in _PLATFORM_TOKENS)


def extract_company_guess(subject, from_addr="", from_display=""):
    """从邮件启发式提取公司名（用于自动创建志愿）。返回 (公司名, 置信度) 或 (None, 0)。
    优先级：① 主题【】『』「」[] 括号（最可靠）② 发件人显示名（去招聘类后缀）③ 发件人域名主体（排除平台白名单）。
    置信度仅供日志参考；能提取出来即建，提取不出才进待确认队列。"""
    def _ok(name):
        n = (name or "").strip().strip('"“”')
        if not (2 <= len(n) <= 20):
            return False
        if n in _CITY_NAMES:
            return False
        if _COMPANY_STOPWORDS.search(n):
            return False
        if re.fullmatch(r"[\d\s.·\-_/]+", n):
            return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", n))

    s = subject or ""
    for m in re.finditer(r"【([^【】]{2,20})】|『([^『』]{2,20})』|「([^「」]{2,20})」|\[([^\[\]]{2,20})\]", s):
        word = next((g for g in m.groups() if g), "")
        if _ok(word) and not _platform_hit(word):
            return word.strip(), 0.9
    d = (from_display or "").strip().strip('"“”')
    d = re.sub(r"[｜|/·]\s*.*$", "", d)                                   # 「腾讯招聘 | 校园招聘」→ 腾讯招聘
    d = re.sub(r"\s*[-—]\s*.*$", "", d)
    d = re.sub(r"\s*(校园)?(招聘|校招)(中心|官网|组|团队)?\s*$", "", d)      # 只剥后缀：腾讯招聘→腾讯
    d = re.sub(r"\s*(HR|人力|人才)\s*$", "", d, flags=re.I)
    if _ok(d) and "@" not in d and not _platform_hit(d):
        return d, 0.75
    dom = (from_addr or "").split("@")[-1].lower().strip()
    # 教育域名不是招聘方（2026-10-02：hr@tsinghua.edu.cn 曾建出「tsinghua」志愿）
    if dom and not any(p in dom for p in _PLATFORM_DOMAINS) and not re.search(r"(?:^|\.)edu(?:\.|$)", dom):
        core = dom.split(".")[0]
        if _ok(core) and "no-reply" not in core and "noreply" not in core and "mail" not in core:
            return core, 0.5
    return None, 0.0


def _auto_create_enabled():
    """config「自动建志愿」开关（默认开）；每次调用实时读，设置页改完即时生效。"""
    return grist_store.get_config().get("自动建志愿") is not False


# ---------------- 公司匹配 ----------------
# 公司别名（邮件称呼 -> 主表公司名）：**代码零内置**，个人映射放 config.json「公司别名」
#（看板「设置」页可视化编辑），代码保持通用、不含任何个性化配置（2026-10-01）。
_COMPANY_ALIAS_BASE = {}
COMPANY_ALIAS = {**_COMPANY_ALIAS_BASE, **(grist_store.get_config().get("公司别名") or {})}

# 营销/广告特征（2026-09-30）：主题命中的邮件一律进待确认队列人工判断，
# 修复：带关键词的促销邮件（如「【华为云】服务器5折优惠，测试体验」）会因公司名
# 子串命中被 1.0 分直接挂账，污染该公司存档甚至建出假考试行。
MARKETING_PAT = re.compile(r"优惠|促销|折扣|返现|领券|领取.*券|抽奖|红包|秒杀|拼团|砍价|立减|满减|低价|特惠|开学季|年中庆|双十一|双十一|618|会员日")

# ---------------- Grist 存储 / 待确认队列 ----------------
def load_main():
    """读 Grist 主表快照：{行id: {"id":.., "fields":{...}}}（形状与原飞书版一致）。"""
    return {it["id"]: it for it in grist_store.list_records(grist_store.T_MAIN)}

def companies_excludes(mains):
    """由主表快照派生 companies（公司名->[(行id, 投递状态), ...]，独立键下同公司可多志愿）
    与 excludes（纯字符串网申链接，供链接排除）。"""
    companies, excludes = {}, []
    for rid, it in mains.items():
        f = it["fields"]
        name = txt(f.get("公司名称"))
        if name: companies.setdefault(name, []).append((rid, txt(f.get("投递状态"))))
        link = f.get("网申链接")
        if isinstance(link, str) and link: excludes.append(link)
    return companies, excludes

def load_queue():
    if not os.path.exists(QUEUE_PATH): return {"items": []}
    try:
        with open(QUEUE_PATH, encoding="utf-8") as fp:   # 显式 with：异常回溯持有句柄会让 Windows 下 replace 被锁（2026-10-01 修）
            q = json.load(fp)
    except Exception:
        # 损坏文件改名保留（不再静默当空队列——修复：写坏后下一轮 save 会把待确认邮件全部清空）
        try:
            corrupt = QUEUE_PATH + ".corrupt"
            os.replace(QUEUE_PATH, corrupt)
            print(f"  ⚠ 待确认队列.json 损坏，已保留为 {os.path.basename(corrupt)}（本轮按空队列处理）")
        except OSError:
            pass
        return {"items": []}
    if not isinstance(q, dict) or not isinstance(q.get("items"), list): return {"items": []}
    return q

def save_queue(q):
    grist_store.atomic_write_json(QUEUE_PATH, q)

def qdate(s):
    """队列里的收信时间字符串 -> CST datetime（解析失败退回当前时间，同 fetch_mails 兜底）。"""
    try: return datetime.datetime.strptime(s or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except Exception: return datetime.datetime.now(CST)

def enqueue(q, m, reason):
    """邮件进待确认队列（原因：公司未匹配 / 写入失败:...），等待下轮重试。"""
    q["items"].append({"uid": m["uid"], "收信时间": f"{m['date']:%Y-%m-%d %H:%M:%S}",
                       "主题": m["subject"], "发件人": m["from"], "发件人名": m.get("from_display", ""),
                       "正文": m["body"],
                       "原因": reason, "入队": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")})


MAX_AUTO_RETRY = 3   # 连败 N 次挂起：毒邮件曾每 2 小时无限重试（2026-10-02）

def mark_retry_failure(it, reason):
    """队列条目自动重试失败：累计次数；连败 MAX_AUTO_RETRY 次挂起（看板手动重试仍可用）。"""
    n = int(it.get("重试次数") or 0) + 1
    it["重试次数"] = n
    if n >= MAX_AUTO_RETRY and not it.get("挂起"):
        it["挂起"] = True
        it["原因"] = f"{reason}（已自动重试{n}次，挂起待人工：看板中仍可手动重试/新建/忽略）"
    else:
        it["原因"] = reason
    return it


def auto_retry_ok(it):
    """挂起条目跳过每轮自动重试（解析必炸的毒邮件不再无限循环）。"""
    return not it.get("挂起")

def _resolve_rows(rows):
    """同公司多志愿消歧：唯一 → 直接用；多个非终态 → 歧义（入待确认，人工指认岗位）；
    非终态唯一 → 用它；全终态 → 取最后一条（最新行，走终态冻结存档路径）。"""
    if len(rows) == 1: return rows[0][0], None
    act = [x for x in rows if x[1] not in ("已通过", "终止")]
    if len(act) > 1: return None, "同公司多记录请指认岗位"
    if len(act) == 1: return act[0][0], None
    return rows[-1][0], None

def match_company(subject, body, companies):
    """返回 (公司名, 行id, 匹配分, 歧义原因)。companies: {公司名: [(行id, 投递状态), ...]}。
    行 id 为 None 且歧义原因非空 = 命中公司但多志愿无法消歧。"""
    hs, hb = (subject or "").casefold(), (body or "")[:2000].casefold()
    if MARKETING_PAT.search(subject or ""):
        return None, None, 0.0, "主题疑似营销/广告邮件，请人工确认"
    for alias, real in COMPANY_ALIAS.items():
        if alias.casefold() in hs:   # 仅主题（2026-10-02 修：原连正文一起裸子串匹配，
            # 正文提一句「比肩阿里」就 0.95 分挂账且优先于主题精确命中别的公司）
            if real in companies:
                rid, amb = _resolve_rows(companies[real])
                if amb: return real, None, 0.95, amb
                return real, rid, 0.95, None
    subj_hits, body_hits = [], []
    for n in companies:
        k = n.casefold()
        if k and k in hs: subj_hits.append(n)
        elif k and k in hb: body_hits.append(n)
    if len(subj_hits) == 1:
        rid, amb = _resolve_rows(companies[subj_hits[0]])
        return subj_hits[0], rid, 1.0, amb
    if len(subj_hits) > 1:
        dis = [n for n in subj_hits if n.casefold() in hb]
        if len(dis) == 1:
            rid, amb = _resolve_rows(companies[dis[0]])
            return dis[0], rid, 0.9, amb
        best = max(dis or subj_hits, key=len)
        rid, amb = _resolve_rows(companies[best])
        return best, rid, 0.6, amb
    if len(body_hits) == 1:
        n = body_hits[0]
        cnt = hb.count(n.casefold())
        if len(n) >= 4 or cnt >= 2:
            rid, amb = _resolve_rows(companies[n])
            return n, rid, 0.85, amb
        # 短公司名（如“移动”）仅在正文出现一次 → 大概率撞普通词（案例：“手机移动网络”
        # 让卡斯柯测评误挂到浙江移动，2026-09-23 修）→ 不自动挂，进队列人工确认
        return None, None, round(0.4, 2), f"正文疑似「{n}」但仅出现一次，需人工确认"
    best, bs = None, 0
    for n in companies:
        s = difflib.SequenceMatcher(None, hs, n.casefold()).ratio()
        if s > bs: best, bs = n, s
    if bs >= 0.8:
        rid, amb = _resolve_rows(companies[best])
        return best, rid, round(bs, 2), amb
    return None, None, round(bs, 2), None

# ---------------- 合并 ----------------
def _naive(v):
    """日期形状归一：Grist 读回带时区 datetime（兼容迁移期毫秒 int）→ 去 tzinfo 后比较/写入，
    写入端 grist_store 会自动把 datetime/date 转 ISO。"""
    if isinstance(v, datetime.datetime): return v.replace(tzinfo=None)
    if isinstance(v, (int, float)): return datetime.datetime.fromtimestamp(v / 1000, CST).replace(tzinfo=None)
    return v

def fmt_dt(v):
    return v.strftime("%m-%d %H:%M") if v else "—"

# ---------------- 相对时限共性抽取（2026-09-20） ----------------
# 一族表述：数字(阿拉伯/中文) × 单位(天/日/小时/周/星期/工作日/月) ×「(之|以)内」或「有效期(+为)」
# 例：3天内 / 3天之内 / 三日内 / 72小时内 / 一周内 / 有效期72小时 / 有效期为7天 / 3个工作日内 / 一个月内
CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

def _cn_num(s):
    if s.isdigit(): return int(s)
    if s == "十": return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_DIGIT.get(a, 1) if a else 1) * 10 + (CN_DIGIT.get(b, 0) if b else 0)
    return CN_DIGIT.get(s)

_REL_UNIT_PAT = r"(\d{1,4}|[一二两三四五六七八九十]{1,3})\s*(个工作日|小时|天|日|周|星期|个?月)"

def _rel_delta(num, unit):
    n = _cn_num(num)
    if n is None: return None
    if unit == "小时": ok, kw = 1 <= n <= 96, {"hours": n}
    elif unit in ("天", "日", "自然日"): ok, kw = 1 <= n <= 30, {"days": n}
    elif unit in ("周", "星期"): ok, kw = 1 <= n <= 4, {"weeks": n}
    elif unit == "个月": ok, kw = 1 <= n <= 3, {"days": 30 * n}   # 月≈30天近似
    else: ok, kw = 1 <= n <= 20, {"days": n}                      # 工作日≈自然日近似
    return datetime.timedelta(**kw) if ok else None

def parse_relative_window(text):
    """相对时限共性抽取：返回 (命中原句, timedelta) 或 None。
    两种句式：①N+单位+(之|以)内  ②有效期(+为)?N+单位。边界：天≤30、小时≤96、周≤4、月≤3、工作日≤20。"""
    text = text or ""
    num = r"(\d{1,4}|[一二两三四五六七八九十]{1,3})"
    unit = r"(个自然日|个工作日|个?月|小时|自然日|天|日|周|星期)"
    for pat in (num + r"\s*" + unit + r"\s*(?:之|以)?内",
                r"有效期(?:为)?\s*" + num + r"\s*" + unit):
        m = re.search(pat, text)
        if m:
            d = _rel_delta(m.group(1), m.group(2))
            if d is not None:
                return m.group(0).strip(), d
    return None

def _default_window(mtype, recv):
    """邮件未给任何时限时的默认行（笔试/测评统一）：开始=收信日、无结束。
    前端把带「默认提醒」标记且无结束时间的行按“每日提醒直到勾完成”展示（蚂蚁类，2026-09-20 定版）。"""
    return (mtype, datetime.datetime(recv.year, recv.month, recv.day), None)

def plan_exam(ext, mtype, recv):
    """由抽取结果规划考试安排（Exams 子表）：返回 (考试类型, 开始, 结束或None)；无安排返回 None。
    邮件明确给出起止的（固定场次/时间窗，如“链接有效期 A-B 任选作答”）→ 保留起止（展示层只显示首尾）；
    只知截止的（截止日/相对时限，“X日前完成”）→ 只记截止时刻。
    测评：场次类窗口(AI面试等) > 截止日(带时刻提前一日) > 相对时限 > 收信+3天默认。
    “选择/预约笔试/面试时间”类事务邮件 → 归「其他事项」类型。"""
    wmode, ws, we, wev, wexp = ext.get("window", ("无信息", None, None, "-", False))
    d, t, raw, cls = ext.get("deadline", (None, None, None, 2))
    if mtype == "问卷":   # 问卷/登记表：通常要填写 → 其他事项；有时限记时限，没有则每日提醒直到勾选
        if ws and wmode != "无信息":
            return ("其他事项", _naive(ws), _naive(we) if we else None)
        return _default_window("其他事项", recv)
    # 选时间/预约类 → 其他事项；但邮件已明示具体场次/窗口（固定场次/时间窗/场次+时长）时
    # 以真实考试为准，不被按钮文字（如“确认参加”）误降级（广联达案例 2026-09-24）
    if ext.get("sched") and mtype in ("笔试", "面试", "测评") and wmode not in ("固定场次", "时间窗", "场次+时长"):
        start = _naive(we) if we else (_naive(ws) if ws else recv)
        return ("其他事项", start, None)
    if mtype == "面试":
        if ws and wmode in ("固定场次", "场次+时长", "时间窗"):
            return ("面试", _naive(ws), _naive(we) if we else None)
        return None
    if mtype == "笔试":
        if not ws or wmode == "无信息":
            if wmode == "无信息":
                return _default_window("笔试", recv)   # 与测评一致：默认提醒窗口（2026-09-20 统一）
            return None
        if wmode in ("截止日", "相对时限"):
            # 有效期窗口：开始=收信时刻，结束=截止（72小时/3天内/9月20日前 同一语义，2026-09-20 用户定版：起止都要）
            return ("笔试", _naive(ws), _naive(we))
        return ("笔试", _naive(ws), _naive(we) if we else None)   # 场次/有效期窗口：保留起止
    if mtype == "测评":
        if ws and wmode in ("固定场次", "场次+时长", "时间窗"):
            return ("测评", _naive(ws), _naive(we) if we else None)
        if wmode == "相对时限" and we:
            return ("测评", _naive(ws), _naive(we))          # 有效期窗口：收信→到期
        if d:
            if t:
                hh, mm = int(t.split(":")[0]), int(t.split(":")[1])
            else:
                hh, mm = 23, 59
            eff = datetime.datetime.combine(d, datetime.time(hh, mm))
            return ("测评", _naive(recv), eff)                # 截止式：收信→截止时刻
        return _default_window("测评", recv)
    if mtype == "其他" and ws:
        return ("其他事项", _naive(ws), _naive(we) if we else None)   # 宣讲会等活动提醒（2026-09-21）
    return None

def plan_main_updates(mf, ext, mtype):
    """主表只填空（v4）：仅 注意事项/场次安排。
    5 个时间/链接字段（笔试时间/笔试结束/面试时间/测评截止/考试链接）已退役——
    考试时间与链接改走 Exams 子表（见 plan_exam + grist_store.add_exam），新邮件=新考试行。"""
    upd, prev, conf = {}, [], []
    notes = ext.get("notes", "")
    if notes and mf.get("注意事项") is None:
        upd["注意事项"] = notes
        prev.append(f"注意事项={notes[:40]}…")
    elif notes: conf.append("注意事项已有值不覆盖")
    ses = ext.get("sessions", "")
    if ses and mf.get("场次安排") is None:
        upd["场次安排"] = ses
        prev.append(f"场次安排={ses[:40]}…")
    elif ses: conf.append("场次安排已有值不覆盖")
    return upd, prev, conf

def report_conflicts():
    """考试安排冲突检测（数据源=Exams 子表）：笔试按 结束-开始（缺省120min）、面试按 ±60min，
    跨度>8小时视为“可任选窗口”不参与；测评=截止类全天，不存在硬冲突。"""
    name_by_rid = {r["id"]: txt(r["fields"].get("公司名称"))
                   for r in grist_store.list_records(grist_store.T_MAIN)}
    events = []
    for x in grist_store.list_records(grist_store.T_EXAM):
        f = x["fields"]
        typ = str(f.get("考试类型") or "")
        if typ not in ("笔试", "面试"): continue
        s = _naive(f.get("开始时间"))
        if not isinstance(s, datetime.datetime): continue
        e = _naive(f.get("结束时间"))
        if typ == "笔试":
            if not isinstance(e, datetime.datetime): e = s + datetime.timedelta(minutes=120)
            if (e - s) > datetime.timedelta(hours=8): continue
        else:
            e = s + datetime.timedelta(minutes=60)
        co = name_by_rid.get(f.get("公司")) or "?"
        events.append((s, e, f"{co} {typ}"))
    warns = []
    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            a, b = events[i], events[j]
            if a[0] < b[1] and b[0] < a[1]:
                warns.append(f"⚠ 时间冲突: {a[2]} {a[0]:%m-%d %H:%M}~{a[1]:%H:%M} ↔ {b[2]} {b[0]:%m-%d %H:%M}~{b[1]:%H:%M}")
    for w in warns: print(w)
    return warns

def fill_note(company, mtype, ext, mdate):
    """公司笔记时间线空行填充：- 笔试： / - 测评：。只填空行，无信息可填则跳过。"""
    label = {"笔试": "笔试", "测评": "测评"}.get(mtype)
    if not label: return False
    p = os.path.join(OUT, company + ".md")
    if not os.path.exists(p): return False
    s = open(p, encoding="utf-8").read()
    d, t, raw, cls = ext.get("deadline", (None, None, None, 2))
    parts = [f"{mdate:%m%d}邮件"]
    if d:
        eff = d - datetime.timedelta(days=1) if t else d
        seg = f"截止{eff:%m-%d}"
        if t: seg += f"（原{d:%m-%d} {t}）"
        parts.append(seg)
    if ext.get("url"): parts.append(ext["url"])
    if ext.get("sessions"): parts.append(str(ext["sessions"])[:40])
    if len(parts) == 1:
        if not ext.get("notes"): return False
        parts.append(str(ext["notes"])[:60])
    content = "，".join(parts)
    pat = re.compile(rf"^(\t- {label}：)\s*$", re.M)
    new, n = pat.subn(lambda m: m.group(1) + content, s, count=1)
    if new != s:
        open(p, "w", encoding="utf-8").write(new)
        return True
    return False

# ---------------- 命令 ----------------
def process_one(m, mains, companies, excludes, dry):
    """处理一封邮件：抽取+匹配（多志愿消歧）+Grist 只填空更新+考试安排行(add_exam)+无条件存档+笔记填充。
    返回 (是否成功, 失败原因)，失败原因供待确认队列记录。"""
    mtype = extract_type(m["subject"], m["body"])
    d, t, raw, cls = find_deadline(m["body"], m["date"].date())
    window = plan_window(m["body"], m["date"], 60 if mtype in ("测评", "面试") else 120)
    url = extract_url(m["body"], excludes)
    ext = {"deadline": (d, t, raw, cls), "window": window, "url": url, "_body": m["body"],
           "notes": extract_notes(m["body"]), "sessions": extract_sessions(m["body"]),
           "sched": is_sched_mail(m["subject"], m["body"])}
    name, rid, score, amb = match_company(m["subject"], m["body"], companies)
    line = f"{m['date']:%m-%d} [{mtype}] {name or '??未匹配'}({score}) {m['subject'][:36]}"
    if amb:
        print(f"  ⚠ {line} —— {amb}，进待确认队列")
        return False, amb
    if not rid:
        # 自动创建志愿（2026-10-01）：识别得出公司名（主题括号/发件人显示名/私有域名）就自动建档，
        # 感谢信/测评/笔试等邮件从此不用手动建条目；识别不出（平台群发/营销）才进待确认队列。
        # 例外：宣讲会/招聘会/类型不明邮件不自动建档——宣讲会不算投递（2026-10-01 用户定版），
        #       进队列由用户在条目详情里选择「新建志愿」或「忽略」。
        if mtype == "其他":
            print(f"  ⚠ {line} —— 宣讲会/类型不明邮件，不自动建档，进待确认队列")
            return False, "宣讲会/类型不明邮件，需人工确认（可在条目中新建志愿或忽略）"
        guess, conf = extract_company_guess(m["subject"], m.get("from", ""), m.get("from_display", ""))
        if not (guess and _auto_create_enabled()):
            print(f"  ⚠ {line} —— 公司未匹配，进待确认队列")
            return False, "公司未匹配"
        exist = grist_store.find_record(guess, "")
        if exist:
            rid, name = exist["id"], guess
            mains[rid] = exist
            companies.setdefault(guess, []).append((rid, txt(exist["fields"].get("投递状态"))))
            print(f"  ↺ {line} —— 识别为公司「{guess}」，挂到已有志愿")
        else:
            rec = grist_store.add_record(grist_store.T_MAIN, {
                "公司名称": guess, "投递状态": "简历评估",
                "投递日期": f"{m['date']:%Y-%m-%d}",
                "状态备注": "（自动创建：来自邮件，请补充岗位/行业）"})
            rid, name = rec["id"], guess
            mains[rid] = rec
            companies.setdefault(guess, []).append((rid, "简历评估"))
            print(f"  ＋ {line} —— 自动创建志愿「{guess}」" + ("（dry-run，未写入）" if dry else ""))
    mf = mains[rid]["fields"]
    if txt(mf.get("投递状态")) in ("已通过", "终止"):
        print(f"  ⏸ {line} 终态仅存档")  # 终态冻结：不动字段、不推进状态、不建考试行
        if not dry:
            try:
                grist_store.add_archive(name, m["date"], m["subject"], m["body"])
            except Exception as e:
                return False, f"写入失败:{e}"
        return True, ""
    upd, prev, conf = plan_main_updates(mf, ext, mtype)
    wmode = ext["window"][0]
    new_status = plan_status(mtype, wmode, txt(mf.get("投递状态")))
    if new_status and new_status != txt(mf.get("投递状态")):
        upd["投递状态"] = new_status
        prev.append(f"状态:{txt(mf.get('投递状态')) or '未投递'}→{new_status}")
    ex = plan_exam(ext, mtype, m["date"])
    if ex:
        prev.append(f"考试安排+{ex[0]} {fmt_dt(ex[1])}" + (f"~{fmt_dt(ex[2])}" if ex[2] else ""))
    note_ok = False
    if not dry:
        if upd:
            try:
                rec = grist_store.update_record(grist_store.T_MAIN, rid, upd)
                mf.update(rec["fields"])  # 用返回的合并 fields 刷新快照，同公司下一封才能正确“不覆盖”
            except Exception as e:
                print(f"  ✗ {line} Grist 写入失败 {e}")
                return False, f"写入失败:{e}"
        if ex:
            try:
                # 同链接定位已有考试行：提醒/改期邮件不再盲目新建（2026-09-20 鲁棒性加固）
                target = None
                if url:
                    for x in grist_store.list_records(grist_store.T_EXAM, filter={"公司": [rid]}):
                        if str(x["fields"].get("考试链接") or "") == url:
                            target = x; break
                resched = bool(RESCHED_PAT.search(m["subject"] or "")
                               or RESCHED_PAT.search((m["body"] or "")[:200]))
                if target and resched:
                    old = fmt_dt(_naive(target["fields"].get("开始时间")))
                    grist_store.update_record(grist_store.T_EXAM, target["id"], {
                        "考试类型": ex[0], "开始时间": ex[1], "结束时间": ex[2],
                        "来源主题": (f"改期至{fmt_dt(ex[1])}(原{old})·{m['subject'][:24]}")[:80],
                    }, clear_none=True)
                    prev.append(f"考试改期→{ex[0]} {fmt_dt(ex[1])}(原{old})")
                elif target:
                    if url and not target["fields"].get("考试链接"):
                        grist_store.update_record(grist_store.T_EXAM, target["id"], {"考试链接": url})
                    conf.append("同链接考试行已存在(提醒/重复邮件,不新建)")
                else:
                    src = m["subject"] + (" · 默认提醒(邮件未给时限)" if ex[0] in ("笔试", "测评", "其他事项") and wmode == "无信息" else "")
                    _, created = grist_store.add_exam(rid, ex[0], ex[1], 结束=ex[2],
                                                      链接=url, 来源主题=src)
                    if not created:
                        conf.append("考试安排已存在(同记录+类型+开始,幂等跳过)")
            except Exception as e:
                print(f"  ✗ {line} Grist 写入失败 {e}")
                return False, f"写入失败:{e}"
        try:
            grist_store.add_archive(name, m["date"], m["subject"], m["body"])  # 无条件存档
        except Exception as e:
            return False, f"写入失败:{e}"
        note_ok = fill_note(name, mtype, ext, m["date"])
    detail = "; ".join(prev) if prev else ("无新填补" if not conf else "")
    if conf: detail += (" | " if detail else "") + ";".join(conf)
    if note_ok: detail += " | 笔记时间线已填"
    print(f"  ✓ {line} -> {detail or '仅存档'}")
    return True, ""

def _load_state():
    """读 mail_state.json（损坏回退 {}，靠存档去重/add_exam 幂等兜底重放）。"""
    try:
        with open(STATE_PATH, encoding="utf-8") as _fp:
            st = json.load(_fp)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}

def _save_state(st):
    grist_store.atomic_write_json(STATE_PATH, st)

def cmd_sync(dry):
    mails, newmax = fetch_mails("--all" in sys.argv)
    hits = [m for m in mails if subject_hit(m["subject"])]
    # 队列/已见集合的整段读-改-写上跨进程文件锁（看板「重试/忽略」与定时任务并发安全）
    with grist_store.cross_lock(QUEUE_PATH, timeout=600, stale=1800):
        st = _load_state()
        seen = set(st.get("uids", []))
        q = load_queue()
        inq = {x.get("uid") for x in q["items"]}
        news = [m for m in hits if m["uid"] not in seen and m["uid"] not in inq]
        mains = load_main()
        companies, excludes = companies_excludes(mains)
        print(f"新邮件 {len(mails)} 封，队列重试 {len(q['items'])} 条，新待处理 {len(news)} 封"
              + ("（dry-run）" if dry else ""))
        # 先重试队列（按收信时间正序，重建 m 走同一 process_one 路径；挂起条目跳过自动重试）
        for it in sorted([x for x in q["items"] if auto_retry_ok(x)], key=lambda x: x.get("收信时间") or ""):
            m = {"uid": it.get("uid"), "date": qdate(it.get("收信时间")),
                 "from": it.get("发件人", ""), "from_display": it.get("发件人名", ""),
                 "subject": it.get("主题", ""), "body": it.get("正文", "")}
            try:
                ok, reason = process_one(m, mains, companies, excludes, dry)
            except Exception as e:   # 单封解析异常不炸全链（2026-09-30：畸形时刻曾致整轮同步中止）
                print(f"  ✗ uid={m['uid']} 解析异常 {type(e).__name__}: {e}")
                ok, reason = False, f"解析异常:{type(e).__name__}"
            if dry: continue
            if ok:
                q["items"] = [x for x in q["items"] if x is not it]; seen.add(m["uid"])
            else:
                mark_retry_failure(it, reason)
        # 再处理新邮件：只有成功才进 seen；失败入队下轮重试
        for m in news:
            try:
                ok, reason = process_one(m, mains, companies, excludes, dry)
            except Exception as e:
                print(f"  ✗ uid={m['uid']} 解析异常 {type(e).__name__}: {e}")
                ok, reason = False, f"解析异常:{type(e).__name__}"
            if dry: continue
            if ok: seen.add(m["uid"])
            else: enqueue(q, m, reason)
        if dry: return
        seen |= {m["uid"] for m in mails if not subject_hit(m["subject"])}  # 非关键词直接 seen，防重复扫描
        # 落盘顺序（2026-10-01 修）：先状态后队列——两文件无法原子同写，中间崩溃时
        # 「已见∩队列」只是多重试一次（幂等），比「队列没了+未标已见」（复活循环）安全
        _save_state({"highest_uid": newmax, "uids": sorted(seen)})
        save_queue(q)
    print(f"完成，highest_uid={newmax}，队列剩 {len(q['items'])} 条")
    report_conflicts()
    try:
        grist_store.snapshot_status()   # 每日状态快照（漏斗历史），失败不影响主链路
    except Exception as e:
        print(f"  ⚠ 状态快照失败（不影响主链路）: {e}")
    try:
        _bp = grist_store.backup_now()   # 每轮同步后自动备份（2026-10-02：download_backup 原是死代码，
        print(f"备份完成: {os.path.basename(_bp)}")   # WAL 下手工复制 .sqlite 会丢最近提交）
    except Exception as e:
        print(f"  ⚠ 自动备份失败（不影响主链路）: {e}")

def cmd_merge(dry):
    """仅重试 待确认队列（成功出队并标记 seen；旧飞书暂存表重试已退役，命令名兼容保留）。"""
    with grist_store.cross_lock(QUEUE_PATH, timeout=600, stale=1800):
        mains = load_main()
        companies, excludes = companies_excludes(mains)
        q = load_queue()
        st = _load_state()
        seen = set(st.get("uids", []))
        print(f"待重试 {len(q['items'])} 条" + ("（dry-run）" if dry else ""))
        for it in sorted([x for x in q["items"] if auto_retry_ok(x)], key=lambda x: x.get("收信时间") or ""):
            m = {"uid": it.get("uid"), "date": qdate(it.get("收信时间")),
                 "from": it.get("发件人", ""), "from_display": it.get("发件人名", ""),
                 "subject": it.get("主题", ""), "body": it.get("正文", "")}
            try:
                ok, reason = process_one(m, mains, companies, excludes, dry)
            except Exception as e:
                print(f"  ✗ uid={m['uid']} 解析异常 {type(e).__name__}: {e}")
                ok, reason = False, f"解析异常:{type(e).__name__}"
            if dry: continue
            if ok:
                q["items"] = [x for x in q["items"] if x is not it]; seen.add(m["uid"])
            else:
                mark_retry_failure(it, reason)
        if dry: return
        _save_state({"highest_uid": st.get("highest_uid", 0), "uids": sorted(seen)})
        save_queue(q)
    report_conflicts()

def cmd_list(n):
    M = imap_connect()
    try:
        _, data = M.search(None, "ALL")
        ids = data[0].split()[-n:]
        hits = []
        print(f"最近 {len(ids)} 封（★=含求职关键词）")
        for i in ids:
            _, d = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
            msg = email.message_from_bytes(d[0][1])
            date = dec(msg.get("Date", ""))[:11]
            frm = dec(msg.get("From", ""))
            frm = re.search(r"<(.+?)>", frm).group(1) if re.search(r"<(.+?)>", frm) else frm
            subj = dec(msg.get("Subject", ""))
            hit = "★" if subject_hit(subj) else " "
            if hit == "★": hits.append((date, frm, subj))
            print(f"{hit} {date} | {frm[:28]:28} | {subj[:52]}")
        if hits:
            print("\n★ 求职相关：")
            for d_, f_, s_ in hits: print(f"  {d_} | {f_} | {s_}")
    finally:
        M.logout()

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        try:
            cmd_list(15)
        except RuntimeError as e:   # 未配置邮箱等友好错误（imap_connect 抛出）
            print(f"✗ {e}")
    elif args[0].isdigit():
        try:
            cmd_list(int(args[0]))
        except RuntimeError as e:
            print(f"✗ {e}")
    elif args[0] == "--sync":
        try:
            cmd_sync("--dry-run" in args)
        except RuntimeError as e:   # 未配置邮箱（imap_connect 抛出的友好错误）
            print(f"✗ {e}")
            sys.exit(1)
        except (imaplib.IMAP4.error, OSError, TimeoutError) as e:
            # 断网/超时/密码错：一行人话 + 常见原因，不再甩英文 traceback（2026-10-02）
            print(f"✗ 同步失败：{type(e).__name__}: {e}")
            print("  常见原因：网络不通 / 服务器地址写错 / 授权码不对（不是登录密码）/ 代理拦截")
            sys.exit(1)
    elif args[0] == "--merge":
        try:
            cmd_merge("--dry-run" in args)
        except RuntimeError as e:
            print(f"✗ {e}")
            sys.exit(1)
        except (imaplib.IMAP4.error, OSError, TimeoutError) as e:
            print(f"✗ 队列重试失败：{type(e).__name__}: {e}")
            print("  常见原因：网络不通 / 服务器地址写错 / 授权码不对（不是登录密码）/ 代理拦截")
            sys.exit(1)
    else:
        print(__doc__)
