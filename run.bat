@echo off
cd /d "%~dp0"
where py >nul 2>nul || (echo Python 3.9 or newer is required. Install it from python.org first. & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo Setting up for the first time...
  py -3 -m venv .venv
)
rem Install libraries on first run and whenever requirements.txt changes
fc /b requirements.txt .venv\installed-requirements.txt >nul 2>nul || (
  echo Installing libraries. This can take a minute...
  .venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt && copy /y requirements.txt .venv\installed-requirements.txt >nul
)
.venv\Scripts\python app.py
rem Closing the browser tab ends the app normally and this window closes; keep it open only to show an error
if errorlevel 1 pause
