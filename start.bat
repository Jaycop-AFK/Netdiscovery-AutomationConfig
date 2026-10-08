@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem NetScope launcher (Windows).  start.bat            -> local use (http://localhost:8080)
rem                                   start.bat --share    -> let friends try it through a public port/tunnel (simulated lab only, access code)
rem Creates .venv on first run, installs deps, starts the server and opens the browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [setup] creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv || (echo Python 3.10+ not found & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo pip install failed & pause & exit /b 1)
)
".venv\Scripts\python.exe" run.py %*
pause
