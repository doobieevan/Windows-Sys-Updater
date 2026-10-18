@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if not %errorlevel%==0 (
    echo Python launcher ^(py^) was not found.
    pause
    exit /b 1
)

py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo PyInstaller installation or upgrade failed.
    pause
    exit /b 1
)

py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name AppUpdater main.py
if errorlevel 1 (
    echo Build failed.
    pause
    exit /b 1
)

echo Build complete: dist\AppUpdater.exe
pause
