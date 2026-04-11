@echo off
REM ============================================================
REM  Build Polytracker.exe for distribution to clients
REM  Requires: PyInstaller (installed automatically)
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo  ============================================================
echo    BUILDING Polytracker.exe
echo  ============================================================
echo.

REM --- Ensure venv exists ---
if not exist ".venv\Scripts\activate.bat" (
    echo  [1/4] Creating virtual environment...
    python -m venv .venv
)
call ".venv\Scripts\activate.bat"

REM --- Install base deps ---
echo  [2/4] Installing base dependencies...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install -r polytracker\requirements.txt

REM --- Install PyInstaller ---
echo  [3/4] Installing PyInstaller...
python -m pip install pyinstaller

REM --- Build ---
echo  [4/4] Building executable (this takes ~2-5 minutes)...
echo.
pyinstaller --clean --noconfirm polytracker.spec

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERROR] Build failed. Check output above.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo    BUILD COMPLETE
echo  ============================================================
echo.
echo  Your executable is at:
echo    dist\Polytracker.exe
echo.
echo  Ship this single file to your clients. They don't need
echo  Python or anything else installed.
echo.
echo  First run: Windows may show a SmartScreen warning
echo  (it's unsigned). Click "More info" then "Run anyway".
echo.
pause
endlocal
