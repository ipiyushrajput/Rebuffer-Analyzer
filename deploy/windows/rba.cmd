@echo off
REM The Windows task runner. Wraps run.ps1 so no execution policy has to be changed on the
REM machine: -ExecutionPolicy Bypass applies to this invocation only.
REM
REM   rba              start backend and frontend together
REM   rba serve        build the bundle and serve it next to the backend
REM   rba test         pytest + vitest
REM   rba help         every command
setlocal
REM PowerShell 7 when it is installed, Windows PowerShell 5.1 otherwise. Both are supported.
set "PSEXE=powershell"
where pwsh >nul 2>&1 && set "PSEXE=pwsh"
"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
