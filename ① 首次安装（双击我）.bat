@echo off
rem ============================================================
rem  ① 首次安装（双击我）
rem  安装依赖 / 检测 Python / 引导填写 DeepSeek API Key
rem  实际逻辑在 scripts\install.ps1（便于处理中文、错误与 .env）
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
pause
endlocal
