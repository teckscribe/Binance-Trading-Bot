@echo off
echo ======================================================
echo    CSB Backtest Engine - Virtual Environment Setup
echo ======================================================
echo.

cd /d "%~dp0"

if exist ".venv" (
    echo [!] .venv already exists. Delete it first to rebuild.
    echo     rmdir /s /q .venv
    echo.
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
    echo ERROR: Failed to create venv. Make sure Python 3.10+ is installed.
    pause
    exit /b 1
)

echo [2/3] Upgrading pip...
.venv\Scripts\python.exe -m pip install --upgrade pip

echo [3/3] Installing requirements...
.venv\Scripts\pip.exe install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Some packages failed to install.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo    Setup complete! Virtual environment ready.
echo ======================================================
echo.
echo  Next steps:
echo    1. Copy your .env file or edit the existing one
echo    2. Run fetch_data.bat to download market data
echo    3. Run run_tier1.bat or run_tier2.bat
echo.
pause
