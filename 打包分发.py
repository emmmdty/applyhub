#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打包「绿色版」分发包（给别人用）：
只收代码 / 文档 / 静态资源 / 示例配置，自动排除隐私与运行时文件。
用法:  python3 打包分发.py              # 产物: dist/投递中台-绿色版-YYYYMMDD.zip
       python3 打包分发.py 1.1.0        # 产物: dist/投递中台-v1.1.0.zip（语义版本，供 Release）
收包人只需: 解压 → 双击 启动中台.bat（Windows）→ 浏览器自动打开 → 「设置」页填邮箱。
"""
import os, sys, zipfile, datetime

HERE = os.path.dirname(os.path.abspath(__file__))

# 显式清单：宁可漏、不可多（多=可能带出隐私）
FILES = [
    # 核心运行
    "中台看板.py", "grist_store.py", "mail.py", "sync.py", "看板.html", "favicon.svg",
    # 双击启动器
    "启动中台.bat", "停止中台.bat", "启动中台.sh",
    # 脚本
    "sync_pipeline.sh", "自启看板.sh",
    # 配置（示例；真实 config.json/凭据.json 一律不带）
    "config.example.json",
    # uv 项目定义（零依赖；未来加依赖时 uv 自动走测速镜像安装）
    "pyproject.toml", "uv.toml",
    # 文档（交接-UI升级.md 为本机内部交接文档，含个人上下文，不入分发包）
    "README.md", "使用说明书.md", "使用说明书.pdf",
    # 测试与工具
    "验收测试.py", "功能测试.py",
    "迁移Grist到SQLite-一次性工具-重跑会覆盖SQLite数据.py",
    "打包分发.py",
]
DIRS = [("assets", ("png", "svg"))]          # 静态资源按扩展名收
ALL_DIRS = ["tools/runtime"]                 # 内置离线 Python 运行时（全收，Windows 嵌入式版）

OUT_DIR = os.path.join(HERE, "dist")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    ver = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lstrip("v")
    if ver:   # 语义版本（发布 Release 用）
        dest = os.path.join(OUT_DIR, f"投递中台-v{ver}.zip")
    else:     # 沿用旧命名：按日期
        tag = datetime.date.today().strftime("%Y%m%d")
        dest = os.path.join(OUT_DIR, f"投递中台-绿色版-{tag}.zip")
    n = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            p = os.path.join(HERE, f)
            if os.path.exists(p):
                z.write(p, f"ApplyHub/{f}")
                n += 1
            else:
                print(f"  ⚠ 清单中缺少（跳过）: {f}")
        for d, exts in DIRS:
            dp = os.path.join(HERE, d)
            if not os.path.isdir(dp):
                continue
            for root, _, files in os.walk(dp):   # 递归收（2026-10-02 修：原 listdir 只收顶层，
                for fn in sorted(files):         # assets/manual/ 5 张说明书截图全部漏包，md 版说明书图文全裂）
                    if fn.lower().endswith(tuple(exts)):
                        full = os.path.join(root, fn)
                        z.write(full, f"ApplyHub/{os.path.relpath(full, HERE)}")
                        n += 1
        for d in ALL_DIRS:
            dp = os.path.join(HERE, d)
            if not os.path.isdir(dp):
                print(f"  ⚠ 内置运行时缺失（跳过）: {d} —— 收包人将需要联网/自装 Python")
                continue
            for root, _, files in os.walk(dp):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, HERE)
                    z.write(full, f"ApplyHub/{rel}")
                    n += 1
    size = os.path.getsize(dest) / 1024
    print(f"✓ 已打包 {n} 个文件 → {dest}（{size:.0f} KB）")
    print("  收包人步骤：解压 → 双击「启动中台.bat」（Windows）或 bash 启动中台.sh → 浏览器自动打开")
    print("              → 「设置」页填邮箱 → 建第一条投递。全程不用命令行。")
    print("  注意：包里没有 凭据.json / config.json / 数据库 —— 这正是为了不带出你的隐私")


if __name__ == "__main__":
    main()
