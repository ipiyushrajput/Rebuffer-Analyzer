@echo off
REM One-time setup for RBA on Windows. Wraps setup.ps1 so no execution policy has to be
REM changed on the machine: -ExecutionPolicy Bypass applies to this invocation only.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
