@echo off
setlocal
set "_ROOT=%~dp0.."
set "_PY=%_ROOT%\.venv\Scripts\python.exe"
if not exist "%_PY%" (
  echo [greenbubbles] .venv not found. Run:
  echo     python -m venv "%_ROOT%\.venv"
  echo     "%_ROOT%\.venv\Scripts\pip.exe" install -r "%_ROOT%\requirements.txt"
  exit /b 1
)
"%_PY%" -m unittest discover -s "%_ROOT%\tests" -t "%_ROOT%" -v %*
exit /b %ERRORLEVEL%
