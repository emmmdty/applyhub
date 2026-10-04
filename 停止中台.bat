@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"
title 停止秋招中台

rem ---------- 找 Python（与 启动中台.bat 同逻辑） ----------
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY ( python --version >nul 2>&1 && set "PY=python" )
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "tools\runtime\python.exe" set "PY=tools\runtime\python.exe"

rem 读端口（config.json，缺省 8790）
set "PORT=8790"
if defined PY for /f "usebackq delims=" %%i in (`%PY% -c "import sys; sys.path.insert(0,'.'); import grist_store as g; print(g.get_config().get('端口') or 8790)" 2^>nul`) do set "PORT=%%i"

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
  if not "%%p"=="0" (
    set "FOUND=1"
    echo [秋招中台] 停止看板进程 pid=%%p（端口 %PORT%）
    taskkill /pid %%p /t /f >nul 2>&1
  )
)
if not defined FOUND (
  echo [秋招中台] 端口 %PORT% 上没有运行中的看板。
)
echo 说明：若是双击「启动中台.bat」开出的窗口，直接关掉那个窗口也可以停止。
pause
