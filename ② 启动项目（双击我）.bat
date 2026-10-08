@echo off
rem ============================================================
rem  ② 启动项目（双击我）
rem  启动本地网页应用并在浏览器打开 http://localhost:8501
rem  停止服务：双击 ③ 停止项目（双击我）.bat，或直接关闭本窗口
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
echo.
pause
endlocal
