@echo off
setlocal enabledelayedexpansion

:: WIT Class Scheduler — Windows installer
:: Downloads the project code and installs its Python dependencies into a
:: private virtual environment. Run this once; afterwards use run.bat.

set "REPO_URL=https://github.com/mukherjeea1atwit/course-scheduler.git"
set "INSTALL_DIR=%USERPROFILE%\WIT-Class-Scheduler"
set "APP_DIR=%INSTALL_DIR%\course-scheduler"

echo ============================================
echo   WIT Class Scheduler - Installer
echo ============================================
echo.

:: Some corporate networks/older Windows builds only negotiate old TLS
:: versions by default, which makes PowerShell's Invoke-WebRequest fail
:: silently against github.com/python.org. Force TLS 1.2 for this session.
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12" >nul 2>nul

:: ── Check for Python; auto-install a fresh copy if it's missing ─────────────
:: NOTE: on many Windows machines, typing "python" with no real Python
:: installed launches a Microsoft Store stub instead of an error. "where"
:: still finds that stub on PATH, so we double-check it actually runs.
set "PY="
where python >nul 2>nul
if not errorlevel 1 (
    python --version >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 --version >nul 2>nul
        if not errorlevel 1 set "PY=py -3"
    )
)

if not defined PY (
    echo Python was not found on this computer - installing it automatically...
    echo This does not need Administrator rights or IT approval.
    echo.

    set "PYEXE_TMP=%INSTALL_DIR%\python-installer.exe"
    if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
    echo Downloading the official Python installer from python.org...
    powershell -NoProfile -Command ^
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%PYEXE_TMP%'"
    if not errorlevel 1 if exist "%PYEXE_TMP%" (
        echo Installing Python silently for your user account only ^(no admin needed^)...
        "%PYEXE_TMP%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1
        del "%PYEXE_TMP%" >nul 2>nul
    ) else (
        echo Direct download failed - trying winget instead...
        where winget >nul 2>nul
        if not errorlevel 1 (
            winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        )
    )

    :: The Python installer registers the "py" launcher in C:\Windows, which is
    :: always on PATH, so it's usable immediately without restarting this shell —
    :: unlike a plain PATH update, which this already-running cmd.exe won't see.
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 --version >nul 2>nul
        if not errorlevel 1 set "PY=py -3"
    )
    if not defined PY (
        where python >nul 2>nul
        if not errorlevel 1 (
            python --version >nul 2>nul
            if not errorlevel 1 set "PY=python"
        )
    )
    if not defined PY (
        echo.
        echo [ERROR] Python installation did not complete successfully.
        echo This can happen if your IT department blocks software installs.
        echo Please install Python manually from https://www.python.org/downloads/
        echo ^(check the box "Add python.exe to PATH" during setup^), then
        echo close this window and re-run install.bat.
        echo If that is also blocked, ask your IT department to install
        echo Python 3.10 or newer for you.
        pause
        exit /b 1
    )
)

for /f "tokens=2 delims= " %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

:: ── Check for git; fall back to a zip download if missing ──────────────────
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

where git >nul 2>nul
if errorlevel 1 (
    echo Git not found - downloading the project as a zip instead...
    powershell -NoProfile -Command ^
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/mukherjeea1atwit/course-scheduler/archive/refs/heads/main.zip' -OutFile '%INSTALL_DIR%\repo.zip'; Expand-Archive -Path '%INSTALL_DIR%\repo.zip' -DestinationPath '%INSTALL_DIR%' -Force"
    if errorlevel 1 (
        echo [ERROR] Download failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
    del "%INSTALL_DIR%\repo.zip"
    for /d %%d in ("%INSTALL_DIR%\course-scheduler-*") do ren "%%d" "course-scheduler"
) else (
    if exist "%INSTALL_DIR%\course-scheduler\.git" (
        echo Project already downloaded - updating to the latest version...
        pushd "%INSTALL_DIR%\course-scheduler"
        git pull
        popd
    ) else (
        echo Downloading project code...
        git clone "%REPO_URL%" "%INSTALL_DIR%\course-scheduler"
        if errorlevel 1 (
            echo [ERROR] git clone failed. Check your internet connection and try again.
            pause
            exit /b 1
        )
    )
)

if not exist "%APP_DIR%\server.py" (
    echo [ERROR] Could not find server.py in the downloaded project.
    pause
    exit /b 1
)

:: ── Create virtual environment ──────────────────────────────────────────────
echo.
echo Setting up a private Python environment...
%PY% -m venv "%APP_DIR%\venv"
if not exist "%APP_DIR%\venv\Scripts\python.exe" (
    echo [ERROR] Failed to create the virtual environment.
    echo Your Python install may be missing the "venv" module.
    pause
    exit /b 1
)

:: ── Install dependencies ─────────────────────────────────────────────────────
echo Installing required packages ^(this may take a minute^)...
"%APP_DIR%\venv\Scripts\python.exe" -m pip install --upgrade pip >nul
"%APP_DIR%\venv\Scripts\python.exe" -m pip install -r "%APP_DIR%\requirements.txt"
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    echo If this mentions a connection/SSL error, your network may block
    echo PyPI ^(pypi.org^) - ask your IT department to allow access, or
    echo try again on a different network.
    pause
    exit /b 1
)

:: ── Drop a Start-Scheduler shortcut on the Desktop ──────────────────────────
:: Not fatal if this fails (e.g. a redirected/read-only Desktop folder) -
:: run.bat in the install folder still works either way.
set "RUN_BAT=%APP_DIR%\windows\run.bat"
set "SHORTCUT=%USERPROFILE%\Desktop\WIT Class Scheduler.lnk"
powershell -NoProfile -Command ^
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%RUN_BAT%'; $s.WorkingDirectory = '%APP_DIR%'; $s.IconLocation = '%SystemRoot%\System32\shell32.dll,220'; $s.Save()" >nul 2>nul
echo.
echo ============================================
echo   Install complete!
echo ============================================
if exist "%SHORTCUT%" (
    echo A "WIT Class Scheduler" shortcut was added to your Desktop.
    echo Double-click it any time to start the scheduler.
) else (
    echo Could not add a Desktop shortcut, but the app is installed.
    echo Start it any time by double-clicking this file:
    echo   %RUN_BAT%
)
echo.
pause
