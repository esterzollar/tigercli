@echo off
REM TigerLiteCode - setup launcher for cmd.exe.
REM Delegates to the PowerShell script (build.ps1), which does the real work.

where powershell >nul 2>nul
if errorlevel 1 (
    echo error: PowerShell was not found. Run build.ps1 directly, or install PowerShell.
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
exit /b %errorlevel%
