#!/bin/bash
# 秋招中台双击启动器（macOS/Linux）：macOS 可改名为 启动中台.command 后双击运行。
# 环境检测顺序：系统 python3 ≥3.9 → 项目 .venv → uv（自动建环境）→ 报错指引。
# （内置离线运行时 tools/runtime/ 仅含 Windows 版；mac/Linux 用户系统一般自带 python3）
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1

PY=""
ver_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; }

if ver_ok python3; then PY=python3
elif [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v uv >/dev/null 2>&1; then
  echo "[秋招中台] 使用 uv 管理环境…"
  uv sync --frozen >/dev/null 2>&1 || uv sync || { echo "uv 环境准备失败，请检查网络"; exit 1; }
  PY=".venv/bin/python"
else
  echo "[秋招中台] 未检测到可用的 Python 3.9+。请安装后重试（macOS: brew install python3 / 官网安装包）。"
  exit 1
fi

PORT="$($PY -c "import grist_store as g; print(g.get_config().get('端口') or 8790)" 2>/dev/null || echo 8790)"

if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${PORT}/ok', timeout=1)" 2>/dev/null; then
  echo "[秋招中台] 看板已在运行，打开网页 → http://127.0.0.1:${PORT}/"
  exec python3 -c "import webbrowser; webbrowser.open('http://127.0.0.1:${PORT}/')"
fi

echo "============================================================"
echo "  秋招中台 启动中…  浏览器将自动打开（Ctrl+C 停止看板）"
echo "============================================================"
exec "$PY" 中台看板.py --open
