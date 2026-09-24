@echo off
chcp 936 >nul 2>nul
setlocal EnableExtensions
cd /d "%~dp0"
title �޸�����ϵͳ�Ͻ�ֹ���нű�

echo ============================================================
echo    PowerShell �ű������޸�����
echo    ������޷������ļ� xxx.ps1����Ϊ�ڴ�ϵͳ�Ͻ�ֹ���нű�
echo ============================================================
echo.

where powershell >nul 2>nul
if errorlevel 1 (
    echo [ʧ��] �Ҳ��� powershell.exe���޷�������
    goto :done
)

if not exist "%~dp0fix_ps_policy.ps1" (
    echo [ʧ��] ȱ�� fix_ps_policy.ps1��������ͱ��ļ�����ͬһ��Ŀ¼��
    goto :done
)

rem �� Bypass �����޸��ű������������Ǳ�����ʱҪ�õ�������ʽ��
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_ps_policy.ps1" -Action Fix
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_ps_policy.ps1" %*
)

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
    echo [���] �����ɹ����Ժ�ֱ��˫�������� .ps1 �����ٱ��������
) else (
    echo [ע��] ������ %RC%����鿴�Ϸ������������������ƣ������Ա������
)

:done
echo.
echo ��������رմ���...
pause >nul
