@echo off
rem Build MediaManager.exe for testers: dist\MediaManager\ (the app) and dist\MediaManager-<date>.zip (to send).
rem Needs run.bat to have been run once (it sets up .venv). Your settings and library data are never included.
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (echo Run run.bat once first. & pause & exit /b 1)
echo Installing the build tool (PyInstaller) if needed...
.venv\Scripts\python -m pip install --disable-pip-version-check -q pyinstaller || (pause & exit /b 1)
if not exist packaging\icon.ico .venv\Scripts\python packaging\make_icon.py
echo Building. This takes a few minutes...
.venv\Scripts\python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\MediaManager.spec || (pause & exit /b 1)
copy /y packaging\TESTERS.txt dist\MediaManager\README.txt >nul
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set STAMP=%%d
powershell -NoProfile -Command "Compress-Archive -Path dist\MediaManager -DestinationPath dist\MediaManager-%STAMP%.zip -Force"
echo.
echo Done: dist\MediaManager\MediaManager.exe
echo To send to testers: dist\MediaManager-%STAMP%.zip
pause
