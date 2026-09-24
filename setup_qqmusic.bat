@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QQ音乐AI播放器 - 环境搭建

echo ============================================================
echo   QQ音乐AI播放器 一键环境搭建
echo ============================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_qqmusic.ps1" %*

echo.
echo [搭建脚本已结束] 按任意键关闭窗口...
pause >nul
