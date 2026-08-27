@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"
echo =============================================
echo  Video Content Analyzer - Python setup
echo =============================================
echo.
echo プロジェクト内 runtime\python\ にスタンドアロン CPython 3.10 を同梱し、
echo そこから .venv を作成して依存パッケージをインストールします。
echo （システムの Python / conda には依存しません）
echo.

set "PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20260825/cpython-3.10.21%%2B20260825-x86_64-pc-windows-msvc-install_only.tar.gz"
set "PY_DIR=%~dp0runtime\python"
set "PY_EXE=%PY_DIR%\python\python.exe"

if exist "%PY_EXE%" (
    echo [1/3] 同梱 Python は取得済み: "%PY_EXE%"
) else (
    echo [1/3] 同梱 Python をダウンロード中...
    if not exist "%PY_DIR%" mkdir "%PY_DIR%"
    curl -L -o "%PY_DIR%\python.tar.gz" "%PY_URL%"
    if errorlevel 1 ( echo [ERROR] ダウンロードに失敗しました & pause & exit /b 1 )
    tar -xzf "%PY_DIR%\python.tar.gz" -C "%PY_DIR%"
    if errorlevel 1 ( echo [ERROR] 展開に失敗しました & pause & exit /b 1 )
    del "%PY_DIR%\python.tar.gz"
)
"%PY_EXE%" --version

echo [2/3] .venv を作成中...
if exist ".venv" (
    echo   既存の .venv を削除して作り直します
    rmdir /s /q ".venv"
)
"%PY_EXE%" -m venv .venv
if errorlevel 1 ( echo [ERROR] venv の作成に失敗しました & pause & exit /b 1 )

echo [3/3] 依存パッケージをインストール中（torch は cu130 ホイール）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install torch --index-url https://download.pytorch.org/whl/cu130
if errorlevel 1 ( echo [ERROR] torch のインストールに失敗しました & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] 依存パッケージのインストールに失敗しました & pause & exit /b 1 )

echo.
echo 完了しました。start.bat で起動できます。
pause
