@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\Users\Suda\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo 启动 Sound Sniffer...
"%PY%" app.py

pause
