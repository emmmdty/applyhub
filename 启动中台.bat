@echo off
setlocal
chcp 936 >nul
cd /d "%~dp0"
if not exist "中台看板.py" (
  echo [提示] 没找到 中台看板.py —— 你可能是直接在压缩包里双击的。
  echo 请先把整个压缩包解压到一个文件夹（右键-全部解压缩），再进入文件夹双击本文件。
  pause
  exit /b 1
)
if not exist "看板.html" (
  echo [提示] 看板.html 缺失，压缩包可能没解压完整，请重新解压后再试。
  pause
  exit /b 1
)
title 秋招中台
set "PYTHONUTF8=1"
set "PY="
set "UV="
set "IDX="
set "PORT=8790"

rem ============ 1) 系统 Python ≥3.9（老用户：直接用，零操作） ============
call :find_py
if defined PY goto :have_py

rem ============ 2) 项目内 .venv（之前 uv 建过） ============
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe" & goto :have_py)
if exist ".venv\bin\python.exe"    (set "PY=.venv\bin\python.exe"    & goto :have_py)

rem ============ 3) 内置离线运行时（无需联网 / 无需安装 Python） ============
if exist "tools\runtime\python.exe" (set "PY=tools\runtime\python.exe" & goto :have_py)

rem ============ 4) uv 管理环境（系统 uv → 项目内 uv → 在线下载） ============
call :find_uv
if defined UV goto :have_uv
echo [秋招中台] 未检测到 Python / 内置运行时 / uv，尝试在线获取 uv（多镜像测速）…
call :fetch_uv
if defined UV goto :have_uv

echo [秋招中台] 无法自动准备环境：本机无 Python、无内置运行时（tools\runtime\），且下载 uv 失败。
echo             任选其一解决： ①联网后重新双击  ②安装 Python 3.9+ 并勾选 Add to PATH
pause
exit /b 1

rem ============ 有解释器：读端口 → 启动 ============
:have_py
call :read_port
echo [秋招中台] 解释器: %PY%
goto :launch

rem ============ uv：镜像测速 → sync → 启动 ============
:have_uv
call :pick_index
set "UV_DEFAULT_INDEX=%IDX%"
echo [秋招中台] 使用 uv 管理环境（已测速选择镜像: %IDX%）
"%UV%" sync --frozen 1>nul 2>&1 || "%UV%" sync 1>nul 2>&1
if exist ".venv\Scripts\python.exe" (set "PY=.venv\Scripts\python.exe" & goto :have_py)
echo [秋招中台] uv 环境准备失败，请检查网络后重试。
pause
exit /b 1

rem ============ 启动 ============
:launch
%PY% -c "import sys; sys.path.insert(0,'.'); import urllib.request; urllib.request.urlopen('http://127.0.0.1:%PORT%/ok', timeout=1)" >nul 2>&1
if not errorlevel 1 goto :already
if defined ZQ_DRYRUN echo [dryrun] PY=%PY% UV=%UV% IDX=%IDX% PORT=%PORT% & exit /b 0
echo ============================================================
echo   秋招中台 启动中…  浏览器将自动打开
echo   保持本窗口开着 = 看板运行中；关闭本窗口 = 停止看板
echo ============================================================
%PY% 中台看板.py --open
echo.
echo [秋招中台] 看板已停止。
pause
exit /b 0

:already
echo [秋招中台] 看板已在运行，直接打开网页 → http://127.0.0.1:%PORT%/
%PY% -c "import webbrowser; webbrowser.open('http://127.0.0.1:%PORT%/')"
ping -n 3 127.0.0.1 >nul
exit /b 0

rem ---------- 子过程：找系统 Python（≥3.9） ----------
:find_py
call :try_py "py -3"
if defined PY goto :eof
call :try_py "python"
if defined PY goto :eof
call :try_py "python3"
goto :eof

:try_py
%~1 --version >nul 2>&1 || goto :eof
set "V="
for /f "tokens=2 usebackq" %%v in (`%~1 --version 2^>^&1`) do set "V=%%v"
if not defined V goto :eof
set "VMAJ=" & set "VMIN="
for /f "tokens=1,2 delims=." %%a in ("%V%") do (
  set /a VMAJ=%%a 2>nul
  set /a VMIN=%%b 2>nul
)
if not defined VMAJ goto :eof
if %VMAJ% GTR 3 (set "PY=%~1" & goto :eof)
if %VMAJ% EQU 3 if %VMIN% GEQ 9 (set "PY=%~1" & goto :eof)
goto :eof

rem ---------- 子过程：找 uv ----------
:find_uv
where uv >nul 2>&1 && (set "UV=uv" & goto :eof)
if exist "tools\uv.exe" (set "UV=tools\uv.exe" & goto :eof)
goto :eof

rem ---------- 子过程：PyPI 镜像测速（下载响应最快的源） ----------
:pick_index
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "$u=@('https://pypi.tuna.tsinghua.edu.cn/simple/','https://mirrors.aliyun.com/pypi/simple/','https://mirrors.cloud.tencent.com/pypi/simple/','https://pypi.mirrors.ustc.edu.cn/simple/','https://pypi.org/simple/'); $b=''; $bt=[double]::MaxValue; foreach($x in $u){try{$t=(Measure-Command{Invoke-WebRequest -UseBasicParsing -Uri $x -TimeoutSec 4 -Method Head}).TotalSeconds}catch{$t=99}; if($t -lt $bt){$bt=$t;$b=$x}}; Write-Output $b"`) do set "IDX=%%i"
if not defined IDX set "IDX=https://pypi.tuna.tsinghua.edu.cn/simple"
goto :eof

rem ---------- 子过程：在线下载 uv（镜像测速：国内加速在前） ----------
:fetch_uv
if not exist "tools" mkdir "tools"
set "UVZIP=%TEMP%\zq_uv.zip"
for /f "delims=" %%r in (`powershell -NoProfile -Command "$u=@('https://ghproxy.net/https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip','https://gh-proxy.com/https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip','https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'); foreach($x in $u){try{Invoke-WebRequest -UseBasicParsing -Uri $x -OutFile $env:UVZIP -TimeoutSec 90; Write-Output OK; break}catch{Write-Output ('fail')}}"`) do set "UVOK=%%r"
if not defined UVOK goto :eof
if not exist "%UVZIP%" goto :eof
powershell -NoProfile -Command "Expand-Archive -Force -Path $env:UVZIP -DestinationPath $env:TEMP\zq_uv" >nul 2>&1
if exist "%TEMP%\zq_uv\uv.exe" move /y "%TEMP%\zq_uv\uv.exe" "tools\uv.exe" >nul
if exist "%TEMP%\zq_uv\uv-x86_64-pc-windows-msvc.exe" move /y "%TEMP%\zq_uv\uv-x86_64-pc-windows-msvc.exe" "tools\uv.exe" >nul
if exist "tools\uv.exe" set "UV=tools\uv.exe"
goto :eof

rem ---------- 子过程：读端口 ----------
:read_port
set "PORT=8790"
for /f "usebackq delims=" %%i in (`%PY% -c "import sys; sys.path.insert(0,'.'); import grist_store as g; print(g.get_config().get('端口') or 8790)" 2^>nul`) do set "PORT=%%i"
goto :eof
