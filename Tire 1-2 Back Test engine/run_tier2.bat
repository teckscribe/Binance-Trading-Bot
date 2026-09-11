@echo off
echo ======================================================
echo    CSB Backtest Engine - Tier 2 Live Replay
echo    (1m bars - faithful to production)
echo ======================================================
echo.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe

if not exist "%PY%" (
    echo ERROR: Virtual environment not found. Run setup_venv.bat first.
    pause
    exit /b 1
)

if not exist "results" mkdir results

REM Load equity from .env
set EQUITY=100
if exist ".env" (
    for /f "tokens=1,2 delims==" %%a in ('findstr /r "^ACCOUNT_EQUITY_USDT=" .env') do set EQUITY=%%b
)

REM Generate timestamped output file
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /format:list 2^>nul') do set DT=%%I
set TIMESTAMP=%DT:~0,8%_%DT:~8,6%
set OUT_FILE=results\replay_%TIMESTAMP%.json

echo   .env equity : %EQUITY%
echo   Output      : %OUT_FILE%
echo.

if "%~1"=="" (
    echo   No args - 30d full replay with resume + checkpoints:
    echo.
    echo ──────────────────────────────────────────────────────
    "%PY%" -u tools\replay\driver.py --days 30 --equity %EQUITY% --out "%OUT_FILE%" --resume --checkpoint-every 1440 --progress-every 360 --skip-drift-check
) else (
    echo   Custom args: %*
    echo.
    echo ──────────────────────────────────────────────────────
    "%PY%" -u tools\replay\driver.py --days 30 --equity %EQUITY% --out "%OUT_FILE%" --resume --checkpoint-every 1440 --progress-every 360 --skip-drift-check %*
)

echo.
if exist "%OUT_FILE%" (
    echo ======================================================
    echo   Tier 2 replay complete. Trades saved to:
    echo   %OUT_FILE%
    echo ======================================================
    echo.
    echo   To analyze results, run:
    echo     analyze.bat %OUT_FILE%
) else (
    echo   WARNING: No trades file produced.
)
echo.
echo   Examples:
echo     run_tier2.bat                              (30d full replay)
echo     run_tier2.bat --last-days 7                (last 7d only)
echo     run_tier2.bat --limit-hours 2              (2h smoke test)
echo     run_tier2.bat --symbols BTCUSDT,ETHUSDT
echo.
pause
