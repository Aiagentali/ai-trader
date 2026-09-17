@echo off
rem ai-trader local control panel launcher (Windows)
REM Uses the project virtualenv at %~dp0.venv; create it first with:
REM   py -3 -m venv .venv && .venv\Scripts\pip install -r requirements.txt
REM See docs\LOCAL_PANEL.md for full Windows setup steps.

setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ai-trader] No .venv found. Setting one up with the installed Python...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if errorlevel 1 (
        echo [ai-trader] Could not create a virtual environment. Install Python 3.10+ from python.org and re-run.
        pause
        exit /b 1
    )
    .venv\Scripts\python -m pip install -r requirements.txt
)

".venv\Scripts\python.exe" panel.py --port 8000 %*
pause
