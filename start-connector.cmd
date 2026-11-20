@echo off
setlocal
set "_ROOT=%~dp0"
set "_PY=%_ROOT%.venv\Scripts\python.exe"
if exist "%_ROOT%python\python.exe" set "_PY=%_ROOT%python\python.exe"
if not exist "%_PY%" (
  echo [greenbubbles] .venv not found. Run: python -m venv "%_ROOT%.venv" ^&^& "%_ROOT%.venv\Scripts\pip.exe" install -r "%_ROOT%requirements.txt"
  echo Or use the portable release, which ships python\python.exe.
  exit /b 1
)
"%_PY%" "%_ROOT%greenbubbles.py" serve --port 8787
