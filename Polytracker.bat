@echo off
REM ============================================================
REM  UPSIDE - POLYTRACKER
REM  Double-click this file to install and run the bot.
REM  No command line required.
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

title Upside - Polytracker
color 0A

echo.
echo  ============================================================
echo    UPSIDE - POLYTRACKER
echo    Polymarket Latency Arbitrage Bot
echo  ============================================================
echo.

REM --- Check for Python ---
where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    where py >nul 2>nul
    if %ERRORLEVEL% NEQ 0 (
        echo  [ERROR] Python is not installed on this system.
        echo.
        echo  Please install Python 3.11+ from:
        echo    https://www.python.org/downloads/
        echo.
        echo  IMPORTANT: During install, check the box that says
        echo  "Add python.exe to PATH" on the first screen.
        echo.
        pause
        exit /b 1
    )
    set PYTHON=py
) else (
    set PYTHON=python
)

REM --- Check Python version ---
%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo  [ERROR] Python 3.11 or higher is required.
    echo  Your version:
    %PYTHON% --version
    echo.
    echo  Download the latest Python from:
    echo    https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo  [OK] Python found:
%PYTHON% --version
echo.

REM --- Create virtual environment on first run ---
if not exist ".venv\Scripts\activate.bat" (
    echo  [1/3] First-time setup: creating virtual environment...
    %PYTHON% -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
    echo.
)

REM --- Activate venv ---
call ".venv\Scripts\activate.bat"

REM --- Install / update dependencies ---
if not exist ".venv\.deps_installed" (
    echo  [2/3] Installing dependencies ^(one-time, ~2 min^)...
    python -m pip install --upgrade pip >nul 2>nul
    python -m pip install -r polytracker\requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo  [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo. > ".venv\.deps_installed"
    echo  [OK] Dependencies installed.
    echo.
) else (
    echo  [OK] Dependencies already installed.
    echo.
)

REM --- Create .env from template if missing ---
if not exist ".env" (
    if exist "polytracker\.env.example" (
        copy "polytracker\.env.example" ".env" >nul
        echo  [INFO] Created .env file. Edit it to add your API keys.
        echo.
    )
)

REM --- Launch the bot (native desktop window) ---
echo  [3/3] Launching Polytracker Dashboard...
echo.
echo  The dashboard will open in a native desktop window.
echo  Close this window or press Ctrl+C to stop the bot.
echo.
echo  ============================================================
echo.

python launcher.py %*

echo.
echo  Polytracker has stopped.
pause
endlocal
