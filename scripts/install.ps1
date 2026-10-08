<#
    毕业论文写作智能体助手 —— ① 首次安装
    ------------------------------------------------------------
    做四件事：
      1) 检测 Python 3.9+（优先官方 python.org 版本或 py 启动器）
      2) 准备运行环境（能直接用就跳过虚拟环境，否则创建 .venv 并装依赖）
      3) 引导填写 DeepSeek API Key（写入 .env；可跳过 → 离线演示模式）
      4) 运行自检，确认安装成功

    用法（双击 ① 首次安装（双击我）.bat 即可，也可以带参数）：
      powershell -ExecutionPolicy Bypass -File scripts\install.ps1
      powershell ... -File scripts\install.ps1 -ApiKey "sk-xxx" -SkipSelfTest
      powershell ... -File scripts\install.ps1 -ForceVenv      # 强制创建独立虚拟环境
#>
param(
    [string]$ApiKey = "",
    [string]$PythonExe = "",
    [switch]$SkipSelfTest,
    [switch]$NonInteractive,
    [switch]$ForceVenv
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
try { $Host.UI.RawUI.WindowTitle = "毕业论文写作智能体助手 - 首次安装" } catch { }

# ============================== 输出辅助 ==============================
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

function Mask-Key {
    param([string]$Key)
    if ([string]::IsNullOrWhiteSpace($Key)) { return "(空)" }
    if ($Key.Length -le 12) { return "****" }
    return $Key.Substring(0, 6) + "****" + $Key.Substring($Key.Length - 4)
}

function Get-KeyFromText {
    # 从任意粘贴内容里提取 sk- 开头的 Key（兼容粘贴了整行 DEEPSEEK_API_KEY=sk-xxx 的情况）
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $m = [regex]::Match($Text, 'sk-[A-Za-z0-9_\-]{10,}')
    if ($m.Success) { return $m.Value }
    return ""
}

function Read-ApiKey {
    <#
      读取 DeepSeek API Key。为兼容各种终端，提供 4 种方式：
        1) 鼠标【右键】粘贴（Windows 传统控制台最可靠）
        2) Ctrl+V 粘贴
        3) 先把 Key 复制好，然后直接按【回车】→ 脚本自动读取剪贴板
        4) 输入 E 回车 → 用记事本手动编辑 .env
      返回值：Key 字符串；返回空串表示跳过；返回 __EDIT__ 表示改用记事本。
    #>
    param()

    if ($NonInteractive) { return "" }

    Write-Host ""
    Write-Host "   ┌─ 粘贴小提示 ───────────────────────────────────────────────┐" -ForegroundColor DarkGray
    Write-Host "   │ 在这个窗口里：鼠标【右键】= 粘贴（最可靠）                  │" -ForegroundColor DarkGray
    Write-Host "   │ 若右键/Ctrl+V 都没反应：先把 Key 复制到剪贴板，再直接按回车 │" -ForegroundColor DarkGray
    Write-Host "   │ 也可以输入 E 回车，用记事本手动填写 .env                    │" -ForegroundColor DarkGray
    Write-Host "   └────────────────────────────────────────────────────────────┘" -ForegroundColor DarkGray

    for ($try = 1; $try -le 3; $try++) {
        $ans = Read-Host "   请粘贴你的 DeepSeek API Key（回车=读剪贴板 / E=记事本 / 空回车跳过）"

        if ($ans -and $ans.Trim() -match '^[Ee]$') { return "__EDIT__" }

        if ($ans -and $ans.Trim()) {
            $key = Get-KeyFromText $ans
            if ($key) { return $key }
            Write-Warn2 "这段内容里没找到以 sk- 开头的 Key，请重新粘贴（或按回车读取剪贴板）"
            continue
        }

        # 空回车 → 尝试从剪贴板读取
        $clip = ""
        try { $clip = ([string](Get-Clipboard -Raw)) } catch { $clip = "" }
        $clipKey = Get-KeyFromText $clip
        if ($clipKey) {
            Write-Ok ("已从剪贴板读取到 Key：" + (Mask-Key $clipKey))
            return $clipKey
        }
        if ($clip -and $clip.Trim()) { Write-Warn2 ("剪贴板内容里没有 sk- 开头的 Key（读到：" + (Mask-Key $clip.Trim()) + "）") }
        else { Write-Warn2 "剪贴板里没有文本内容（可能是沙箱/远程桌面环境读不到剪贴板）" }
        Write-Note "已跳过输入：应用将以【离线演示模式】运行，之后随时可以重跑本脚本填写"
        return ""
    }
    return ""
}

function Edit-EnvWithNotepad {
    # 用记事本手动编辑 .env，关闭记事本后自动读回 Key
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        [System.IO.File]::WriteAllText($Path,
            "# 把下面等号后面替换成你的 DeepSeek API Key（保留 sk- 前缀），保存后关闭记事本`r`n`r`nDEEPSEEK_API_KEY=`r`n",
            (New-Object System.Text.UTF8Encoding($false)))
    }
    Write-Note "正在打开记事本，请在 DEEPSEEK_API_KEY= 后面粘贴你的 Key，然后 Ctrl+S 保存并关闭记事本…"
    try { Start-Process -FilePath "notepad.exe" -ArgumentList ('"' + $Path + '"') -Wait }
    catch { Write-Warn2 "无法打开记事本：$($_.Exception.Message)" }
    if (Test-Path -LiteralPath $Path) {
        $m = [regex]::Match([System.IO.File]::ReadAllText($Path), 'DEEPSEEK_API_KEY\s*=\s*(\S+)')
        if ($m.Success) {
            $k = Get-KeyFromText $m.Groups[1].Value
            if ($k) { return $k }
        }
    }
    return ""
}

function Resolve-KeyInput {
    # 统一处理 Read-ApiKey 的返回值：__EDIT__ → 改用记事本；其余原样返回
    param([string]$Value)
    if ($Value -eq "__EDIT__") { return (Edit-EnvWithNotepad -Path $envFile) }
    if ($Value) { return $Value }
    return ""
}

# ============================== 环境检测 ==============================
function Get-PythonInfo {
    param([string]$Exe, [string[]]$Pre = @())
    try {
        $out = & $Exe @Pre -c "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $lines = @($out)
        if ($lines.Count -lt 2) { return $null }
        return [pscustomobject]@{ Version = $lines[0].Trim(); Exe = $lines[1].Trim() }
    } catch { return $null }
}

function Test-VersionOk {
    param([string]$Version)
    try { $v = [version]$Version } catch { return $false }
    return ($v -ge [version]"3.9")
}

function Find-Python {
    $tries = New-Object System.Collections.Generic.List[object]
    if ($PythonExe) { $tries.Add(@($PythonExe, @())) }
    foreach ($v in @("-3.12", "-3.11", "-3.10", "-3.9")) { $tries.Add(@("py", @($v))) }
    $tries.Add(@("python", @()))
    $tries.Add(@("python3", @()))
    foreach ($t in $tries) {
        $info = Get-PythonInfo -Exe $t[0] -Pre $t[1]
        if ($info -and (Test-VersionOk $info.Version)) { return $info }
    }
    return $null
}

function Test-Deps {
    param([string]$Py)
    if (-not (Test-Path -LiteralPath $Py)) { return $false }
    & $Py -c "import streamlit, openai, pandas" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Install-Deps {
    param([string]$Py)
    Write-Step "安装项目依赖（首次约 1-3 分钟，请保持网络畅通）"
    Write-Note "streamlit / openai / pandas / python-dotenv"
    & $Py -m pip install --upgrade pip --disable-pip-version-check
    & $Py -m pip install -r (Join-Path $Root "requirements.txt") --disable-pip-version-check
    if (-not (Test-Deps -Py $Py)) {
        Write-Warn2 "默认源安装未完成，改用清华镜像重试…"
        & $Py -m pip install -r (Join-Path $Root "requirements.txt") -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --disable-pip-version-check
    }
    if (-not (Test-Deps -Py $Py)) {
        Write-Bad "依赖仍未安装成功。常见原因："
        Write-Host "       1) 网络无法访问 PyPI —— 可换手机热点后重试"
        Write-Host "       2) 当前 Python 发行版异常（部分 Anaconda / 精简版缺少 ssl 模块）"
        Write-Host "       3) 推荐到 https://www.python.org/downloads/ 安装官方 Python 3.11 / 3.12，"
        Write-Host "          安装时务必勾选【Add python.exe to PATH】，然后重新双击本脚本"
        return $false
    }
    return $true
}

function Set-EnvKey {
    param([string]$Path, [string]$Key)
    $lines = @()
    if (Test-Path -LiteralPath $Path) { $lines = [System.IO.File]::ReadAllLines($Path) }
    $found = $false
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($l in $lines) {
        if ($l -match '^\s*DEEPSEEK_API_KEY\s*=') { $out.Add("DEEPSEEK_API_KEY=" + $Key); $found = $true }
        else { $out.Add($l) }
    }
    if (-not $found) {
        if ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -ne "") { $out.Add("") }
        $out.Add("# DeepSeek API Key（由「首次安装」写入；该文件已在 .gitignore 中，不会被推送到 GitHub）")
        $out.Add("DEEPSEEK_API_KEY=" + $Key)
    }
    # 注意：必须写成「不带 BOM」的 UTF-8，否则 python-dotenv 会把 BOM 当成变量名的一部分
    [System.IO.File]::WriteAllLines($Path, $out, (New-Object System.Text.UTF8Encoding($false)))
}

# ============================== 主流程 ==============================
Write-Title "毕业论文写作智能体助手 · 首次安装"
Write-Note ("项目目录：" + $Root)

# ---------- 1/5 Python ----------
Write-Step "第 1 步 / 共 5 步：检测 Python 环境"
$py = Find-Python
if (-not $py) {
    Write-Bad "未检测到 Python 3.9 或更高版本。"
    Write-Host "       请到 https://www.python.org/downloads/ 下载安装 Python 3.11 / 3.12，"
    Write-Host "       安装时务必勾选【Add python.exe to PATH】，装好后重新双击本脚本。"
    if (Ask-YesNo "是否现在打开 Python 下载页面？" "Y") { Start-Process "https://www.python.org/downloads/" }
    exit 1
}
Write-Ok ("Python " + $py.Version + "   路径：" + $py.Exe)

# ---------- 2/5 运行环境 ----------
Write-Step "第 2 步 / 共 5 步：准备运行环境与会话依赖"
$interp = ""
$venvDir = Join-Path $Root ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"
$directOk = $false
if ((Test-Deps -Py $py.Exe) -and (-not $ForceVenv)) {
    Write-Ok "当前 Python 已安装 streamlit / openai / pandas"
    $directOk = Ask-YesNo "直接使用该 Python（不创建虚拟环境，最快）？" "Y"
}

if ($directOk) {
    $interp = $py.Exe
} else {
    if (-not (Test-Path -LiteralPath $venvPy)) {
        Write-Note "正在创建独立虚拟环境 .venv …"
        & $py.Exe -m venv $venvDir
    }
    $venvOk = $false
    if (Test-Path -LiteralPath $venvPy) {
        & $venvPy -c "import ssl" 2>$null | Out-Null
        $venvOk = ($LASTEXITCODE -eq 0)
    }
    if (-not $venvOk) {
        Write-Warn2 "该 Python 无法创建可用的虚拟环境（缺少 ssl 模块，常见于部分 Anaconda / 精简版）。"
        if (Ask-YesNo "改为直接在系统 Python 中安装依赖？" "Y") {
            $interp = $py.Exe
        } else {
            Write-Bad "已取消。建议安装官方 Python 3.11 / 3.12 后重试。"
            exit 1
        }
    } else {
        $interp = $venvPy
        Write-Ok "虚拟环境就绪：.venv"
    }
    if (-not (Test-Deps -Py $interp)) {
        if (-not (Install-Deps -Py $interp)) { exit 1 }
    }
}

if (-not $interp) { $interp = $py.Exe }
Write-Ok ("运行环境：" + $interp)

# 记录运行环境，供 ② 启动项目 使用
[System.IO.File]::WriteAllText((Join-Path $Root ".python_path"), $interp, (New-Object System.Text.UTF8Encoding($false)))
Write-Note "已写入 .python_path，② 启动项目会自动使用该环境"

# ---------- 3/5 API Key ----------
Write-Step "第 3 步 / 共 5 步：配置 DeepSeek API Key"
$envFile = Join-Path $Root ".env"
$currentKey = ""
if (Test-Path -LiteralPath $envFile) {
    $m = [regex]::Match([System.IO.File]::ReadAllText($envFile), 'DEEPSEEK_API_KEY\s*=\s*(\S+)')
    if ($m.Success) { $currentKey = $m.Groups[1].Value }
}

$newKey = ""
if ($ApiKey) {
    $newKey = $ApiKey.Trim()
    Write-Note "使用命令行参数提供的 Key"
} elseif ($currentKey -match '^sk-\S+') {
    Write-Ok ("项目 .env 中已有 Key：" + (Mask-Key $currentKey))
    if (Ask-YesNo "是否重新输入（覆盖原值）？" "N") { $newKey = Resolve-KeyInput (Read-ApiKey) }
} elseif ($env:DEEPSEEK_API_KEY) {
    Write-Ok ("系统环境变量中已有 Key：" + (Mask-Key $env:DEEPSEEK_API_KEY))
    Write-Note "系统环境变量优先级更高，可直接使用；下面的 .env 是可选的备份方式"
    if (Ask-YesNo "是否同时写入项目 .env？" "N") { $newKey = Resolve-KeyInput (Read-ApiKey) }
} else {
    Write-Note "Key 申请地址：https://platform.deepseek.com/api_keys（登录后点 Create API key）"
    Write-Note "不想现在填也行：直接回车跳过，应用会进入【离线演示模式】，流程可完整体验"
    $newKey = Resolve-KeyInput (Read-ApiKey)
    if (-not $newKey) { Write-Warn2 "已跳过：未配置 Key，启动后为离线演示模式（想补填就重跑本脚本 ①）" }
}

if ($newKey) {
    if ($newKey -notmatch '^sk-') { Write-Warn2 "该 Key 不以 sk- 开头，请确认没有复制错（应形如 sk-xxxxxxxx）" }
    Set-EnvKey -Path $envFile -Key $newKey
    Write-Ok ("已写入 .env：" + (Mask-Key $newKey))
    Write-Note "该文件已在 .gitignore 中，不会被提交到 Git 仓库"
}

# ---------- 4/5 自检 ----------
Write-Step "第 4 步 / 共 5 步：环境自检"
if ($SkipSelfTest) {
    Write-Note "已跳过自检（-SkipSelfTest）"
} elseif (Ask-YesNo "是否运行自检 smoke_test.py（不需要 API Key，约 1 分钟）？" "Y") {
    Write-Note "自检会验证：合规规则引擎 / 人在回路门禁 / 审计日志哈希链 / 完整 UI 流程"
    & $interp (Join-Path $Root "smoke_test.py")
    if ($LASTEXITCODE -eq 0) { Write-Ok "自检全部通过" }
    else { Write-Warn2 "自检存在失败项，请把上面的输出反馈给开发者（不影响基本使用）" }
}

# ---------- 5/5 完成 ----------
Write-Step "第 5 步 / 共 5 步：完成"
$hasKeyFinal = $false
if ($env:DEEPSEEK_API_KEY) { $hasKeyFinal = $true }
if (Test-Path -LiteralPath $envFile) {
    if ([System.IO.File]::ReadAllText($envFile) -match 'DEEPSEEK_API_KEY\s*=\s*sk-\S+') { $hasKeyFinal = $true }
}
if ($hasKeyFinal) { Write-Ok "已配置 API Key：启动后将调用 deepseek-chat" }
else { Write-Warn2 "未配置 API Key：启动后为离线演示模式（输出会标注「演示模式输出」）" }

Write-Title "安装完成！下一步：双击 ② 启动项目（双击我）.bat"
Write-Note "启动后浏览器会自动打开 http://localhost:8501"
Write-Note "使用建议：先看【概览与流程】页，再按 规划师 → 撰写员 → 合规审查 的顺序操作"
Write-Note "每个环节生成后都会暂停等你修改/批准，系统不会一键写完"
