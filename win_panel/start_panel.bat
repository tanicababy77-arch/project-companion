@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [x] 没找到 Python。请先装 Python 3.9+：https://www.python.org/downloads/
  echo     安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
  pause
  exit /b 1
)

echo 正在启动看板（这个黑窗口别关，关了看板就停）...
start "" python project_panel.py --port 8899
timeout /t 3 >nul
start "" http://127.0.0.1:8899
