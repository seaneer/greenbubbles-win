@echo off
setlocal
set "_ROOT=%~dp0"
set "_PS=pwsh"
where /q pwsh || set "_PS=powershell"
"%_PS%" -NoProfile -ExecutionPolicy Bypass -File "%_ROOT%scripts\connector-service.ps1" start -BindHost 127.0.0.1 -Port 8787 %*
if errorlevel 1 (
  echo.
  echo [greenbubbles] start failed, see state\logs\connector-*.err.log
  pause
  exit /b 1
)
echo.
echo [greenbubbles] running on 127.0.0.1:8787 only (SSH tunnel mode).
echo [greenbubbles] remote side: ssh -N -L 8787:127.0.0.1:8787 ^<user^>@^<this-pc^>
echo [greenbubbles] stop it with stop-connector-local.cmd
ping -n 4 127.0.0.1 >nul
exit /b 0
