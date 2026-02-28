@echo off
echo =============================================
echo  Movie Review
echo =============================================
echo.

set HF_HOME=%~dp0models

call conda activate main
if errorlevel 1 (
    echo [ERROR] Failed to activate conda env "main".
    pause
    exit /b 1
)

echo [1/2] Starting backend... (http://127.0.0.1:8765)
start "LCP Backend" cmd /k "conda activate main && set HF_HOME=%~dp0models && python "%~dp0run_backend.py""

echo [2/2] Waiting for models to load...
timeout /t 10 /nobreak > nul

echo [2/2] Starting Electron frontend...
cd /d "%~dp0"
npm start

pause
