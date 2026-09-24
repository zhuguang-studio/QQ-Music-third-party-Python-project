#Requires -Version 5.1
<#
=====================================================================
 QQ音乐AI播放器 —— Windows 一键环境搭建脚本
---------------------------------------------------------------------
 作用：
   1. 自动探测本机可用的 Python (>=3.10)，避开 WindowsApps 假 Python
   2. 安装/补齐 pip 依赖 (openai / qqmusic-api-python / python-mpv ...)
   3. 安装 mpv 播放器 (winget -> scoop -> 手动提示) 并写入用户 PATH
   4. 生成/修复 start.bat（自动填入正确的 python 路径与 API Key）
   5. 全流程自检，输出可直接运行的命令

 用法（在本脚本所在目录）：
   powershell -ExecutionPolicy Bypass -File .\setup_qqmusic.ps1
   powershell -ExecutionPolicy Bypass -File .\setup_qqmusic.ps1 -VerifyOnly
   powershell -ExecutionPolicy Bypass -File .\setup_qqmusic.ps1 -PythonExe "C:\Users\admin\anaconda3\python.exe"

 可选参数：
   -PythonExe <路径>   手动指定 python.exe
   -SkipMpv            跳过 mpv 安装
   -SkipDeps           跳过 pip 依赖安装
   -VerifyOnly         只做检测，不做任何安装/修改
=====================================================================
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [switch]$SkipMpv,
    [switch]$SkipDeps,
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Continue'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyScript  = Join-Path $ScriptDir 'qq_music_ai_player.py'
$ReqFile   = Join-Path $ScriptDir 'requirements_qqmusic.txt'
$StartBat  = Join-Path $ScriptDir 'start.bat'

$Global:FailCount = 0

# ------------------------- 输出小工具 -------------------------
function Write-Title($t) {
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor DarkCyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor DarkCyan
}
function Write-Step($t) { Write-Host "`n[步骤] $t" -ForegroundColor Yellow }
function Write-Ok($t)   { Write-Host "  [OK] $t"   -ForegroundColor Green }
function Write-Warn($t) { Write-Host "  [警告] $t" -ForegroundColor DarkYellow }
function Write-Err($t)  { Write-Host "  [失败] $t" -ForegroundColor Red; $Global:FailCount++ }
function Write-Info($t) { Write-Host "  $t"        -ForegroundColor Gray }

Write-Title "QQ音乐AI播放器 · 一键环境搭建  (Windows)"
Write-Info "工作目录: $ScriptDir"
Write-Info "时间    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
if ($VerifyOnly) { Write-Warn "当前为 -VerifyOnly 模式：只检测，不安装、不修改任何文件" }

# =====================================================================
# 步骤 1 —— 选择可用的 Python (>= 3.10)
# =====================================================================
Write-Step "1/6  探测 Python 解释器"

function Get-PyVersion([string]$exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $null }
    try {
        $out = & $exe -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        $line = @($out)[0]
        if (-not $line) { return $null }
        return [version]$line.ToString().Trim()
    } catch { return $null }
}

$candidates = New-Object System.Collections.Generic.List[string]

if ($PythonExe) { $candidates.Add($PythonExe) }

# 1) py launcher 已注册的版本
$pyExe = (Get-Command py.exe -ErrorAction SilentlyContinue).Source
if ($pyExe) {
    $list = & $pyExe -0p 2>$null
    foreach ($line in $list) {
        if ($line -match '([A-Za-z]:\\.+?python\.exe)') { $candidates.Add($matches[1]) }
    }
}

# 2) 常见安装位置
$pyRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
if (Test-Path $pyRoot) {
    Get-ChildItem $pyRoot -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
}
$candidates.Add('C:\Users\admin\anaconda3\python.exe')
$candidates.Add((Join-Path $env:USERPROFILE 'anaconda3\python.exe'))
$candidates.Add('C:\ProgramData\anaconda3\python.exe')
$candidates.Add('C:\Python312\python.exe')
$candidates.Add('C:\Python311\python.exe')
$candidates.Add('C:\Python310\python.exe')

# 3) PATH 里的 python / python3
foreach ($n in @('python','python3')) {
    Get-Command $n -All -ErrorAction SilentlyContinue | ForEach-Object { $candidates.Add($_.Source) }
}

$chosen = $null
$seen = @{}
foreach ($c in $candidates) {
    if (-not $c) { continue }
    $full = $c
    try { $full = [System.IO.Path]::GetFullPath($c) } catch {}
    if ($seen.ContainsKey($full.ToLower())) { continue }
    $seen[$full.ToLower()] = $true

    # 跳过微软商店的假 python 占位符
    if ($full -match 'WindowsApps') { continue }

    $v = Get-PyVersion $full
    if (-not $v) { continue }

    if ($v -ge [version]'3.10') {
        if (-not $chosen) {
            $chosen = [pscustomobject]@{ Path = $full; Version = $v }
        } else {
            Write-Info "备用: $full  (Python $v)"
        }
    } else {
        Write-Info "跳过: $full  (Python $v < 3.10，版本过低)"
    }
}

if (-not $chosen) {
    Write-Err "没有找到 Python 3.10 或更高版本！"
    Write-Info "请先安装 Python 3.12+（务必勾选 Add python.exe to PATH）："
    Write-Info "  winget install --id=Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements"
    Write-Info "  下载页: https://www.python.org/downloads/"
    exit 1
}

$PyExe      = $chosen.Path
$PyVersion  = $chosen.Version
Write-Ok "使用 Python $PyVersion"
Write-Info "路径: $PyExe"

# 校验工程文件
foreach ($f in @($PyScript, $ReqFile)) {
    if (Test-Path $f) { Write-Ok "找到 $(Split-Path $f -Leaf)" }
    else { Write-Err "缺少文件: $f（请确认脚本放在工程根目录）" }
}

# =====================================================================
# 步骤 2 —— pip 自检
# =====================================================================
Write-Step "2/6  检查 pip"
$pipVer = & $PyExe -m pip --version 2>$null
if ($LASTEXITCODE -eq 0 -and $pipVer) {
    Write-Ok ($pipVer -join ' ')
} else {
    Write-Warn "pip 不可用，尝试通过 ensurepip 修复..."
    if (-not $VerifyOnly) {
        & $PyExe -m ensurepip --upgrade 2>&1 | Out-Null
        $pipVer = & $PyExe -m pip --version 2>$null
        if ($LASTEXITCODE -eq 0) { Write-Ok ($pipVer -join ' ') }
        else { Write-Err "pip 修复失败，请重装 Python 并勾选 pip" }
    }
}

# =====================================================================
# 步骤 3 —— 安装 pip 依赖
# =====================================================================
Write-Step "3/6  安装 Python 依赖"

$packages = @(
    'openai>=1.0.0',
    'qqmusic-api-python',
    'python-mpv',
    'httpx',
    'tqdm',
    'pywin32'
)

function Get-MissingPackages {
    $probe = @'
import importlib.util, json, sys
mods = {"openai":"openai","qqmusic_api":"qqmusic-api-python","mpv":"python-mpv",
        "httpx":"httpx","tqdm":"tqdm","win32file":"pywin32"}
missing = [pkg for mod, pkg in mods.items() if importlib.util.find_spec(mod) is None]
print(json.dumps(missing))
'@
    $tmp = Join-Path $env:TEMP ("qqprobe_" + [guid]::NewGuid().ToString('N') + ".py")
    Set-Content -Path $tmp -Value $probe -Encoding UTF8
    try {
        $out = & $PyExe $tmp 2>$null | Select-Object -Last 1
        return @($out | ConvertFrom-Json)
    } catch { return @() }
    finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

$missing = Get-MissingPackages
if ($missing.Count -eq 0) {
    Write-Ok "所有依赖均已安装，无需处理"
} else {
    Write-Warn ("缺失依赖: " + ($missing -join ', '))
    if ($VerifyOnly) {
        Write-Info "（-VerifyOnly 模式，跳过安装）"
    } elseif ($SkipDeps) {
        Write-Warn "已指定 -SkipDeps，跳过依赖安装"
    } else {
        Write-Info "升级 pip 到最新版..."
        & $PyExe -m pip install --upgrade pip --disable-pip-version-check -q 2>&1 |
            ForEach-Object { Write-Info $_ }

        Write-Info "开始安装依赖（首次较慢，请耐心等待）..."
        if (Test-Path $ReqFile) {
            & $PyExe -m pip install -r $ReqFile --disable-pip-version-check 2>&1 |
                ForEach-Object { Write-Info $_ }
        }
        # 兜底：逐个补装（应对 requirements 里没有的包）
        & $PyExe -m pip install @packages --disable-pip-version-check 2>&1 |
            ForEach-Object { Write-Info $_ }

        $missing = Get-MissingPackages
        if ($missing.Count -eq 0) { Write-Ok "全部依赖安装成功" }
        else { Write-Err ("仍有依赖缺失: " + ($missing -join ', ')) }
    }
}

# =====================================================================
# 步骤 4 —— 依赖导入 & 编译自检
# =====================================================================
Write-Step "4/6  代码导入自检"
if (-not $VerifyOnly) {
    $check = & $PyExe -c @"
import sys
sys.path.insert(0, r'$ScriptDir')
ok = True
for name, mod in [('openai','openai'), ('qqmusic-api-python','qqmusic_api'),
                  ('httpx','httpx'), ('tqdm','tqdm')]:
    try:
        __import__(mod); print('  [OK] ' + name)
    except Exception as e:
        ok = False; print('  [失败] ' + name + ' -> ' + repr(e))
# python-mpv 需要 mpv 的 DLL，没装 mpv 时报 OSError 属正常（程序会自动回退子进程模式）
try:
    __import__('mpv'); print('  [OK] python-mpv (原生库模式)')
except OSError as e:
    print('  [提示] python-mpv 未找到 mpv DLL —— 未安装 mpv 时属正常，将走子进程模式')
except Exception as e:
    ok = False; print('  [失败] python-mpv -> ' + repr(e))
try:
    __import__('py_compile').compile(r'$PyScript', doraise=True)
    print('  [OK] qq_music_ai_player.py 语法检查通过')
except Exception as e:
    ok = False; print('  [失败] 语法错误 -> ' + repr(e))
sys.exit(0 if ok else 1)
"@ 2>&1
    $check | ForEach-Object { Write-Info $_ }
    if ($LASTEXITCODE -eq 0) { Write-Ok "导入与语法自检通过" }
    else { Write-Warn "自检存在问题，请看上方输出" }
} else {
    Write-Info "（-VerifyOnly 模式，跳过）"
}

# =====================================================================
# 步骤 5 —— mpv 播放器
# =====================================================================
Write-Step "5/6  检查 / 安装 mpv 播放器"

function Find-Mpv {
    $c = (Get-Command mpv -ErrorAction SilentlyContinue).Source
    if ($c) { return $c }
    $paths = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\mpv.exe",
        "$env:LOCALAPPDATA\Programs\mpv\mpv.exe",
        "$env:APPDATA\mpv\mpv.exe",
        "$env:USERPROFILE\scoop\shims\mpv.exe",
        "$env:USERPROFILE\scoop\apps\mpv\current\mpv.exe",
        'C:\Program Files\mpv\mpv.exe',
        'C:\Program Files\MPV Player\mpv.exe',
        'C:\Program Files (x86)\mpv\mpv.exe',
        'D:\mpv\mpv.exe'
    )
    foreach ($p in $paths) { if ($p -and (Test-Path $p)) { return $p } }
    # winget 便携包目录
    $wg = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $wg) {
        $hit = Get-ChildItem $wg -Recurse -Filter 'mpv.exe' -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Add-UserPathEntry([string]$dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return $false }
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($cur -split ';' | Where-Object { $_ -and $_.Trim() })
    if ($parts -contains $dir) { return $false }
    $new = (@($parts) + $dir) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $new, 'User')
    if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$env:Path;$dir" }
    return $true
}

$mpv = Find-Mpv
if ($mpv) {
    Write-Ok "已找到 mpv: $mpv"
    try { Write-Info ((& $mpv --version 2>&1 | Select-Object -First 1)) } catch {}
} else {
    Write-Warn "未找到 mpv"
    if ($VerifyOnly) {
        Write-Info "（-VerifyOnly 模式，跳过安装）"
    } elseif ($SkipMpv) {
        Write-Warn "已指定 -SkipMpv，跳过"
    } else {
        $winget = (Get-Command winget.exe -ErrorAction SilentlyContinue).Source
        if ($winget) {
            Write-Info "使用 winget 安装 shinchiro.mpv ..."
            # 注意：部分 winget 版本不支持 --disable-interactivity，故不使用该参数
            & $winget install --id shinchiro.mpv -e --silent `
                --accept-package-agreements --accept-source-agreements 2>&1 |
                ForEach-Object { Write-Info $_ }
        } else {
            Write-Warn "未找到 winget"
        }

        # 刷新 PATH 后重新查找
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path','User')
        $mpv = Find-Mpv

        if (-not $mpv) {
            # 兜底 1：便携版下载解压（GitHub 直连不通时自动走加速镜像，最可靠）
            $portable = Join-Path $ScriptDir 'install_mpv_portable.ps1'
            if (Test-Path $portable) {
                Write-Info "改用便携版下载方案（直连 + 镜像 + 7zr 解压）..."
                # 注意：这里千万不要用管道( | ForEach-Object )去捕获子进程输出，
                # 否则父进程会用自己控制台的编码去解码子进程 stdout，中文会变成
                # 乱码（例如 "mpv 渚挎惡鐗堝畨瑁?"）。让它直接写控制台即可。
                & powershell -NoProfile -ExecutionPolicy Bypass -File $portable -PythonExe $PyExe
                $mpv = Find-Mpv
            }
        }

        if (-not $mpv) {
            # 兜底 2：scoop
            if (Get-Command scoop -ErrorAction SilentlyContinue) {
                Write-Info "尝试 scoop 安装 mpv ..."
                & scoop install mpv 2>&1 | ForEach-Object { Write-Info $_ }
                $mpv = Find-Mpv
            }
        }

        if ($mpv) {
            Write-Ok "mpv 安装成功: $mpv"
            $links = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
            if (Add-UserPathEntry $links) { Write-Ok "已把 $links 写入用户 PATH" }
            if (Add-UserPathEntry (Split-Path $mpv -Parent)) { Write-Ok "已把 mpv 目录写入用户 PATH" }
        } else {
            Write-Err "mpv 自动安装失败，请手动安装（任选其一）："
            Write-Info "  1) 运行 .\install_mpv_portable.ps1"
            Write-Info "  2) 手动下载 https://github.com/shinchiro/mpv-winbuild-cmake/releases"
            Write-Info "     用 7-Zip 解压 .7z，把 mpv.exe 放到: $env:LOCALAPPDATA\Programs\mpv\"
        }
    }
}

# =====================================================================
# 步骤 6 —— API Key + start.bat
# =====================================================================
Write-Step "6/6  配置 DeepSeek API Key 与启动脚本"

function Get-ExistingKey {
    $k = $env:DEEPSEEK_API_KEY
    if ($k -and $k -match '^sk-\S{10,}$') { return $k.Trim() }
    if (Test-Path $StartBat) {
        $m = Select-String -Path $StartBat -Pattern 'sk-[A-Za-z0-9_\-]{20,}' -ErrorAction SilentlyContinue |
             Select-Object -First 1
        if ($m) { return $m.Matches[0].Value }
    }
    $u = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')
    if ($u -and $u -match '^sk-\S{10,}$') { return $u.Trim() }
    return $null
}

$apiKey = Get-ExistingKey
if ($apiKey) {
    Write-Ok "已检测到 DEEPSEEK_API_KEY（来自环境变量或旧的 start.bat）"
} else {
    Write-Warn "未检测到 DEEPSEEK_API_KEY"
    Write-Info "获取地址: https://platform.deepseek.com/api_keys"
    if (-not $VerifyOnly) {
        $userKeyInput = Read-Host "  请粘贴你的 DeepSeek API Key（直接回车可跳过）"
        if ($userKeyInput -and $userKeyInput.Trim() -match '^sk-') { $apiKey = $userKeyInput.Trim() }
        else { Write-Warn "未输入有效 Key，稍后需手动设置" }
    } else {
        Write-Info "（-VerifyOnly 模式，跳过）"
    }
}

if ($apiKey -and -not $VerifyOnly) {
    # 持久化到用户环境变量，今后所有终端都能直接用
    [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $apiKey, 'User')
    $env:DEEPSEEK_API_KEY = $apiKey
    Write-Ok "API Key 已写入用户环境变量 DEEPSEEK_API_KEY"
}

if (-not (Test-Path $PyScript)) {
    Write-Err "找不到 qq_music_ai_player.py，无法生成 start.bat"
} elseif ($VerifyOnly) {
    Write-Info "（-VerifyOnly 模式，跳过 start.bat 生成）"
} else {
    $keyLine = if ($apiKey) { $apiKey } else { 'sk-请在此填入你的DeepSeek密钥' }
    $bat = @"
@echo off
cd /d "%~dp0"
title QQ音乐AI播放器

rem ===== 本文件由 setup_qqmusic.ps1 自动生成 =====
rem Python 解释器（本机自动探测结果，Python $PyVersion）
set "PYEXE=$PyExe"
if not exist "%PYEXE%" set "PYEXE=python"

rem DeepSeek API Key
set "DEEPSEEK_API_KEY=$keyLine"

"%PYEXE%" "%~dp0qq_music_ai_player.py" %*
echo.
echo [进程已退出] 按任意键关闭窗口...
pause >nul
"@
    Set-Content -Path $StartBat -Value $bat -Encoding OEM
    Write-Ok "已重新生成 start.bat（Python: $PyExe）"
}

# 同步更新 requirements 文件
if ((Test-Path $ReqFile) -and -not $VerifyOnly) {
    $req = @'
# QQ音乐AI播放器 - 依赖清单
# 安装方式: pip install -r requirements_qqmusic.txt
# 一键搭建: powershell -ExecutionPolicy Bypass -File .\setup_qqmusic.ps1

openai>=1.0.0
qqmusic-api-python
python-mpv
httpx
tqdm
pywin32 ; sys_platform == "win32"
'@
    Set-Content -Path $ReqFile -Value $req -Encoding ASCII
    Write-Ok "已更新 requirements_qqmusic.txt"
}

# =====================================================================
# 汇总
# =====================================================================
Write-Title "搭建结果汇总"
Write-Host "  Python      : $PyExe  ($PyVersion)" -ForegroundColor White
$mpvFinal = Find-Mpv
Write-Host ("  mpv         : " + $(if ($mpvFinal) { $mpvFinal } else { '未安装（仅搜索/推荐模式）' })) -ForegroundColor White
$keyState = if ($apiKey) { '已配置 ✅' } else { '未配置 ❌' }
Write-Host "  API Key     : $keyState" -ForegroundColor White
Write-Host "  启动脚本    : $StartBat" -ForegroundColor White
Write-Host ""
if ($Global:FailCount -eq 0 -and $mpvFinal -and $apiKey) {
    Write-Host "  🎉 环境就绪！双击 start.bat 或运行下面命令即可开始：" -ForegroundColor Green
    Write-Host "     `"$PyExe`" `"$PyScript`"" -ForegroundColor Green
} else {
    Write-Host "  ⚠️ 仍有未完成项，请按上方提示处理后重新运行本脚本。" -ForegroundColor Yellow
    Write-Host "     复检命令: powershell -ExecutionPolicy Bypass -File .\setup_qqmusic.ps1 -VerifyOnly" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  首次运行需用「手机号 + 短信验证码」登录 QQ 音乐，" -ForegroundColor Gray
Write-Host "  凭证保存在 credential.json，之后自动复用。" -ForegroundColor Gray
Write-Host ""
