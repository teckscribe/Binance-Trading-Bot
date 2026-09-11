@echo off
echo ======================================================
echo    CSB Backtest Engine - Fetch 1m Data from Binance
echo ======================================================
echo.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe

if not exist "%PY%" (
    echo ERROR: Virtual environment not found. Run setup_venv.bat first.
    pause
    exit /b 1
)

set DAYS=%1
if "%DAYS%"=="" set DAYS=30

set SYMBOLS=%2
set WORKERS=8

echo   Days    : %DAYS%
echo   Workers : %WORKERS%
if not "%SYMBOLS%"=="" (
    echo   Symbols : %SYMBOLS%
) else (
    echo   Symbols : full watchlist
)
echo.

if not "%SYMBOLS%"=="" (
    "%PY%" tools\fetch_1m.py --days %DAYS% --workers %WORKERS% --symbols %SYMBOLS%
) else (
    "%PY%" tools\fetch_1m.py --days %DAYS% --workers %WORKERS%
)

echo.
echo ======================================================
echo   Data fetch complete. Files saved to data\ folder.
echo ======================================================
echo.

REM Check regime symbols
set MISSING=
if not exist "data\BTCUSDT_1m_%DAYS%d.csv" set MISSING=%MISSING% BTCUSDT
if not exist "data\ETHUSDT_1m_%DAYS%d.csv" set MISSING=%MISSING% ETHUSDT
if not exist "data\SOLUSDT_1m_%DAYS%d.csv" set MISSING=%MISSING% SOLUSDT

if not "%MISSING%"=="" (
    echo   WARNING: Missing regime symbols:%MISSING%
    echo   Backtests need BTC/ETH/SOL for regime classification!
) else (
    echo   Regime symbols (BTC/ETH/SOL) present.
)
echo.
pause
