@echo off
rem YouTube changes often. If trailer downloads start failing, close the app and run this.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (echo Run run.bat once first. & pause & exit /b 1)
echo Updating the trailer downloader (yt-dlp)...
.venv\Scripts\python -m pip install --disable-pip-version-check -q --upgrade "yt-dlp[default]" deno
echo Done. Start the app again with run.bat.
pause
