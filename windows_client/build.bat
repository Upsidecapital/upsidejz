@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo  GreymatterAI - Windows EXE Builder
echo ============================================================
echo.

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Please install Python 3.11+ and ensure it is in PATH.
    pause
    exit /b 1
)

echo [1/3] Installing dependencies...
pip install pyinstaller PyQt6==6.7.1 PyQt6-WebEngine==6.7.0 requests==2.32.3
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection and Python environment.
    pause
    exit /b 1
)

echo.
echo [2/3] Building GreymatterAI.exe...
pyinstaller --noconfirm greymatter.spec
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See output above for details.
    pause
    exit /b 1
)

echo.
echo [3/3] Build complete!
echo.
echo ============================================================
echo  Output: dist\GreymatterAI\GreymatterAI.exe
echo ============================================================
echo.
echo To distribute:
echo   Zip the entire dist\GreymatterAI\ folder and share it.
echo   The recipient does NOT need Python installed.
echo.
pause
