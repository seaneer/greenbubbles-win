@echo off
setlocal
set "_ROOT=%~dp0"
set "_PS=pwsh"
where /q pwsh || set "_PS=powershell"
"%_PS%" -NoProfile -ExecutionPolicy Bypass -File "%_ROOT%scripts\connector-service.ps1" status
pause
exit /b 0
