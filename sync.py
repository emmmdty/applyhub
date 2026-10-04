# -*- coding: utf-8 -*-
"""Grist -> 本地 Obsidian 单向同步（Grist 为唯一事实源）：
1) 公司笔记 frontmatter 刷新：投递状态/投递日期/笔试时间/面试时间/测评截止
   （缺键补、改值同步、显式清空同步——Grist 里空值即视为清空）
   其中三个时间键从「考试安排」Exams 子表取每类最近一场（多轮面试=最新一轮）；
   Exams 表不存在时回退主表旧字段（迁移前的过渡兼容）
2) Grist 新增公司 -> 自动建空笔记（行业/岗位/链接仅此时初填一次）
3) 打印本地孤儿笔记（本地有、Grist 没有），只报告不处理
用法: python3 sync.py （同目录需 grist_store.py 与含 grist_doc_id 的 凭据.json，无第三方依赖）
注意：主表「笔试时间/笔试结束/面试时间/测评截止/考试链接」已退役（进 Exams 子表），
      管道不再读主表这三键；日期列读出为 datetime(CST)，写 frontmatter 取 .date()。"""
import json, os, re, sys, datetime
import grist_store
DRY = "--dry-run" in sys.argv

BASE = os.path.dirname(os.path.abspath(__file__))
VAULT = os.environ.get("SYNC_VAULT_DIR") or grist_store.get_config().get("笔记库目录") or ""
VAULT = os.path.abspath(os.path.expanduser(VAULT)) if str(VAULT).strip() else os.path.dirname(os.path.dirname(BASE))
OUT = os.path.join(VAULT, str(grist_store.get_config().get("笔记子目录") or "09-秋招投递"))
os.makedirs(OUT, exist_ok=True)
VAULT_NAME = os.path.basename(VAULT)
CST = datetime.timezone(datetime.timedelta(hours=8))

# (Grist 列名候选（按优先级）, 本地 frontmatter 键, 类型)
TRACK = [(("投递状态",), "投递状态", "text"),
         (("投递日期",), "投递日期", "date"),
         (("笔试时间",), "笔试时间", "date"),
         (("面试时间",), "面试时间", "date"),
         (("测评截止",), "测评截止", "date")]

def dms(v):
    return datetime.datetime.fromtimestamp(v / 1000, CST).date() if isinstance(v, (int, float)) else None

def pick(f, names):
    """返回 (是否显式存在该字段, 值)。"""
    for n in names:
        if n in f: return True, f[n]
    return False, None

# ---- 读 Grist 主表 ----
try:
    _rows = grist_store.list_records(grist_store.T_MAIN)
except RuntimeError as e:
    print("读取 Grist 失败：", e)
    print("请确认 Grist Desktop 已启动，且 凭据.json 已配置 grist_doc_id（或设置环境变量 GRIST_DOC_ID）。")
    sys.exit(1)

# ---- 读「考试安排」Exams 子表：记录id -> {类型: 该键应写的时间}；表不存在则回退主表旧字段 ----
# 笔试时间/面试时间=最近一场的开始；测评截止=最近一场的结束（截止式窗口：开始=收信、
# 结束=截止——修复：原取开始时间，笔记里「测评截止」写成的是收件日而非真实截止，2026-09-30 修）
EXAM_SRC = {"笔试时间": "笔试", "面试时间": "面试", "测评截止": "测评"}
EXAM_USE_END = {"测评"}
def _latest_exams():
    best = {}
    for x in grist_store.list_records(grist_store.T_EXAM):
        f = x.get("fields") or {}
        ref, typ, s = f.get("公司"), f.get("考试类型"), f.get("开始时间")
        if not isinstance(ref, int) or typ not in EXAM_SRC.values():
            continue
        if not isinstance(s, datetime.datetime):
            continue
        cur = best.setdefault(ref, {}).get(typ)
        if cur is None or s > cur[0]:
            v = f.get("结束时间") if typ in EXAM_USE_END else s
            best[ref][typ] = (s, v if isinstance(v, datetime.datetime) else None)
    return {rid: {t: v for t, (_, v) in d.items()} for rid, d in best.items()}

try:
    _exams = _latest_exams()
    HAS_EXAMS = True
except RuntimeError:
    _exams, HAS_EXAMS = {}, False  # 子表未建（迁移未跑）：三时间键回退主表旧字段

recs = []
for it in _rows:
    f = it["fields"]
    name = f.get("公司名称") or ""
    if not name: continue
    row = {"rid": it["id"], "公司": name,
           "行业": f.get("行业") or "", "岗位": f.get("岗位名称") or "",
           "链接": f.get("网申链接") or ""}
    for names, key, typ in TRACK:
        if HAS_EXAMS and key in EXAM_SRC:
            v = _exams.get(it["id"], {}).get(EXAM_SRC[key])  # 最近一场；无该类型考试=显式清空
            exists = True
            empty = v is None
        else:
            exists, v = pick(f, names)
            empty = (v is None or v == "")
        if typ == "text":
            row[key] = None if empty else v
        else:
            if isinstance(v, datetime.datetime): v = v.date()
            elif isinstance(v, datetime.date): pass
            elif isinstance(v, (int, float)): v = dms(v)  # 历史毫秒时间戳兼容
            else: v = None
            row[key] = v
        # Grist 固定 schema 全列返回：空值（None 或 ""）即显式清空
        row[key + "__清"] = empty
    recs.append(row)
have_f = {r["公司"] for r in recs}

# ---- 1) Grist -> 本地 frontmatter 单向刷新（缺键补、改值同步、显式清空同步） ----
def upsert_key(fm, key, line):
    pat = re.compile(rf"^{re.escape(key)}[：:].*$", re.M)
    if pat.search(fm): return pat.sub(lambda m: line, fm)
    return fm.rstrip("\n") + "\n" + line

n_sync = 0
sync_names = []
for r in recs:
    p = os.path.join(OUT, r["公司"] + ".md")
    if not os.path.exists(p): continue
    s = open(p, encoding="utf-8").read()
    m = re.match(r"^(---\n)(.*?)(\n---\n)(.*)$", s, re.S)
    if not m: continue
    fm, body, changed = m.group(2), m.group(4), False
    for names, key, typ in TRACK:
        exists = r[key + "__清"]
        if not exists and r[key] is None: continue
        if typ == "text":
            line = f"{key}: {json.dumps(r[key] or '', ensure_ascii=False)}"
        else:
            line = f"{key}: {r[key].isoformat() if r[key] else ''}"
        new_fm = upsert_key(fm, key, line)
        if new_fm != fm: fm, changed = new_fm, True
    if changed:
        if not DRY:
            open(p, "w", encoding="utf-8").write(m.group(1) + fm + m.group(3) + body)
        n_sync += 1
        sync_names.append(r["公司"])

# ---- 2) Grist 新增 -> 建空笔记（身份字段仅此刻初填） ----
new_notes = 0
for r in recs:
    p = os.path.join(OUT, r["公司"] + ".md")
    if os.path.exists(p): continue
    fm = ["---",
          f"行业: {json.dumps(r['行业'], ensure_ascii=False)}",
          f"岗位名称: {json.dumps(r['岗位'], ensure_ascii=False)}",
          f"网申链接: {json.dumps(r['链接'], ensure_ascii=False)}"]
    for names, key, typ in TRACK:
        fm.append(f"{key}: {json.dumps(r[key] or '', ensure_ascii=False)}" if typ == "text"
                  else f"{key}: {r[key].isoformat() if r[key] else ''}")
    fm += ["---", "", "- 链接"]
    if r["链接"]: fm.append(f"\t- {r['链接']}")
    fm += ["\t- [[06-面试复盘/链接大全|链接大全]]", "- 岗位JD：", "- 面试复盘：", "- 时间线",
           "\t- 投递简历：", "\t- 笔试：", "\t- 测评：", "\t- 一面：", "\t- 二面：", "\t- 三面：",
           "\t- HR面：", "\t- OC："]
    if not DRY:
        open(p, "w", encoding="utf-8").write("\n".join(fm) + "\n")
    new_notes += 1

# ---- 3) 孤儿笔记报告（只报告） ----
have_local = {f[:-3] for f in os.listdir(OUT) if f.endswith(".md")}
orphans = sorted(have_local - have_f)

print(f"{'[dry-run] ' if DRY else ''}完成：frontmatter 刷新 {n_sync} 篇，新建笔记 {new_notes} 篇")
if DRY and sync_names:
    print("将刷新：" + "、".join(sync_names))
if orphans:
    print(f"本地孤儿笔记 {len(orphans)} 篇（Grist 无记录，未处理）：" + "、".join(orphans))
