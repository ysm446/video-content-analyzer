@echo off
chcp 65001 > nul
echo =============================================
echo  Video Content Analyzer
echo =============================================
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [ERROR] ".venv" が見つかりません。先に setup_python.bat を実行してください。
    pause
    exit /b 1
)
call "%~dp0.venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] ".venv" を有効化できません（ベース Python が壊れている可能性）。setup_python.bat で作り直してください。
    pause
    exit /b 1
)

echo [1/1] Starting Electron frontend...
echo Backend process is managed by Electron and will stop automatically on app exit.
cd /d "%~dp0"
npm start

pause
