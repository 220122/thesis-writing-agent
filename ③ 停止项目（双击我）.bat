@echo off
rem ============================================================
rem  ③ 停止项目（双击我）
rem  结束正在运行的网页应用（按 PID 精确结束，不影响其它程序）
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1" %*
echo.
pause
endlocal
