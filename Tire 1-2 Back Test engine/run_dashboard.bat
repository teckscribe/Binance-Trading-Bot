@echo off
echo ======================================================
echo    CSB Backtest Dashboard - Web Server
echo    http://localhost:8200
echo ======================================================
echo.

cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe

if not exist "%PY%" (
    echo ERROR: Virtual environment not found. Run setup_venv.bat first.
    pause
    exit /b 1
)

echo   Starting web server on port 8200...
echo   Open http://localhost:8200 in your browser.
echo   Press Ctrl+C to stop.
echo.
echo ──────────────────────────────────────────────────────

"%PY%" backtest_server.py
pause
