<#
    毕业论文写作智能体助手 —— ② 启动项目
    ------------------------------------------------------------
    1) 找到可用的运行环境（.python_path → .venv → 系统 Python）
    2) 检查依赖是否装好（没装会提示先运行 ① 首次安装）
    3) 自动挑选空闲端口（默认 8501）并启动，浏览器自动打开
    4) 把进程号写入 .run\app.pid，便于 ③ 停止项目 精确结束

    用法：
      powershell -ExecutionPolicy Bypass -File scripts\start.ps1
      powershell ... -File scripts\start.ps1 -Port 8600     # 指定端口
      powershell ... -File scripts\start.ps1 -NoBrowser     # 不自动打开浏览器
#>
param(
    [int]$Port = 0,
    [switch]$NoBrowser,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
try { $Host.UI.RawUI.WindowTitle = "毕业论文写作智能体助手 - 运行中（关闭本窗口即停止）" } catch { }

function Write-Step { param([string]$Text) Write-Host ""; Write-Host (">> " + $Text) -ForegroundColor Yellow }
function Write-Ok   { param([string]$Text) Write-Host ("   [OK] " + $Text) -ForegroundColor Green }
function Write-Note { param([string]$Text) Write-Host ("   [i]  " + $Text) -ForegroundColor Gray }
function Write-Warn2 { param([string]$Text) Write-Host ("   [!]  " + $Text) -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host ("   [X]  " + $Text) -ForegroundColor Red }

function Test-Deps {
    param([string]$Py)
    if (-not (Test-Path -LiteralPath $Py)) { return $false }
    & $Py -c "import streamlit, openai, pandas" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-PortFree {
    param([int]$P)
    try {
        $conn = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop
        if ($conn) { return $false }
        return $true
    } catch { }
    try {
        $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $P)
        $l.Start(); $l.Stop()
        return $true
    } catch { return $false }
}

Write-Host ""
Write-Host ("=" * 68) -ForegroundColor DarkCyan
Write-Host "  毕业论文写作智能体助手 · 启动" -ForegroundColor Cyan
Write-Host ("=" * 68) -ForegroundColor DarkCyan

# ---------- 1) 运行环境 ----------
Write-Step "第 1 步 / 共 3 步：定位运行环境"
$interp = ""
$pathFile = Join-Path $Root ".python_path"
if (Test-Path -LiteralPath $pathFile) {
    $p = ([System.IO.File]::ReadAllText($pathFile)).Trim()
    if ($p -and (Test-Path -LiteralPath $p)) { $interp = $p }
}
if (-not $interp) {
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) { $interp = $venvPy }
}
if (-not $interp) {
    foreach ($t in @(@("py", @("-3.12")), @("py", @("-3.11")), @("py", @("-3.10")), @("py", @("-3.9")), @("python", @()))) {
        try {
            $out = & $t[0] @($t[1]) -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $interp = (@($out))[0].Trim(); break }
        } catch { }
    }
}

if (-not $interp) {
    Write-Bad "未找到可用的运行环境。请先双击 ① 首次安装（双击我）.bat"
    exit 1
}
Write-Ok ("运行环境：" + $interp)

# ---------- 2) 依赖与 Key ----------
Write-Step "第 2 步 / 共 3 步：检查依赖与 API Key"
if (-not (Test-Deps -Py $interp)) {
    Write-Bad "当前运行环境缺少依赖（streamlit / openai / pandas）。"
    Write-Host "       请先双击 ① 首次安装（双击我）.bat 完成安装。"
    exit 1
}
Write-Ok "依赖检查通过"

$hasKey = $false
if ($env:DEEPSEEK_API_KEY) { $hasKey = $true }
$envFile = Join-Path $Root ".env"
if (-not $hasKey -and (Test-Path -LiteralPath $envFile)) {
    if ([System.IO.File]::ReadAllText($envFile) -match 'DEEPSEEK_API_KEY\s*=\s*sk-\S+') { $hasKey = $true }
}
if ($hasKey) { Write-Ok "已检测到 DeepSeek API Key（模型 deepseek-chat）" }
else { Write-Warn2 "未检测到 API Key → 将以【离线演示模式】运行（本地模板模拟输出，流程可完整体验）" }

# ---------- 3) 端口与启动 ----------
Write-Step "第 3 步 / 共 3 步：启动本地服务"
$pidFile = Join-Path $Root ".run\app.pid"
if (Test-Path -LiteralPath $pidFile) {
    $oldTxt = ([System.IO.File]::ReadAllText($pidFile)).Trim()
    $oldPid = 0
    if ([int]::TryParse($oldTxt, [ref]$oldPid)) {
        $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($oldProc) {
            Write-Warn2 ("项目似乎已在运行（进程号 " + $oldPid + "）。")
            Write-Note "如需重启：先双击 ③ 停止项目（双击我）.bat，再运行本脚本。"
            exit 0
        }
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
}

$chosen = 0
if ($Port -gt 0) {
    if (-not (Test-PortFree -P $Port)) {
        Write-Bad ("端口 " + $Port + " 已被占用。可换端口：② 启动项目（双击我）.bat -Port 8600")
        exit 1
    }
    $chosen = $Port
} else {
    foreach ($p in @(8501, 8502, 8503, 8510, 8520)) {
        if (Test-PortFree -P $p) { $chosen = $p; break }
    }
    if ($chosen -eq 0) {
        Write-Bad "8501 / 8502 / 8503 / 8510 / 8520 均被占用，请指定端口：② 启动项目（双击我）.bat -Port 8600"
        exit 1
    }
}
if ($chosen -ne 8501) { Write-Note ("8501 不可用，已自动改用端口 " + $chosen) }

# Streamlit 首次运行会交互询问邮箱（非交互环境下会直接退出），这里预先写入空邮箱配置
try {
    $stCfgDir = Join-Path $env:USERPROFILE ".streamlit"
    $credFile = Join-Path $stCfgDir "credentials.toml"
    if (-not (Test-Path -LiteralPath $credFile)) {
        New-Item -ItemType Directory -Force -Path $stCfgDir | Out-Null
        [System.IO.File]::WriteAllText($credFile, "[general]`r`nemail = `"`"`r`n", (New-Object System.Text.UTF8Encoding($false)))
    }
} catch {
    Write-Warn2 "无法写入 Streamlit 配置（可忽略，必要时用 -NoBrowser 参数启动）"
}

$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path (Join-Path $Root ".run") | Out-Null
$streamlitArgs = @("-m", "streamlit", "run", "app.py",
                   "--server.port", "$chosen",
                   "--server.address", "127.0.0.1",
                   "--browser.gatherUsageStats", "false")
if ($NoBrowser) { $streamlitArgs += @("--server.headless", "true") }
else { $streamlitArgs += @("--server.headless", "false") }

$proc = Start-Process -FilePath $interp -ArgumentList $streamlitArgs -WorkingDirectory $Root -NoNewWindow -PassThru
Set-Content -LiteralPath $pidFile -Value $proc.Id -Encoding ASCII
Start-Sleep -Seconds 5
if ($proc.HasExited) {
    Write-Bad "启动失败（进程已退出），请看上面的报错信息。"
    Write-Host "   常见处理办法："
    Write-Host "   1) 若上面出现 Welcome to Streamlit / Email 提示 —— 首次运行配置没写成，"
    Write-Host "      改用不自动打开浏览器的模式重试：② 启动项目（双击我）.bat -NoBrowser"
    Write-Host "   2) 若提示端口被占用：② 启动项目（双击我）.bat -Port 8600"
    Write-Host "   3) 若提示缺少依赖：先双击 ① 首次安装（双击我）.bat"
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    exit 1
}

Write-Ok ("已启动：http://localhost:" + $chosen)
Write-Note "浏览器若未自动打开，请手动访问上面的地址"
Write-Note "本窗口保持打开即可；停止服务请双击 ③ 停止项目（双击我）.bat，或直接关闭本窗口"

# 等待服务结束：用轮询而不是 Wait-Process，避免个别环境下拿不到进程句柄而报错
while (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }
Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
Write-Note "服务已停止。"
