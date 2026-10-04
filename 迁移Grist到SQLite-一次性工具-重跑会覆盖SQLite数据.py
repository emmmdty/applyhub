# -*- coding: utf-8 -*-
"""一次性迁移：Grist 4 张表 → 本目录 SQLite（中台.sqlite）。
从旧目录（Grist 版 grist_store.py）读，写入本目录 SQLite，逐表核对行数。
用法: python3 迁移Grist到SQLite.py   （可重复执行=全量覆盖重建，不产生重复行）
"""
import os, sys, datetime, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
OLD = os.path.join(os.path.dirname(HERE), "中台")
sys.path.insert(0, OLD)
import grist_store as gGrist          # 旧目录 = Grist 引擎
os.environ.setdefault("SQLITE_PATH", os.path.join(HERE, "中台.sqlite"))
_spec = importlib.util.spec_from_file_location("grist_store_lite", os.path.join(HERE, "grist_store.py"))
gLite = importlib.util.module_from_spec(_spec)
sys.modules["grist_store_lite"] = gLite
_spec.loader.exec_module(gLite)       # 本目录 = SQLite 引擎（独立模块名避免缓存冲突）

TABLES = [(gGrist.T_MAIN, gLite.T_MAIN), (gGrist.T_MAIL, gLite.T_MAIL),
          (gGrist.T_EXAM, gLite.T_EXAM), (gGrist.T_SNAP, gLite.T_SNAP)]

print("== 1) 从 Grist 读取 ==")
src = {}
for t_src, _ in TABLES:
    src[t_src] = gGrist.list_records(t_src)
    print(f"  {t_src}: {len(src[t_src])} 行")

print("== 2) 重建 SQLite 表 ==")
with gLite._LOCK:
    for _, t_dst in TABLES:
        gLite._ensure_table(t_dst)
        gLite._conn().execute(f'DELETE FROM "{t_dst}"')
    gLite._conn().commit()

print("== 3) 写入（保留原行 id，引用关系不断裂） ==")
n = {}
for t_src, t_dst in TABLES:
    cnt = 0
    for r in src[t_src]:
        f = dict(r["fields"])
        for k, v in list(f.items()):
            if isinstance(v, str) and k in gLite.DATE_FIELDS:
                try: f[k] = datetime.datetime.fromisoformat(v)
                except ValueError: pass
        if t_dst == gLite.T_SNAP and "d" in f:
            f["d"] = gLite.snap_date_str(f["d"])    # epoch/字符串 → "YYYY-MM-DD"
        gLite.add_record(t_dst, {**f, "id": r["id"]})   # 显式 id
        cnt = max(cnt, r["id"])
    # 自增序列推进到最大 id 之后，避免新行复用旧 id
    gLite._conn().execute(
        f'INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES (?, ?)', (t_dst, cnt))
    gLite._conn().commit()
    n[t_dst] = cnt
    print(f"  {t_dst}: 写入 {len(src[t_src])} 行 (最大 id={cnt})")

print("== 4) 核对 ==")
ok = True
for t_src, t_dst in TABLES:
    back = len(gLite.list_records(t_dst))
    match = back == len(src[t_src])
    ok = ok and match
    print(f"  {t_dst}: Grist={len(src[t_src])} SQLite={back} {'✓' if match else '✗'}")
if not ok:
    sys.exit("✗ 行数不一致，中止")
print("MIGRATE-OK")
