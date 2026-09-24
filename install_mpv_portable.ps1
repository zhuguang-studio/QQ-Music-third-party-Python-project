#Requires -Version 5.1
<#
=====================================================================
 mpv 便携版安装脚本（winget 不可用时的兜底方案）
---------------------------------------------------------------------
 从 GitHub 下载 shinchiro 的 mpv Windows 构建（.7z），
 解压到 %LOCALAPPDATA%\Programs\mpv  —— QQ音乐AI播放器会自动识别该目录。

 用法:
   powershell -ExecutionPolicy Bypass -File .\install_mpv_portable.ps1
=====================================================================
#>
[CmdletBinding()]
param(
    [string]$TargetDir = (Join-Path $env:LOCALAPPDATA 'Programs\mpv'),
    [string]$PythonExe = '',
    [string]$Proxy = ''
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m) { Write-Host "  $m" -ForegroundColor Gray }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [警告] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [失败] $m" -ForegroundColor Red }

Write-Host ""
Write-Host "===== mpv 便携版安装 =====" -ForegroundColor Cyan
Info "目标目录: $TargetDir"

$mpvExe = Join-Path $TargetDir 'mpv.exe'
if (Test-Path $mpvExe) {
    Ok "mpv 已存在于: $mpvExe"
    & $mpvExe --version 2>&1 | Select-Object -First 1 | ForEach-Object { Info $_ }
    return
}

# ---------- 1. 找下载地址 ----------
$api = 'https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest'
$assetUrl = $null
$assetName = $null
$expectSize = 0

try {
    Info "查询最新版本信息..."
    $headers = @{ 'User-Agent' = 'mpv-portable-installer' }
    $rel = Invoke-RestMethod -Uri $api -Headers $headers -TimeoutSec 60
    $asset = $rel.assets |
             Where-Object { $_.name -match '^mpv-x86_64-.*\.7z$' -and $_.name -notmatch 'v3|dev' } |
             Select-Object -First 1
    if (-not $asset) {
        $asset = $rel.assets | Where-Object { $_.name -match '^mpv-x86_64-.*\.7z$' } |
                 Sort-Object size -Descending | Select-Object -First 1
    }
    if ($asset) {
        $assetUrl  = $asset.browser_download_url
        $expectSize = [long]$asset.size
        $assetName = $asset.name
        Ok "找到版本: $($rel.tag_name)  ->  $assetName"
        Info ("大小: {0:N1} MB" -f ($asset.size / 1MB))
    } else {
        Warn "未在 release 资源中找到 .7z 包"
    }
} catch {
    Warn "GitHub API 查询失败: $($_.Exception.Message)"
}

if (-not $assetUrl) {
    Write-Host ""
    Fail "无法自动获取下载地址，请手动安装："
    Info "1) 打开 https://github.com/shinchiro/mpv-winbuild-cmake/releases"
    Info "2) 下载 mpv-x86_64-*-git-*.7z"
    Info "3) 解压，把里面的 mpv.exe（及同目录文件）放到: $TargetDir"
    exit 1
}

# ---------- 2. 下载（直连 + 国内镜像 + curl 兜底） ----------
# 校验一个 .7z 是否"看起来是完整可用的"：
#   1) 大小接近官方发布的大小（防止只用了一半就断网）
#   2) 开头 6 字节是 7z 魔数 37 7A BC AF 27 1C
# 这一步很关键：以前只判断">1MB"就复用临时文件，结果下载中断留下的残缺包
# 被反复拿去解压，永远报 "Unexpected end of archive"。
function Test-Good7z([string]$path, [long]$expectSize) {
    if (-not $path -or -not (Test-Path -LiteralPath $path)) { return $false }
    $len = (Get-Item -LiteralPath $path).Length
    if ($len -le 1MB) { return $false }
    if ($expectSize -gt 0 -and $len -lt [long]($expectSize * 0.98)) { return $false }
    $fs = $null
    try {
        $fs = [System.IO.File]::OpenRead($path)
        $h = New-Object byte[] 6
        if ($fs.Read($h, 0, 6) -ne 6) { return $false }
    } catch { return $false } finally { if ($fs) { $fs.Close() } }
    return ($h[0] -eq 0x37 -and $h[1] -eq 0x7A -and $h[2] -eq 0xBC -and
            $h[3] -eq 0xAF -and $h[4] -eq 0x27 -and $h[5] -eq 0x1C)
}

New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
$tmp = Join-Path $env:TEMP $assetName
$doDownload = $true
if (Test-Path -LiteralPath $tmp) {
    if (Test-Good7z $tmp $expectSize) {
        Info "复用已下载的临时文件: $tmp"
        $doDownload = $false
    } else {
        Warn ("丢弃残缺/损坏的临时文件（仅 {0:N1} MB，应为 {1:N1} MB）: $tmp" -f `
              ((Get-Item $tmp).Length / 1MB), ($expectSize / 1MB))
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Download-File([string]$Url, [string]$Out) {
    Remove-Item $Out -Force -ErrorAction SilentlyContinue
    $old = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        $p = @{ Uri = $Url; OutFile = $Out; TimeoutSec = 1800; UseBasicParsing = $true }
        if ($Proxy) { $p['Proxy'] = $Proxy }
        Invoke-WebRequest @p
        if (Test-Good7z $Out $expectSize) { $ProgressPreference = $old; return $true }
    } catch {
        Warn "Invoke-WebRequest 失败: $($_.Exception.Message)"
    } finally {
        $ProgressPreference = $old
    }
    $curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
    if ($curl) {
        Info "改用 curl.exe 重试 ..."
        & $curl -L --fail --retry 3 --connect-timeout 30 -o $Out $Url
        if ($LASTEXITCODE -eq 0 -and (Test-Good7z $Out $expectSize)) { return $true }
        Warn "curl.exe 失败 (exit=$LASTEXITCODE)"
    }
    Remove-Item -LiteralPath $Out -Force -ErrorAction SilentlyContinue   # 清掉残缺文件，避免下次被误复用
    return $false
}

if ($doDownload) {
    Info "开始下载（约 32 MB，网络差时可能需要几分钟）..."
    # 依次尝试：直连 -> 国内加速镜像
    $mirrors = @(
        '',
        'https://ghfast.top/',
        'https://gh-proxy.com/',
        'https://ghproxy.net/',
        'https://mirror.ghproxy.com/'
    )
    $downloaded = $false
    foreach ($m in $mirrors) {
        $u = if ($m) { $m + $assetUrl } else { $assetUrl }
        Info "尝试: $(if ($m) { $m } else { 'GitHub 直连' })"
        if (Download-File $u $tmp) { $downloaded = $true; break }
    }
    if (-not $downloaded) {
        Write-Host ""
        Fail "所有下载通道均失败。请手动下载后解决："
        Info "1) 用浏览器打开: $assetUrl"
        Info "2) 下载得到的 .7z 放到: $tmp"
        Info "3) 重新运行本脚本（会自动复用该文件）"
        exit 1
    }
    Ok ("下载完成: {0:N1} MB" -f ((Get-Item $tmp).Length / 1MB))
}

# ---------- 3. 解压 ----------
Info "解压到 $TargetDir ..."
$mpvExe = Join-Path $TargetDir 'mpv.exe'

# 3a. Windows 自带的 bsdtar（部分 7z 因 LZMA 编码不支持）
$tar = (Get-Command tar.exe -ErrorAction SilentlyContinue).Source
if ($tar -and -not (Test-Path $mpvExe)) {
    try {
        & $tar -xf $tmp -C $TargetDir 2>&1 | ForEach-Object { Info $_ }
        if (Test-Path $mpvExe) { Ok "使用 tar 解压成功" }
    } catch { Warn "tar 解压失败: $($_.Exception.Message)" }
}

# 3b. 7-Zip
if (-not (Test-Path $mpvExe)) {
    $sevenZip = @(
        'C:\Program Files\7-Zip\7z.exe',
        'C:\Program Files (x86)\7-Zip\7z.exe',
        (Join-Path $env:LOCALAPPDATA 'Programs\7-Zip\7z.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $sevenZip) { $sevenZip = (Get-Command 7z.exe -ErrorAction SilentlyContinue).Source }
    if ($sevenZip) {
        & $sevenZip x $tmp "-o$TargetDir" -y | Out-Null
        if (Test-Path $mpvExe) { Ok "使用 7-Zip 解压成功" }
    }
}

# 3c. 下载 7-Zip 官方独立版 7zr.exe（对 7z 格式支持最完整，含 BCJ2）
if (-not (Test-Path $mpvExe)) {
    $sevenZr = Join-Path $env:TEMP '7zr.exe'
    if (-not (Test-Path $sevenZr)) {
        foreach ($u in @(
            'https://www.7-zip.org/a/7zr.exe',
            'https://sparanoid.com/lab/7z/7zr.exe'
        )) {
            Info "下载 7zr.exe: $u"
            $old = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'
            try {
                Invoke-WebRequest -Uri $u -OutFile $sevenZr -TimeoutSec 300 -UseBasicParsing
            } catch {
                Warn "下载失败: $($_.Exception.Message)"
            } finally {
                $ProgressPreference = $old
            }
            if ((Test-Path $sevenZr) -and (Get-Item $sevenZr).Length -gt 100KB) { break }
        }
    }
    if ((Test-Path $sevenZr) -and (Get-Item $sevenZr).Length -gt 100KB) {
        Ok ("7zr.exe 就绪 ({0:N0} KB)" -f ((Get-Item $sevenZr).Length / 1KB))
        Info "使用 7zr.exe 解压 ..."
        $prevEap2 = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $sevenZr x $tmp "-o$TargetDir" -y 2>&1 | ForEach-Object { Info $_ }
        $ErrorActionPreference = $prevEap2
        if (Test-Path $mpvExe) { Ok "使用 7zr.exe 解压成功" }
    } else {
        Warn "7zr.exe 下载失败"
    }
}

# 3d. Python + py7zr（备选）
if (-not (Test-Path $mpvExe)) {
    $py = $PythonExe
    if (-not $py) {
        $tryPy = @(
            (Join-Path $env:USERPROFILE 'anaconda3\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        $py = $tryPy
    }
    if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if ($py) {
        Info "使用 Python + py7zr 解压: $py"
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        Info "确认 py7zr 已就绪（已安装时会秒过）..."
        & $py -m pip install py7zr --disable-pip-version-check -q 2>&1 |
            ForEach-Object { Info $_ }
        $ext = Join-Path $PSScriptRoot 'tools_extract_7z.py'
        & $py $ext $tmp $TargetDir 2>&1 | ForEach-Object { Info $_ }
        $ErrorActionPreference = $prevEap
        if (Test-Path $mpvExe) { Ok "使用 py7zr 解压成功" }
        else { Warn "py7zr 解压失败" }
    } else {
        Warn "未找到 Python，无法用 py7zr 解压"
    }
}

# 3c. 解压出来的可能带一层子目录，扁平化
if (Test-Path $TargetDir) {
    Get-ChildItem $TargetDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        if (Test-Path (Join-Path $_.FullName 'mpv.exe')) {
            Info "提升子目录内容: $($_.Name)"
            Get-ChildItem $_.FullName -Force | Move-Item -Destination $TargetDir -Force
            Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path $mpvExe)) {
    Write-Host ""
    Fail "解压后仍未找到 mpv.exe"
    Info "请手动解压 $tmp 到 $TargetDir"
    exit 1
}

Ok "mpv 安装完成: $mpvExe"
& $mpvExe --version 2>&1 | Select-Object -First 1 | ForEach-Object { Info $_ }

# ---------- 4. 加入用户 PATH ----------
$cur = [Environment]::GetEnvironmentVariable('Path', 'User')
$parts = @($cur -split ';' | Where-Object { $_ -and $_.Trim() })
if ($parts -notcontains $TargetDir) {
    [Environment]::SetEnvironmentVariable('Path', (($parts + $TargetDir) -join ';'), 'User')
    Ok "已把 $TargetDir 追加到用户 PATH"
} else {
    Info "用户 PATH 中已存在该目录"
}

Remove-Item $tmp -Force -ErrorAction SilentlyContinue
Write-Host ""
Ok "🎉 mpv 就绪，QQ音乐AI播放器现在可以播放音乐了。"
