@echo off
echo ======================================================
echo    CSB Backtest Engine - Tier 1 Portfolio Backtest
echo    (15m bars - fast screening)
echo ======================================================
echo.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe

if not exist "%PY%" (
    echo ERROR: Virtual environment not found. Run setup_venv.bat first.
    pause
    exit /b 1
)

REM Load defaults from .env
set CAPITAL=100
set SLOTS=3
set MARGIN=0.60

if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /r "^ACCOUNT_EQUITY_USDT=" .env') do set CAPITAL=%%b
    for /f "tokens=1,2 delims==" %%a in ('findstr /r "^MAX_CONCURRENT=" .env') do set SLOTS=%%b
    for /f "tokens=1,2 delims==" %%a in ('findstr /r "^MAX_TOTAL_MARGIN_PCT=" .env') do set MARGIN=%%b
)

echo   .env defaults: capital=%CAPITAL%  slots=%SLOTS%  margin=%MARGIN%
echo.

if "%~1"=="" (
    echo   No args - using production defaults:
    echo   strategies=CSM,VRP,LIQ  days=30  capital=%CAPITAL%
    echo   slots=%SLOTS%  margin=%MARGIN%
    echo.
    echo ──────────────────────────────────────────────────────
    "%PY%" backtest_optimizer.py --days 30 --capital %CAPITAL% --strategies CSM,VRP,LIQ --slots %SLOTS% --margin %MARGIN%
) else (
    echo   Custom args: %*
    echo.
    echo ──────────────────────────────────────────────────────
    "%PY%" backtest_optimizer.py %*
)

echo.
echo ======================================================
echo   Tier 1 backtest complete.
echo ======================================================
echo.
echo   Examples:
echo     run_tier1.bat                              (30d, default)
echo     run_tier1.bat --days 30 --last-days 7      (last 7d only)
echo     run_tier1.bat --strategies CSM             (single strategy)
echo     run_tier1.bat --strategies CSM,VRP,LIQ --no-regime
echo.
pause
