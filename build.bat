@echo off
cd /d "%~dp0"
echo Installing requirements...
python -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Building GameShot.exe ...
python build.py
if errorlevel 1 goto fail
echo.
echo Done! Your app is in the "dist" folder: dist\GameShot.exe
pause
exit /b 0
:fail
echo.
echo Something went wrong. Make sure Python 3.9+ is installed and on PATH.
pause
exit /b 1
