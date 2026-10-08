@echo off
cd /d "%~dp0"
title Polymarket Parity Arbitrage Bot

echo ====================================================================
echo   Polymarket Parity Arbitrage Bot Launcher
echo ====================================================================

if not exist ".\venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment Python was not found at:
    echo   .\venv\Scripts\python.exe
    echo.
    echo Please make sure the virtual environment is set up.
    echo.
    pause
    exit /b 1
)

.\venv\Scripts\python.exe start_bot.py
set BOT_EXIT_CODE=%ERRORLEVEL%

if %BOT_EXIT_CODE% NEQ 0 (
    echo.
    echo ====================================================================
    echo [ERROR] Polymarket Bot exited with error code %BOT_EXIT_CODE%.
    echo ====================================================================
    echo Check the console output above for error details.
    echo Press any key to close this window...
    pause >nul
)

