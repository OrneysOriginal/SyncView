@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_tools\build_windows.ps1"
if errorlevel 1 (
    echo.
    echo Build failed. Check the error above.
    pause
    exit /b 1
)
echo.
echo Open dist\SyncWatch\SyncWatch.exe to start the application.
start "" explorer.exe "%~dp0dist\SyncWatch"
pause
