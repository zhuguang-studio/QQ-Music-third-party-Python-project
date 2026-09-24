#Requires -Version 5.1
<#
=====================================================================
 修复 "无法加载文件 ...ps1，因为在此系统上禁止运行脚本"
（ExecutionPolicy = Restricted / AllSigned / Undefined 导致的启动报错）
---------------------------------------------------------------------
 这类机器的典型特征：
   Get-ExecutionPolicy  ->  Restricted
   Get-ExecutionPolicy -List  ->  所有作用域全是 Undefined
   （即：从没配置过执行策略，系统用默认的"禁止一切脚本"）

 本工具做的事（全部免管理员）：
   1. 把「当前用户」执行策略设为 RemoteSigned —— 永久生效
   2. 顺带解除从网上下载的 .ps1 的文件锁定(Zone.Identifier)
   3. 自检：真正起一个子进程验证 .ps1 现在能跑
   4. 可选 (-FixBom)：给无 BOM 的 UTF-8 .ps1 补 BOM，修复中文乱码

 注意（本工具为什么直接写注册表）：
   本工具自身必须用 -ExecutionPolicy Bypass 启动（不然它自己也跑不起来）。
   而在这种会话里调用 PowerShell 的 Set-ExecutionPolicy 命令，会出现
   "安全性错误。" —— 实际上它已经偷偷把值写进注册表了，只是又抛了异常，
   结果被误判成"失败"。所以本工具直接写
   HKCU:\Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell
   的 ExecutionPolicy 值，行为完全一致，且不会被误报。

 用法：
   双击 fix_ps_policy.bat                     # 最简单
   powershell -ExecutionPolicy Bypass -File .\fix_ps_policy.ps1
   powershell -ExecutionPolicy Bypass -File .\fix_ps_policy.ps1 -Action Status
   powershell -ExecutionPolicy Bypass -File .\fix_ps_policy.ps1 -Action Undo
   powershell -ExecutionPolicy Bypass -File .\fix_ps_policy.ps1 -FixBom

 参数：
   -Action     Fix(默认) | Status(只查看) | Undo(还原成 Restricted)
   -Policy     目标策略，默认 RemoteSigned
   -Directory  要解除锁定/补 BOM 的目录，默认本脚本所在目录
   -NoUnblock  跳过"解除文件锁定"
   -FixBom     顺带补 UTF-8 BOM
=====================================================================
#>
[CmdletBinding()]
param(
    [ValidateSet('Fix','Status','Undo')]
    [string]$Action = 'Fix',

    [ValidateSet('RemoteSigned','Unrestricted','Bypass','AllSigned','Restricted')]
    [string]$Policy = 'RemoteSigned',

    [string]$Directory = '',

    [switch]$NoUnblock,
    [switch]$FixBom
)

$ErrorActionPreference = 'Continue'

# ------------------------- 输出小工具 -------------------------
function Write-Head($t) {
    Write-Host ""
    Write-Host ('=' * 66) -ForegroundColor DarkCyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ('=' * 66) -ForegroundColor DarkCyan
}
function Write-Info($t) { Write-Host "  $t" -ForegroundColor Gray }
function Write-Ok($t)   { Write-Host "  [OK] $t" -ForegroundColor Green }
function Write-Warn($t) { Write-Host "  [警告] $t" -ForegroundColor Yellow }
function Write-Err($t)  { Write-Host "  [失败] $t" -ForegroundColor Red }

$Scopes = @('MachinePolicy','UserPolicy','Process','CurrentUser','LocalMachine')

# 执行策略在两个注册表位置各存一份（与 Set-ExecutionPolicy 的存储位置完全一致）
$PolicyKeys = @{
    CurrentUser  = 'HKCU:\Software\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell'
    LocalMachine = 'HKLM:\SOFTWARE\Microsoft\PowerShell\1\ShellIds\Microsoft.PowerShell'
}

function Show-Policy {
    Write-Info ''
    Write-Info '  作用范围              当前设置'
    Write-Info '  ------------------------------------'
    foreach ($s in $Scopes) {
        $p = [string](Get-ExecutionPolicy -Scope $s)
        Write-Host ("  {0,-18}  {1}" -f $s, $p) -ForegroundColor White
    }
    Write-Info '  ------------------------------------'
    $eff = [string](Get-ExecutionPolicy)
    $color = if ($eff -eq 'Restricted' -or $eff -eq 'AllSigned') { 'Red' } else { 'Green' }
    Write-Host ("  有效(Effective)     $eff") -ForegroundColor $color
}

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { $false }
}

# 直接写注册表来设置执行策略（等价于 Set-ExecutionPolicy，但不会误报"安全性错误"）
function Set-PolicyRegistry([string]$scope, [string]$value) {
    $key = $PolicyKeys[$scope]
    if (-not $key) { throw "未知作用域: $scope" }
    $parent = Split-Path -Path $key -Parent
    if (-not (Test-Path -Path $parent)) { New-Item -Path $parent -Force | Out-Null }
    if (-not (Test-Path -Path $key))    { New-Item -Path $key    -Force | Out-Null }
    Set-ItemProperty -Path $key -Name 'ExecutionPolicy' -Value $value -Type String -Force
}
function Get-PolicyRegistry([string]$scope) {
    $key = $PolicyKeys[$scope]
    if (-not $key -or -not (Test-Path -Path $key)) { return $null }
    try { return [string](Get-ItemProperty -Path $key -ErrorAction Stop).ExecutionPolicy } catch { return $null }
}

# 起一个全新子进程，清掉可能继承的进程级策略变量，读它看到的真实策略
function Get-EffectiveInFreshProcess([string]$scope) {
    $line = 'set "PSExecutionPolicyPreference=" & powershell -NoProfile -Command "Get-ExecutionPolicy -Scope {0}"' -f $scope
    try { return ((& cmd /c $line 2>&1) -join ' ').Trim() } catch { return '' }
}

# 起一个全新的 powershell 子进程去跑一个临时 .ps1 —— 真正验证策略是否已放开
function Test-ScriptExecution {
    $tmp = Join-Path $env:TEMP ('psexec_probe_' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        Set-Content -LiteralPath $tmp -Value "Write-Output 'PS-EXEC-OK'" -Encoding UTF8
        # 先清掉可能被子进程继承的进程级策略变量，保证测的是注册表里的真实策略
        $line = 'set "PSExecutionPolicyPreference=" & powershell -NoProfile -File "{0}"' -f $tmp
        $out = (& cmd /c $line 2>&1) -join "`n"
        return @(($out -match 'PS-EXEC-OK'), $out)
    } catch {
        return @($false, $_.Exception.Message)
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Unblock-Scripts([string]$dir) {
    if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { return }
    $found = $false
    Get-ChildItem -LiteralPath $dir -Filter *.ps1 -File -ErrorAction SilentlyContinue | ForEach-Object {
        $stream = Get-Item -LiteralPath $_.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue
        if ($stream) {
            Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue
            Write-Ok "已解除文件锁定: $($_.Name)"
            $found = $true
        }
    }
    if (-not $found) { Write-Info '没有发现被系统锁定(下载自网络)的 .ps1 文件' }
}

function Add-Utf8Bom([string]$dir) {
    if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { return }
    $strict = New-Object System.Text.UTF8Encoding($false, $true)   # 严格 UTF-8 校验
    $bom    = New-Object System.Text.UTF8Encoding($true)
    $n = 0
    Get-ChildItem -LiteralPath $dir -Filter *.ps1 -File -ErrorAction SilentlyContinue | ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { return }
        try { $text = $strict.GetString($bytes) } catch { Write-Warn "跳过(不是UTF-8，可能已是GBK): $($_.Name)"; return }
        [System.IO.File]::WriteAllText($_.FullName, $text, $bom)
        Write-Ok "已补 UTF-8 BOM（修复中文乱码）: $($_.Name)"
        $n++
    }
    if ($n -eq 0) { Write-Info '所有 .ps1 都已有 BOM，无需处理' }
}

if (-not $Directory) { $Directory = $PSScriptRoot }
if (-not $Directory) { $Directory = (Get-Location).Path }

Write-Head 'PowerShell 脚本启动修复工具'
Write-Info "目标目录: $Directory"
Write-Info "时间    : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Info "PS 版本 : $($PSVersionTable.PSVersion)"

# ============================ Status ============================
if ($Action -eq 'Status') {
    Write-Head '当前执行策略'
    Show-Policy
    Write-Info ''
    Write-Info ("  注册表 CurrentUser  : " + $(if (Get-PolicyRegistry 'CurrentUser')   { Get-PolicyRegistry 'CurrentUser' }   else { '(未设置)' }))
    Write-Info ("  注册表 LocalMachine : " + $(if (Get-PolicyRegistry 'LocalMachine')  { Get-PolicyRegistry 'LocalMachine' }  else { '(未设置)' }))
    $ok, $out = Test-ScriptExecution
    if ($ok) { Write-Ok '自检：.ps1 脚本可以正常启动' }
    else { Write-Err '自检：.ps1 脚本【无法】启动（就是本机现在的状态）'; Write-Info $out }
    exit 0
}

# ============================ Undo ==============================
if ($Action -eq 'Undo') {
    Write-Head '还原执行策略为 Restricted（系统默认：禁止脚本）'
    try {
        Set-PolicyRegistry 'CurrentUser' 'Restricted'
        Write-Ok "CurrentUser 已还原为 Restricted（注册表值: $(Get-PolicyRegistry 'CurrentUser')）"
    } catch { Write-Err "还原 CurrentUser 失败: $($_.Exception.Message)" }
    if (Test-Admin) {
        try {
            Set-PolicyRegistry 'LocalMachine' 'Restricted'
            Write-Ok 'LocalMachine 已还原为 Restricted'
        } catch { Write-Warn "还原 LocalMachine 失败: $($_.Exception.Message)" }
    }
    exit 0
}

# ============================ Fix ===============================
Write-Head '1/3  检测执行策略'
Show-Policy

$mp = [string](Get-ExecutionPolicy -Scope MachinePolicy)
$up = [string](Get-ExecutionPolicy -Scope UserPolicy)
$gpoLocked = ($mp -ne 'Undefined') -or ($up -ne 'Undefined')

Write-Head '2/3  修复执行策略'
if ($gpoLocked) {
    Write-Warn '检测到组策略(MachinePolicy / UserPolicy)已强制规定执行策略，用户级设置会被其覆盖。'
    Write-Info '若修复后仍报错，需要让管理员在组策略里放开。'
}

$setOk = $false

# 1) 写「当前用户」——免管理员，永久生效
try {
    Set-PolicyRegistry 'CurrentUser' $Policy
    $back = Get-PolicyRegistry 'CurrentUser'
    if ($back -eq $Policy) {
        Write-Ok "已设置 当前用户(CurrentUser) 执行策略 = $Policy   （写入注册表成功，永久生效，免管理员）"
        $setOk = $true
    } else {
        Write-Err "写入 CurrentUser 后回读不一致（读到: $back）"
    }
} catch {
    Write-Err "设置 CurrentUser 失败: $($_.Exception.Message)"
}

# 2) 若已是管理员，顺便把「本机」也设了（可选，装了新用户也一样生效）
if (Test-Admin) {
    try {
        Set-PolicyRegistry 'LocalMachine' $Policy
        Write-Ok "已设置 本机(LocalMachine) 执行策略 = $Policy"
    } catch { Write-Warn "设置 LocalMachine 失败: $($_.Exception.Message)" }
} else {
    Write-Info '当前非管理员运行：只改「当前用户」范围，足以解决本机所有脚本启动问题。'
}

# 3) 用一个全新子进程回读，确认不是"自我安慰"
$fresh = Get-EffectiveInFreshProcess 'CurrentUser'
Write-Info "全新子进程看到的 CurrentUser 策略: $fresh"

Write-Head '3/3  收尾与自检'
if (-not $NoUnblock) { Unblock-Scripts $Directory }
if ($FixBom)         { Add-Utf8Bom $Directory }

$ok, $out = Test-ScriptExecution
if ($ok) {
    Write-Ok '自检通过：现在 .ps1 脚本可以正常启动'
} else {
    Write-Err '自检未通过，.ps1 仍无法启动'
    Write-Info $out
    if ($gpoLocked) { Write-Info '原因很可能是组策略强制限制，请让管理员处理。' }
    Write-Info '临时绕过（不改系统设置）: powershell -NoProfile -ExecutionPolicy Bypass -File "你的脚本.ps1"'
}

Write-Head '完成'
if ($ok) {
    Write-Info '以后在本机，双击 .ps1 / 右键"使用 PowerShell 运行" / powershell -File xxx.ps1'
    Write-Info '都不会再出现「因为在此系统上禁止运行脚本」。'
} else {
    Write-Info '问题依旧，请把上面输出发给管理员或截图。'
}
if ($setOk) { exit 0 } else { exit 1 }
