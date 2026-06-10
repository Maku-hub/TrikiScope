@echo off
rem Launcher for TrikiScope - no venv activation needed.
rem Double-click to run with auto-connect, or pass your own args:
rem   run.bat --scan
rem   run.bat --mode complementary
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] Virtual environment not found.
    echo     Create it first:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if "%~1"=="" (
    ".venv\Scripts\python.exe" -m trikiscope --auto-connect
) else (
    ".venv\Scripts\python.exe" -m trikiscope %*
)
