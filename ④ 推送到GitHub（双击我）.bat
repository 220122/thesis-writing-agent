@echo off
rem ============================================================
rem  ④ 推送到GitHub（双击我）
rem  首次会自动 git init 并询问仓库地址；之后每次自动提交并推送
rem  会自动把 .env / data/ 排除在版本控制之外（密钥与数据库不上传）
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\push.ps1" %*
echo.
pause
endlocal
