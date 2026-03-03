@echo off
chcp 65001 > nul
echo =============================================
echo  Video Content Analyzer
echo =============================================
echo.

call conda activate main
if errorlevel 1 (
    echo [ERROR] Failed to activate conda env "main".
    pause
    exit /b 1
)

echo [1/1] Starting Electron frontend...
echo Backend process is managed by Electron and will stop automatically on app exit.
cd /d "%~dp0"
npm start

pause
