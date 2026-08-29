# QQ音乐AI播放器 - 清理脚本
# 删除运行过程中产生的临时/生成文件

Write-Host "=== QQ音乐AI播放器 清理脚本 ===" -ForegroundColor Cyan

$filesToDelete = @(
    "credential.json",   # 登录凭证缓存
    "qrcode.png",        # 二维码图片
    "downloads"          # 下载的歌曲
)

$deleted = 0
foreach ($file in $filesToDelete) {
    if (Test-Path $file) {
        Remove-Item $file -Force
        Write-Host "  已删除: $file" -ForegroundColor Green
        $deleted++
    } else {
        Write-Host "  不存在: $file" -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($deleted -gt 0) {
    Write-Host "清理完成，共删除 $deleted 个文件。" -ForegroundColor Cyan
} else {
    Write-Host "没有需要清理的文件。" -ForegroundColor Cyan
}
