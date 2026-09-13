@echo off
REM One-time setup for RBA on Windows. Wraps setup.ps1 so no execution policy has to be
REM changed on the machine: -ExecutionPolicy Bypass applies to this invocation only.
REM
REM   setup.cmd -Dev            also install pytest, ruff and mypy
REM   setup.cmd -SkipBrowser    skip the Playwright download used for PDF export
setlocal
REM PowerShell 7 when it is installed, Windows PowerShell 5.1 otherwise. Both are supported.
set "PSEXE=powershell"
where pwsh >nul 2>&1 && set "PSEXE=pwsh"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
exit /b %ERRORLEVEL%
