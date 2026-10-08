<#
    毕业论文写作智能体助手 —— ④ 推送到 GitHub
    ------------------------------------------------------------
    首次运行：git init（分支 main）→ 询问仓库地址 → 提交 → 推送
    之后运行：自动提交本次改动并推送
    安全措施：自动把 .env（密钥）与 data/（数据库）移出版本控制；
              提交前展示改动清单并请你确认

    用法：
      powershell -ExecutionPolicy Bypass -File scripts\push.ps1
      powershell ... -File scripts\push.ps1 -RemoteUrl "https://github.com/用户名/仓库.git"
      powershell ... -File scripts\push.ps1 -Message "完成第三章" -Yes
#>
param(
    [string]$Message = "",
    [string]$RemoteUrl = "",
    [switch]$Yes,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
try { $Host.UI.RawUI.WindowTitle = "毕业论文写作智能体助手 - 推送到 GitHub" } catch { }

function Write-Title { param([string]$Text)
    Write-Host ""
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
    Write-Host ("  " + $Text) -ForegroundColor Cyan
    Write-Host ("=" * 68) -ForegroundColor DarkCyan
}
function Write-Step { param([string]$Text) Write-Host ""; Write-Host (">> " + $Text) -ForegroundColor Yellow }
function Write-Ok   { param([string]$Text) Write-Host ("   [OK] " + $Text) -ForegroundColor Green }
function Write-Note { param([string]$Text) Write-Host ("   [i]  " + $Text) -ForegroundColor Gray }
function Write-Warn2 { param([string]$Text) Write-Host ("   [!]  " + $Text) -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host ("   [X]  " + $Text) -ForegroundColor Red }

function Ask-YesNo {
    param([string]$Question, [string]$Default = "Y")
    if ($Yes) { return $true }
    if ($NonInteractive) { return ($Default -eq "Y") }
    $hint = "[Y/n]"
    if ($Default -ne "Y") { $hint = "[y/N]" }
    while ($true) {
        $ans = Read-Host ("   " + $Question + " " + $hint)
        if ([string]::IsNullOrWhiteSpace($ans)) { return ($Default -eq "Y") }
        if ($ans -match '^[Yy]') { return $true }
        if ($ans -match '^[Nn]') { return $false }
    }
}

Write-Title "毕业论文写作智能体助手 · 推送到 GitHub"

# ---------- 1) 检查 git ----------
Write-Step "第 1 步 / 共 5 步：检查 Git"
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    Write-Bad "未检测到 Git。请先安装 Git for Windows：https://git-scm.com/download/win"
    Write-Note "安装后重新双击本脚本即可（安装时保持默认选项）"
    if (Ask-YesNo "是否现在打开下载页面？" "Y") { Start-Process "https://git-scm.com/download/win" }
    exit 1
}
$gitVersion = (& git --version) -join " "
Write-Ok $gitVersion

# ---------- 2) 初始化仓库 ----------
Write-Step "第 2 步 / 共 5 步：准备本地仓库"
$gitDir = Join-Path $Root ".git"
if (-not (Test-Path -LiteralPath $gitDir)) {
    Write-Note "首次推送：正在初始化本地 Git 仓库…"
    & git init -b main 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        & git init | Out-Null
        & git branch -M main 2>$null | Out-Null
    }
    Write-Ok "已初始化（默认分支 main）"
} else {
    Write-Ok "本地仓库已存在"
}

# 确保 .gitignore 覆盖敏感文件
$ignoreFile = Join-Path $Root ".gitignore"
$ignoreNeed = @("data/", ".env", ".venv/", "__pycache__/", ".run/", ".python_path", ".smoke_data/")
if (-not (Test-Path -LiteralPath $ignoreFile)) {
    [System.IO.File]::WriteAllLines($ignoreFile, $ignoreNeed, (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok "已创建 .gitignore"
} else {
    $cur = [System.IO.File]::ReadAllText($ignoreFile)
    $missing = @()
    foreach ($line in $ignoreNeed) { if ($cur -notmatch [regex]::Escape($line)) { $missing += $line } }
    if ($missing.Count -gt 0) {
        $add = New-Object System.Collections.Generic.List[string]
        foreach ($l in [System.IO.File]::ReadAllLines($ignoreFile)) { $add.Add($l) }
        $add.Add("")
        foreach ($m in $missing) { $add.Add($m) }
        [System.IO.File]::WriteAllLines($ignoreFile, $add, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok ("已补全 .gitignore：" + ($missing -join " "))
    }
}

# 安全：把已经误提交的密钥/数据库移出版本控制
$trackedEnv = & git ls-files -- ".env"
if ($trackedEnv) {
    & git rm --cached --quiet -- ".env" 2>$null | Out-Null
    Write-Warn2 "检测到 .env 已被版本控制，已移出（你的 API Key 不会被推送）"
}
$trackedData = & git ls-files -- "data"
if ($trackedData) {
    & git rm -r --cached --quiet -- "data" 2>$null | Out-Null
    Write-Warn2 "检测到 data/ 已被版本控制，已移出（本地数据库不上传）"
}

# ---------- 3) 远程仓库 ----------
Write-Step "第 3 步 / 共 5 步：配置远程仓库"
$remote = ""
try { $remote = ((& git remote get-url origin 2>$null) -join "").Trim() } catch { }
if (-not $remote) {
    $url = $RemoteUrl
    if (-not $url) {
        Write-Note "请先在 GitHub 网页新建一个空仓库（不要勾选 Add a README file），"
        Write-Note "然后复制它的地址，形如：https://github.com/你的用户名/thesis-agent.git"
        if ($NonInteractive) {
            Write-Bad "非交互模式下需要用 -RemoteUrl 指定仓库地址"
            exit 1
        }
        $url = (Read-Host "   请粘贴仓库地址（直接回车放弃推送）").Trim()
    }
    if (-not $url) {
        Write-Warn2 "未提供仓库地址，已取消推送（本地提交已完成，可稍后再推）。"
        exit 0
    }
    & git remote add origin $url | Out-Null
    $remote = $url
    Write-Ok ("已添加 origin：" + $remote)
} else {
    Write-Ok ("远程仓库：" + $remote)
}

# ---------- 4) 提交 ----------
Write-Step "第 4 步 / 共 5 步：提交改动"
$userName = ((& git config user.name 2>$null) -join "").Trim()
$userMail = ((& git config user.email 2>$null) -join "").Trim()
if (-not $userName -or -not $userMail) {
    Write-Warn2 "尚未配置 Git 提交身份（会作为提交作者显示在 GitHub 上）"
    if ($NonInteractive) {
        if (-not $userName) { $userName = $env:USERNAME }
        if (-not $userMail) { $userMail = $env:USERNAME + "@users.noreply.github.com" }
    } else {
        $in = (Read-Host ("   你的名字/昵称 [" + $env:USERNAME + "]")).Trim()
        if ($in) { $userName = $in } elseif (-not $userName) { $userName = $env:USERNAME }
        $in2 = (Read-Host "   你的邮箱（建议与 GitHub 账号一致）").Trim()
        if ($in2) { $userMail = $in2 } elseif (-not $userMail) { $userMail = $env:USERNAME + "@users.noreply.github.com" }
    }
    if ($userName) { & git config user.name $userName | Out-Null }
    if ($userMail) { & git config user.email $userMail | Out-Null }
    Write-Ok ("提交身份：" + $userName + " <" + $userMail + ">")
}

& git add -A
$status = @(& git status --short)
if ($status.Count -eq 0) {
    Write-Note "没有需要提交的新改动，直接进入推送步骤。"
} else {
    Write-Host ""
    Write-Host "   本次将提交以下改动：" -ForegroundColor Yellow
    $status | Select-Object -First 30 | ForEach-Object { Write-Host ("     " + $_) }
    if ($status.Count -gt 30) { Write-Note ("… 共 " + $status.Count + " 项") }
    Write-Host ""

    if (-not $Message) {
        $defaultMsg = "更新毕业论文写作智能体助手 " + (Get-Date -Format "yyyy-MM-dd HH:mm")
        $Message = $defaultMsg
        if (-not $NonInteractive -and -not $Yes) {
            $in = (Read-Host ("   提交说明 [" + $defaultMsg + "]")).Trim()
            if ($in) { $Message = $in }
        }
    }

    if (Ask-YesNo "确认提交？" "Y") {
        & git commit -m $Message | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Ok ("已提交：" + $Message) }
        else { Write-Warn2 "提交未成功（可能没有实际变化），继续尝试推送…" }
    } else {
        Write-Note "已取消提交与推送。"
        exit 0
    }
}

# ---------- 5) 推送 ----------
Write-Step "第 5 步 / 共 5 步：推送到 GitHub"
$branch = ((& git rev-parse --abbrev-ref HEAD 2>$null) -join "").Trim()
if (-not $branch -or $branch -eq "HEAD") { $branch = "main" }
Write-Note ("目标：" + $remote + "  分支：" + $branch)
& git push -u origin $branch
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Bad "推送失败。常见原因与处理办法："
    Write-Host "   1) 要求输入账号密码：GitHub 已停用密码登录，请把 Personal Access Token 当作密码粘贴"
    Write-Host "      （GitHub → Settings → Developer settings → Personal access tokens → repo 权限）"
    Write-Host "   2) 仓库地址写错或仓库不存在：先运行 git remote set-url origin <正确地址>"
    Write-Host "   3) 远程仓库已有内容（新建时勾了 README）："
    Write-Host ("      先执行：git pull --rebase origin " + $branch + " --allow-unrelated-histories")
    Write-Host "      解决冲突后再双击本脚本重新推送"
    Write-Host "   4) 网络不通：可开启代理后重试"
    exit 1
}
Write-Host ""
Write-Ok "推送成功！"
Write-Note ("仓库地址：" + $remote)
Write-Note "以后每次改完代码，双击本脚本即可自动提交并推送"
