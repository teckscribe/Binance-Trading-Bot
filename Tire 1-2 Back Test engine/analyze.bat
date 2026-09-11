@echo off
echo ======================================================
echo    CSB Backtest Engine - Tier 2 Results Analyzer
echo ======================================================
echo.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe

if not exist "%PY%" (
    echo ERROR: Virtual environment not found. Run setup_venv.bat first.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo Usage: analyze.bat ^<trades.json^> [--no-live-compare]
    echo.
    echo Available result files:
    dir /b /o-d results\*.json 2>nul
    echo.
    pause
    exit /b 1
)

set TRADES=%~1
if not exist "%TRADES%" (
    echo ERROR: File not found: %TRADES%
    pause
    exit /b 1
)

REM Load equity from .env
set CAPITAL=100
if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /r "^ACCOUNT_EQUITY_USDT=" .env') do set CAPITAL=%%b
)

echo   Trades file : %TRADES%
echo   Capital     : %CAPITAL%
echo.
echo ──────────────────────────────────────────────────────

"%PY%" tools\replay\analyze.py "%TRADES%" --capital %CAPITAL% %2 %3

echo.
pause
