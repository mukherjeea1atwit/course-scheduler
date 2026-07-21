@echo off
setlocal

:: WIT Class Scheduler — launcher
:: Starts the local server and opens it in your default browser.
:: Run install.bat first if you haven't already.

set "APP_DIR=%~dp0.."
set "VENV_PY=%APP_DIR%\venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment not found.
    echo Please run install.bat first.
    pause
    exit /b 1
)

echo Starting WIT Class Scheduler...
start "WIT Class Scheduler - Server" /min "%VENV_PY%" "%APP_DIR%\server.py"

:: Give the server a moment to start, then open the browser.
timeout /t 3 /nobreak >nul
start "" http://localhost:8000

echo.
echo The scheduler is running in a minimized window titled
echo "WIT Class Scheduler - Server". Close that window to stop it.
echo.
pause
