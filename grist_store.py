# -*- coding: utf-8 -*-
"""投递中台 · SQLite 存储层（A 方案，2026-09-24）——替换 Grist 的本地引擎

对外接口与 Grist 版 grist_store.py 完全一致（mail.py/中台看板.py/sync.py 零改动）：
    list_records / add_record / update_record / delete_record / add_exam / exams_of
    find_record / get_by_company / list_by_company / companies / add_archive / archives_of
    snapshot_status / ensure_snaps / snap_date_str / download_backup / create_tables
    migrate_exams_from_main / column_names / companies_excludes 由 mail.py 自持

数据文件：默认 <本目录>/中台.sqlite，可用环境变量 SQLITE_PATH 覆盖（验收测试用临时库）。
并发：WAL 模式 + busy_timeout + 写锁，看板(多线程)与邮件管道(独立进程)可同时读写。
表结构：列名直接用中文概念字段名（SQLite 支持 UTF-8 列名），省去 ASCII colId 双向翻译；
        Snaps 为机器表，列名保持 d/total/s1..s6 与 Grist 侧一致。
日期：统一存 ISO 字符串（含 +08:00），读出转 datetime(CST)——与 Grist 版 _out 行为一致。
"""
import json, os, sqlite3, tempfile, threading, time, contextlib, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CST = datetime.timezone(datetime.timedelta(hours=8))

T_MAIN, T_MAIL, T_EXAM, T_SNAP = "Deliveries", "MailArchive", "Exams", "Snaps"
TABLE_CN = {T_MAIN: "投递记录", T_MAIL: "邮件存档", T_EXAM: "考试安排", T_SNAP: "状态快照"}
SQLITE_PATH = os.environ.get("SQLITE_PATH") or os.path.join(BASE, "中台.sqlite")

# ---------------- 配置（config.json，可交给用户在网页「设置」页修改） ----------------
CONFIG_PATH = os.path.join(BASE, "config.json")
_DEFAULT_CONFIG = {"端口": 8790, "笔记库目录": "", "笔记子目录": "", "备份目录": "",
                   "公司别名": {}, "关键词": [], "自动建志愿": True,
                   "品牌站点名": "", "品牌Logo": "", "品牌Logo时间": 0}


def get_config():
    """读 config.json（缺失/损坏回退默认值）；空值字段回落默认。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as _fp:
            cfg = json.load(_fp)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    out = dict(_DEFAULT_CONFIG)
    for k, v in cfg.items():
        if v is not None:
            out[k] = v
    return out


def save_config(cfg):
    atomic_write_json(CONFIG_PATH, cfg)


# ---------------- 原子写 / 跨进程文件锁（看板与邮件管道是两个进程） ----------------
def atomic_write_json(path, obj, indent=1):
    """先写临时文件再 os.replace 原子替换——进程中途被杀也不会留下半个 JSON。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def cross_lock(path, timeout=30, stale=300):
    """跨进程互斥锁（O_EXCL 锁文件；崩溃残留超过 stale 秒自动接管）。
    看板(多线程)与 mail.py 定时任务并发改 待确认队列.json / mail_state.json 时用，
    修复：裸 json.dump 非原子 + 无锁，曾可互相覆盖/写坏 JSON 导致队列被静默清空。
    2026-10-02 加固：锁内写随机 token，释放时仅当 token 仍是自己的才删——
    修复：旧持有者被 stale 接管后退出会误删新持有者的锁，第三个进程可再进入临界区。"""
    lock = os.path.abspath(str(path)) + ".lock"
    os.makedirs(os.path.dirname(lock) or ".", exist_ok=True)
    token = f"{os.getpid()} {time.time_ns()}"
    fd, start = None, time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > stale:
                    # 原子改名接管：只有一个进程能 replace 成功，其余回到重试
                    os.replace(lock, lock + ".stale")
                    try:
                        os.unlink(lock + ".stale")
                    except OSError:
                        pass
                    continue
            except FileNotFoundError:
                pass
            if time.time() - start > timeout:
                raise TimeoutError(f"文件锁等待超时: {lock}")
            time.sleep(0.05)
    try:
        try:
            os.write(fd, token.encode())
        except OSError:
            pass
        yield
    finally:
        os.close(fd)
        try:
            with open(lock, encoding="utf-8") as _lf:
                mine = _lf.read().strip() == token
        except OSError:
            mine = False
        if mine:   # 只有仍持有自己的锁才删除（被接管后不得误删新持有者的锁）
            try:
                os.unlink(lock)
            except OSError:
                pass

# 概念字段全集（列定义）；Snaps 例外（机器表，列=d/total/s1..s6）
MAIN_COLS = ["公司名称", "行业", "岗位名称", "网申链接", "投递状态", "投递日期",
             "笔试时间", "笔试结束", "面试时间", "测评截止", "考试链接", "注意事项",
             "场次安排", "状态备注", "终止原因", "测评完成", "笔试完成", "面试完成",
             "招聘渠道", "内推人"]
MAIL_COLS = ["公司", "收信时间", "主题", "正文"]
EXAM_COLS = ["公司", "考试类型", "开始时间", "结束时间", "考试链接", "完成", "完成时间", "放弃", "来源主题"]
DATE_FIELDS = {"投递日期", "笔试时间", "笔试结束", "面试时间", "测评截止",
               "收信时间", "开始时间", "结束时间", "完成时间"}
BOOL_FIELDS = {"测评完成", "笔试完成", "面试完成", "完成", "放弃"}
TERMINAL = ("已通过", "终止")

_LOCK = threading.RLock()
_CONN = None


def _conn():
    """进程内单连接（跨线程复用）；WAL + busy_timeout 支撑与邮件管道的多进程并发。"""
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=10)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA busy_timeout=8000")
        _CONN.execute("PRAGMA synchronous=NORMAL")
    return _CONN


REF_COLS = {"公司"}   # 引用列（指向主表行 id），必须保持整数亲和性


def _migrate_cols(table, cols):
    """旧库缺列自动补齐（类型规则同建表）——修复：用户按说明书 Q10 用旧 中台.sqlite 换新程序时，
    SELECT 出的行缺键，_row_to_record 直接 IndexError，/api/data 永久 ok:false 且无自愈路径。"""
    have = {r[1] for r in _conn().execute(f'PRAGMA table_info("{table}")')}
    for c in cols:
        if c not in have:
            t = "INTEGER" if c in BOOL_FIELDS or c in REF_COLS else "TEXT"
            _conn().execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" {t}')


def _ensure_table(table):
    cols = {"Deliveries": MAIN_COLS, "MailArchive": MAIL_COLS, "Exams": EXAM_COLS}.get(table)
    if cols:
        def coltype(c):
            if c in BOOL_FIELDS or c in REF_COLS: return "INTEGER"
            return "TEXT"
        ddl = ", ".join(['"id" INTEGER PRIMARY KEY AUTOINCREMENT'] +
                        [f'"{c}" {coltype(c)}' for c in cols])
        _conn().execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({ddl})')
        _migrate_cols(table, cols)   # 旧库缺列自动补齐（新库刚建满列时为 no-op）
    elif table == T_SNAP:
        _conn().execute(f'CREATE TABLE IF NOT EXISTS "{T_SNAP}" ('
                        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, d TEXT, total INTEGER,'
                        's1 INTEGER, s2 INTEGER, s3 INTEGER, s4 INTEGER, s5 INTEGER, s6 INTEGER)')
    _conn().commit()


def _norm_out(col, v):
    """SQLite 值 → 上层期望形状：日期列=datetime(CST)、布尔列=bool、其余原样。"""
    if col in DATE_FIELDS:
        if v in (None, ""): return None
        if isinstance(v, str):
            try:
                dt = datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=CST)
            except ValueError:
                return v
        return v
    if col in BOOL_FIELDS:
        if v is None: return False
        if isinstance(v, str):                      # TEXT 列存过的 0/'false' 也能正确解析
            return v.strip().lower() not in ("", "0", "false", "none", "null")
        return bool(v)
    return v


def _norm_in(col, v):
    """上层值 → SQLite：datetime→ISO(CST)、bool→0/1、None→NULL。"""
    if v is None: return None
    if col in DATE_FIELDS and isinstance(v, datetime.datetime):
        v = v if v.tzinfo else v.replace(tzinfo=CST)
        return v.astimezone(CST).isoformat()
    if col in BOOL_FIELDS: return 1 if v else 0
    return v


def _row_to_record(table, r):
    cols = {"Deliveries": MAIN_COLS, "MailArchive": MAIL_COLS, "Exams": EXAM_COLS}.get(table)
    keys = cols if cols else [k for k in r.keys() if k != "id"]
    return {"id": r["id"], "fields": {k: _norm_out(k, r[k]) for k in keys}}


# ---------------- 基础 CRUD ----------------
def list_records(table, filter=None):
    with _LOCK:
        _ensure_table(table)
        sql, args = f'SELECT * FROM "{table}"', []
        if filter:
            conds = []
            for k, vals in filter.items():
                col = "id" if k == "id" else k
                vals = vals if isinstance(vals, (list, tuple)) else [vals]
                ph = ",".join("?" * len(vals))
                conds.append(f'"{col}" IN ({ph})')
                args += [_norm_in(col, v) if col != "id" else v for v in vals]
            if conds: sql += " WHERE " + " AND ".join(conds)
        rows = _conn().execute(sql + ' ORDER BY "id"', args).fetchall()
        return [_row_to_record(table, r) for r in rows]


def _ensure_cols(table, fields):
    """字段不在表里时自动加列（TEXT）——对齐 Grist 可随时加列的行为。"""
    cols = {r[1] for r in _conn().execute(f'PRAGMA table_info("{table}")')}
    for k, v in fields.items():
        if k != "id" and k not in cols:
            t = "INTEGER" if (isinstance(v, int) and not isinstance(v, bool)) or k in REF_COLS else "TEXT"
            _conn().execute(f'ALTER TABLE "{table}" ADD COLUMN "{k}" {t}')
            if table == T_MAIN and k not in MAIN_COLS: MAIN_COLS.append(k)
            if table == T_MAIL and k not in MAIL_COLS: MAIL_COLS.append(k)
            if table == T_EXAM and k not in EXAM_COLS: EXAM_COLS.append(k)
    _conn().commit()


def add_record(table, fields):
    with _LOCK:
        _ensure_table(table)
        _ensure_cols(table, fields)
        items = {k: _norm_in(k, v) for k, v in fields.items() if v is not None}
        cols = ", ".join(f'"{k}"' for k in items)
        ph = ", ".join("?" * len(items))
        cur = _conn().execute(f'INSERT INTO "{table}" ({cols}) VALUES ({ph})', list(items.values()))
        _conn().commit()
        rid = items.get("id") or cur.lastrowid
        return {"id": rid, "fields": {k: _norm_out(k, v) for k, v in items.items()}}


def update_record(table, rid, fields, clear_none=False):
    """按行更新；clear_none=True 时把显式 None 写为空（与 Grist 版语义一致）。返回合并后记录。"""
    with _LOCK:
        _ensure_table(table)
        _ensure_cols(table, fields)
        sets, args = [], []
        for k, v in fields.items():
            if k == "id": continue
            if v is None and not clear_none: continue
            sets.append(f'"{k}"=?'); args.append(_norm_in(k, v))
        if sets:
            _conn().execute(f'UPDATE "{table}" SET {", ".join(sets)} WHERE "id"=?', args + [rid])
            _conn().commit()
        rows = _conn().execute(f'SELECT * FROM "{table}" WHERE "id"=?', (rid,)).fetchall()
        return _row_to_record(table, rows[0]) if rows else {"id": rid, "fields": {}}


def delete_record(table, rid):
    with _LOCK:
        _ensure_table(table)
        _conn().execute(f'DELETE FROM "{table}" WHERE "id"=?', (rid,))
        _conn().commit()


def _get_row(table, rid):
    rows = list_records(table, filter={"id": [rid]})
    return rows[0] if rows else None


def column_names(table):
    _ensure_table(table)
    cols = {"Deliveries": MAIN_COLS, "MailArchive": MAIL_COLS, "Exams": EXAM_COLS}.get(table)
    return cols if cols else ["日期", "投递总数", "简历评估", "测评中", "笔试中", "面试中", "已通过", "终止"]


# ---------------- 独立键：公司+岗位=志愿 ----------------
def list_by_company(公司):
    return list_records(T_MAIN, filter={"公司名称": [公司]})


def _pick_active(rows):
    """多行消歧：非终态优先 → 投递日期最新 → 行 id 最大。"""
    if not rows: return None
    def key(r):
        f = r["fields"]
        terminal = 1 if str(f.get("投递状态") or "") in TERMINAL else 0
        d = f.get("投递日期")
        ts = 0.0
        if isinstance(d, datetime.datetime):
            ts = (d if d.tzinfo else d.replace(tzinfo=CST)).timestamp()
        return (terminal, -ts, -r["id"])
    return sorted(rows, key=key)[0]


def get_active_record(公司):
    return _pick_active(list_by_company(公司))


def find_record(公司, 岗位):
    rows = [r for r in list_by_company(公司)
            if str(r["fields"].get("岗位名称") or "") == str(岗位 or "")]
    return _pick_active(rows)


def get_by_company(公司):
    return get_active_record(公司)


def companies():
    return {r["fields"]["公司名称"]: r["id"] for r in list_records(T_MAIN) if r["fields"].get("公司名称")}


# ---------------- 考试安排（Exams） ----------------
def _exam_start_naive(v):
    if isinstance(v, datetime.datetime): return v.replace(tzinfo=None)
    if isinstance(v, datetime.date): return datetime.datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        return datetime.datetime.fromtimestamp(v / 1000, CST).replace(tzinfo=None)
    try: return datetime.datetime.fromisoformat(str(v))
    except ValueError: return v


def add_exam(记录id, 考试类型, 开始, 结束=None, 链接="", 来源主题="", done=False, done_at=None, 按链去重=True):
    """幂等：①同记录+类型+开始 ②同记录+类型+非空链接；③已完成吸收（同公司同类型已有已完成行、
    新窗口开始≤今天 → 视为同一考试的重复/提醒邮件，不再建未勾新行）。撞键只补空字段。"""
    st = _exam_start_naive(开始)
    mine = list_records(T_EXAM, filter={"公司": [记录id]})
    for x in mine:
        f = x["fields"]
        same_key = str(f.get("考试类型") or "") == 考试类型 and _exam_start_naive(f.get("开始时间")) == st
        same_link = 按链去重 and bool(链接) and str(f.get("考试链接") or "") == str(链接) \
            and str(f.get("考试类型") or "") == 考试类型
        if same_key or same_link:
            patch = {}
            if 结束 and not f.get("结束时间"): patch["结束时间"] = 结束
            if 链接 and not f.get("考试链接"): patch["考试链接"] = 链接
            if patch: update_record(T_EXAM, x["id"], patch)
            return _get_row(T_EXAM, x["id"]) or x, False
    # 默认提醒唯一（2026-09-24 用户定版）：同记录+同类型最多一条“无结束提醒行”——
    # 本质上是一条考试记录，重复提醒邮件不新建；新邮件带真实窗口时升级已有行
    if 考试类型 in ("笔试", "测评", "其他事项"):
        for x in mine:
            f = x["fields"]
            if str(f.get("考试类型") or "") == 考试类型 and not f.get("结束时间"):
                if not 结束:
                    return x, False                       # 仍是默认提醒 → 不新建
                update_record(T_EXAM, x["id"], {"开始时间": 开始, "结束时间": 结束})
                return _get_row(T_EXAM, x["id"]) or x, False   # 升级为真实窗口
    if not done and 考试类型 in ("测评", "笔试") and st.date() <= datetime.datetime.now(CST).date():
        co = _get_row(T_MAIN, 记录id)
        co_name = (co or {}).get("fields", {}).get("公司名称")
        if co_name:
            sib_ids = {r["id"] for r in list_records(T_MAIN, filter={"公司名称": [co_name]})}
            for x in list_records(T_EXAM):
                f = x["fields"]
                if f.get("完成") is True and f.get("公司") in sib_ids \
                        and str(f.get("考试类型") or "") == 考试类型:
                    # 已完成吸收（2026-09-24 定版）。2026-10-08 修：新窗口截止晚于已完成行
                    # → 是新一轮考试而非旧考试的提醒，不吸收（平安银行 AI 面试截止
                    # 10-13 晚于已完成测评的 10-11，曾被当重复提醒静默吞掉）
                    new_end = _exam_start_naive(结束) if 结束 else None
                    old_end = _exam_start_naive(f.get("结束时间"))
                    newer_round = isinstance(new_end, datetime.datetime) \
                        and isinstance(old_end, datetime.datetime) and new_end > old_end
                    if not newer_round:
                        return x, False
                    # 更晚的已完成行可能还有，继续找；都找不到才真正建新行
    rec = add_record(T_EXAM, {"公司": 记录id, "考试类型": 考试类型, "开始时间": 开始,
                              "结束时间": 结束, "考试链接": 链接 or "", "完成": bool(done),
                              "完成时间": (done_at or datetime.datetime.now(CST)) if done else None,
                              "来源主题": 来源主题 or ""})
    return rec, True


def exams_of(记录id):
    rows = list_records(T_EXAM, filter={"公司": [记录id]})
    rows.sort(key=lambda x: str(x["fields"].get("开始时间") or ""), reverse=True)
    return rows


def migrate_exams_from_main():
    """旧主表时间字段 → Exams 行（幂等）。SQLite 版保留以兼容部署脚本。"""
    n_add = n_dup = 0
    for it in list_records(T_MAIN):
        f = it["fields"]
        link = f.get("考试链接") if isinstance(f.get("考试链接"), str) else ""
        for fs, fe, fd, typ in (("笔试时间", "笔试结束", "笔试完成", "笔试"),
                                ("面试时间", None, "面试完成", "面试"),
                                ("测评截止", None, "测评完成", "测评")):
            s = f.get(fs)
            if not isinstance(s, datetime.datetime) and not (isinstance(s, str) and s.strip()):
                continue
            e = f.get(fe) if fe else None
            _, created = add_exam(it["id"], typ, s,
                                  结束=e if isinstance(e, datetime.datetime) else None,
                                  链接=link or "", 来源主题="存量迁移",
                                  done=f.get(fd) is True,
                                  done_at=datetime.datetime.now(CST) if f.get(fd) is True else None)
            n_add, n_dup = (n_add + 1, n_dup) if created else (n_add, n_dup + 1)
    return n_add, n_dup


# ---------------- 邮件存档 ----------------
def add_archive(公司, 收信时间, 主题, 正文, cap_body=6000):
    main = get_by_company(公司)
    if not main: return False
    t = (收信时间 if isinstance(收信时间, datetime.datetime)
         else datetime.datetime.now(CST)).replace(tzinfo=None, microsecond=0)
    old = list_records(T_MAIL, filter={"公司": [main["id"]]})
    for x in old:
        fx = x["fields"]
        if fx.get("主题") == 主题 and isinstance(fx.get("收信时间"), datetime.datetime) \
                and fx["收信时间"].replace(tzinfo=None) == t:
            return False
    add_record(T_MAIL, {"公司": main["id"], "收信时间": t, "主题": 主题, "正文": (正文 or "")[:cap_body]})
    return True


def archives_of(公司):
    main = get_by_company(公司)
    if not main: return []
    rows = list_records(T_MAIL, filter={"公司": [main["id"]]})
    rows.sort(key=lambda x: str(x["fields"].get("收信时间")), reverse=True)
    return rows


# ---------------- 状态快照 ----------------
def ensure_snaps():
    _ensure_table(T_SNAP)


def _raw_add(table, fields):
    return add_record(table, fields)


def _raw_update(table, rid, fields):
    update_record(table, rid, fields)


def snap_date_str(v):
    if v is None: return ""
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())          # 迁移来的 epoch 秒字符串（Grist Date 列形状）
    if isinstance(v, (int, float)):
        try: return datetime.datetime.fromtimestamp(v, CST).strftime("%Y-%m-%d")
        except Exception: return str(v)
    return str(v)[:10]


def snapshot_status():
    """每日状态快照（漏斗历史）：按日期 upsert，幂等。"""
    ensure_snaps()
    m = {"简历评估": "s1", "测评中": "s2", "笔试中": "s3", "面试中": "s4", "已通过": "s5", "终止": "s6"}
    cnt = {"total": 0, "s1": 0, "s2": 0, "s3": 0, "s4": 0, "s5": 0, "s6": 0}
    for r in list_records(T_MAIN):
        cnt["total"] += 1
        st = str((r.get("fields") or {}).get("投递状态") or "")
        if st in m: cnt[m[st]] += 1
    dstr = datetime.datetime.now(CST).strftime("%Y-%m-%d")
    matches = [r for r in list_records(T_SNAP) if snap_date_str(r.get("fields", {}).get("d")) == dstr]
    if matches:
        for r in matches:
            _raw_update(T_SNAP, r["id"], cnt)
        return matches[0]["id"], False
    rec = _raw_add(T_SNAP, {"d": dstr, **cnt})
    return rec["id"], True


# ---------------- 建表 / 备份 ----------------
def create_tables(columns_main=None, columns_mail=None, columns_exam=None):
    """兼容部署脚本签名；SQLite 按内置 schema 建表，忽略 Grist 列定义。"""
    for t in (T_MAIN, T_MAIL, T_EXAM, T_SNAP):
        _ensure_table(t)
    return {t: True for t in (T_MAIN, T_MAIL, T_EXAM)}


def download_backup(dest_path):
    """备份 = sqlite3 backup API 在线一致性快照（修复：先 checkpoint 再裸 copy，
    拷贝期间另一进程写入会产生撕裂备份；相对路径时 dirname 为空也会崩）。"""
    dest = os.path.abspath(dest_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with _LOCK:
        dst = sqlite3.connect(dest)
        try:
            _conn().backup(dst)
        finally:
            dst.close()
    return dest


def backup_now(keep=30):
    """每轮同步后的自动备份（2026-10-02：原 download_backup 无任何调用者，等于从不备份）：
    在线一致性快照到 备份目录（config「备份目录」或 <本目录>/backups/），滚动保留最近 keep 份。"""
    bdir = str(get_config().get("备份目录") or "").strip() or os.path.join(BASE, "backups")
    os.makedirs(bdir, exist_ok=True)
    name = f"中台-{datetime.datetime.now(CST):%Y%m%d-%H%M%S}.sqlite"
    dest = download_backup(os.path.join(bdir, name))
    baks = sorted((f for f in os.listdir(bdir) if f.endswith(".sqlite")),
                  key=lambda f: os.path.getmtime(os.path.join(bdir, f)), reverse=True)
    for old in baks[keep:]:
        try:
            os.unlink(os.path.join(bdir, old))
        except OSError:
            pass
    return dest
