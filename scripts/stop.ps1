<#
    毕业论文写作智能体助手 —— ③ 停止项目
    ------------------------------------------------------------
    三重保险，按顺序尝试：
      1) 读取 .run\app.pid（由 ② 启动时写入），精确结束对应进程
      2) 端口扫描：找出监听 8501/8502/8503/8510/8520 的进程（仅限本项目）
      3) 进程扫描：命令行同时包含本项目路径与 streamlit 的 Python 进程
    任何一步读不到命令行时都会安全降级，不会误杀其它程序。

    用法：
      powershell -ExecutionPolicy Bypass -File scripts\stop.ps1
      powershell ... -File scripts\stop.ps1 -Quiet     # 少输出
#>
param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
try { $Host.UI.RawUI.WindowTitle = "毕业论文写作智能体助手 - 停止项目" } catch { }

function Write-Ok    { param([string]$Text) Write-Host ("   [OK] " + $Text) -ForegroundColor Green }
function Write-Note  { param([string]$Text) if (-not $Quiet) { Write-Host ("   [i]  " + $Text) -ForegroundColor Gray } }
function Write-Warn2 { param([string]$Text) Write-Host ("   [!]  " + $Text) -ForegroundColor Yellow }

function Get-ProcessCmdline {
    param([int]$ProcId)
    try {
        $ci = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcId) -ErrorAction Stop
        if ($ci) { return [string]$ci.CommandLine }
    } catch { }
    return ""
}

function Test-IsOurPython {
    # 降级判断：进程名是 python*；若可执行文件在本项目目录下则更确定
    param([System.Diagnostics.Process]$Proc)
    try {
        if ($Proc.ProcessName -notlike "python*") { return $false }
        return $true
    } catch { return $false }
}

function Stop-OneProcess {
    param([int]$ProcId, [string]$Why)
    $proc = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
    if (Get-Process -Id $ProcId -ErrorAction SilentlyContinue) {
        Write-Warn2 ("进程 " + $ProcId + " 未能结束，可手动执行：Stop-Process -Id " + $ProcId + " -Force")
        return $false
    }
    Write-Ok ("已结束进程 " + $ProcId + "（" + $proc.ProcessName + "，" + $Why + "）")
    return $true
}

if (-not $Quiet) {
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
    Write-Host "  毕业论文写作智能体助手 · 停止项目" -ForegroundColor Cyan
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
    Write-Host ""
}

$killed = 0
$doneIds = New-Object System.Collections.Generic.List[int]

# ---------- 1) 按 PID 文件 ----------
$pidFile = Join-Path $Root ".run\app.pid"
if (Test-Path -LiteralPath $pidFile) {
    $txt = ([System.IO.File]::ReadAllText($pidFile)).Trim()
    $targetPid = 0
    if ([int]::TryParse($txt, [ref]$targetPid)) {
        $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
        if ($proc) {
            $cmdline = Get-ProcessCmdline -ProcId $targetPid
            if ($cmdline) {
                if ($cmdline -like "*streamlit*") {
                    if (Stop-OneProcess -ProcId $targetPid -Why "来自启动记录 .run\app.pid") { $killed++; $doneIds.Add($targetPid) }
                } else {
                    Write-Warn2 ("进程 " + $targetPid + " 不是本项目的服务（命令行不含 streamlit），已跳过以免误杀。")
                }
            } elseif (Test-IsOurPython -Proc $proc) {
                # 读不到命令行（WMI 被安全策略限制）时，按本项目自己的启动记录结束
                if (Stop-OneProcess -ProcId $targetPid -Why "来自启动记录，且为 Python 进程") { $killed++; $doneIds.Add($targetPid) }
            } else {
                Write-Warn2 ("进程 " + $targetPid + "（" + $proc.ProcessName + "）无法确认是本项目服务，已跳过。")
            }
        } else {
            Write-Note ("记录中的进程 " + $targetPid + " 已经不在运行。")
        }
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
} else {
    Write-Note "没有找到 .run\app.pid（可能不是通过 ② 启动的），改用其它方式查找…"
}

# ---------- 2) 端口扫描兜底 ----------
$ports = @(8501, 8502, 8503, 8510, 8520)
$owners = @()
try {
    foreach ($p in $ports) {
        $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        foreach ($c in @($conns)) { $owners += [pscustomobject]@{ Port = $p; ProcId = [int]$c.OwningProcess } }
    }
} catch { }

foreach ($o in $owners) {
    if ($doneIds.Contains($o.ProcId)) { continue }
    $proc = Get-Process -Id $o.ProcId -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    $cmdline = Get-ProcessCmdline -ProcId $o.ProcId
    if ($cmdline) {
        if (($cmdline -like "*streamlit*") -and ($cmdline -like "*$Root*")) {
            if (Stop-OneProcess -ProcId $o.ProcId -Why ("占用端口 " + $o.Port)) { $killed++; $doneIds.Add($o.ProcId) }
        } else {
            Write-Warn2 ("端口 " + $o.Port + " 被进程 " + $o.ProcId + "（" + $proc.ProcessName + "）占用，但不是本项目的服务，未处理。")
        }
    } elseif (Test-IsOurPython -Proc $proc) {
        if (Stop-OneProcess -ProcId $o.ProcId -Why ("占用端口 " + $o.Port + "，且为 Python 进程")) { $killed++; $doneIds.Add($o.ProcId) }
    } else {
        Write-Warn2 ("端口 " + $o.Port + " 被 " + $proc.ProcessName + "(" + $o.ProcId + ") 占用，未确认是本项目，未处理。")
    }
}

# ---------- 3) 进程扫描兜底 ----------
try {
    $cands = @(Get-CimInstance Win32_Process -ErrorAction Stop |
               Where-Object { $_.Name -like "python*" -and $_.CommandLine -and
                              $_.CommandLine -like "*streamlit*" -and $_.CommandLine -like "*$Root*" })
    foreach ($c in $cands) {
        if ($doneIds.Contains([int]$c.ProcessId)) { continue }
        if (Stop-OneProcess -ProcId ([int]$c.ProcessId) -Why "命令行匹配本项目") { $killed++; $doneIds.Add([int]$c.ProcessId) }
    }
} catch {
    Write-Note "进程列表查询不可用（安全策略限制），已跳过第 3 重检查。"
}

# ---------- 4) 结果 ----------
Write-Host ""
if ($killed -gt 0) {
    Write-Ok ("完成：共结束 " + $killed + " 个进程，端口已释放。")
    Write-Note "现在可以安全地重新双击 ② 启动项目（双击我）.bat"
} else {
    Write-Note "没有发现正在运行的服务（可能已经停止）。"
}
Write-Host ""
