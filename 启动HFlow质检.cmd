@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\wsl\quality-check.ps1" -InputPath "%~1"
set "QC_EXIT=%ERRORLEVEL%"
echo.
echo Exit code: %QC_EXIT%  ^(0=pass, 2=review/findings, 3=execution error^)
pause
exit /b %QC_EXIT%
