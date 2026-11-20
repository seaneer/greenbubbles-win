@echo off
setlocal
"%~dp0.venv\Scripts\python.exe" -m unittest discover -s "%~dp0tests" -t "%~dp0" -v %*
endlocal
