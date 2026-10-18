@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 main.py
    exit /b %errorlevel%
)

where python >nul 2>&1
if %errorlevel%==0 (
    python main.py
    exit /b %errorlevel%
)

echo Python 3 was not found. Install Python 3 with tkinter support, then run this file again.
pause
exit /b 1
