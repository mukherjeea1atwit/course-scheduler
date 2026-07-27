@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem ===========================================================================
rem  WIT Class Scheduler - launcher
rem  Starts the local server and opens it in your default browser.
rem  Run install.bat first if you have not already.
rem
rem  Must be saved with CRLF line endings and plain ASCII (see .gitattributes).
rem ===========================================================================

rem Normalise "...\windows\.." into a real path.
pushd "%~dp0.." || goto :nofolder
set "APP_DIR=%CD%"
popd

set "VENV_PY=%APP_DIR%\venv\Scripts\python.exe"
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"

if not exist "%VENV_PY%" (
    echo [ERROR] The scheduler is not installed yet, or the install is damaged.
    echo Expected to find:
    echo    %VENV_PY%
    echo.
    echo Please run install.bat and let it finish, then try again.
    echo.
    pause
    exit /b 1
)

rem A server left over from a previous launch keeps port 8000 busy, which makes
rem the new one exit immediately and the browser show a stale or dead page.
echo Stopping any copy that is already running...
%PS% "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith('%APP_DIR%', [System.StringComparison]::OrdinalIgnoreCase) -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo Starting WIT Class Scheduler...
start "WIT Class Scheduler - Server" /min "%VENV_PY%" "%APP_DIR%\server.py"

rem Wait for the server to actually accept connections instead of guessing with
rem a fixed sleep, so the browser never opens on a page that is not up yet.
set "READY="
for /f "usebackq delims=" %%r in (`%PS% "$ok = $false; foreach ($i in 1..30) { try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 8000); $c.Close(); $ok = $true; break } catch { Start-Sleep -Milliseconds 500 } }; if ($ok) { 'READY' } else { 'TIMEOUT' }" 2^>nul`) do set "READY=%%r"

if /i "%READY%"=="READY" (
    start "" http://localhost:8000
    echo.
    echo The scheduler is running in a minimized window titled
    echo    "WIT Class Scheduler - Server"
    echo Close that window when you are finished, to stop it.
    echo If your browser did not open, go to: http://localhost:8000
) else (
    echo.
    echo [ERROR] The server did not start.
    echo Look at the minimized window titled "WIT Class Scheduler - Server"
    echo for the error message - it may have closed already.
    echo.
    echo The usual fix is to re-run install.bat, which reinstalls everything
    echo cleanly.
)

echo.
pause
exit /b 0

:nofolder
echo [ERROR] Could not find the scheduler folder.
echo This file must stay in the "windows" folder inside the project.
echo.
pause
exit /b 1
