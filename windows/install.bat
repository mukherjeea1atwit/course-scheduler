@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem ===========================================================================
rem  WIT Class Scheduler - Windows installer
rem
rem  Performs a CLEAN install: removes any previous installation (code, virtual
rem  environment, desktop shortcuts, running servers), then downloads a fresh
rem  copy of the project and installs all required packages into a private
rem  virtual environment.
rem
rem  Safe to run any number of times. No Administrator rights required.
rem
rem  IMPORTANT MAINTENANCE NOTES (do not "simplify" these away):
rem   * This file MUST be saved with CRLF line endings and plain ASCII only.
rem     .gitattributes enforces that. cmd.exe is unreliable with LF-only batch
rem     files, and non-ASCII bytes get mangled under legacy OEM codepages.
rem   * The script copies itself to %TEMP% and re-runs from there before it
rem     touches the install folder. cmd.exe reads a batch file by byte offset
rem     as it executes; if the file is overwritten mid-run (which is exactly
rem     what happens when a returning user runs the copy inside the installed
rem     repo and we then re-download the project) cmd resumes at a garbage
rem     offset and silently kills the window. That was the "terminal opens and
rem     immediately closes" bug.
rem   * Keep long PowerShell commands on a single line. Caret (^) continuation
rem     is fragile and interacts badly with quoting.
rem ===========================================================================

rem --- Re-run from a private copy so we can never overwrite ourselves --------
if /i "%~1"=="--relaunched" goto :main
set "SELF_COPY=%TEMP%\wit-scheduler-installer-%RANDOM%%RANDOM%.bat"
set "DONE_FLAG=%SELF_COPY%.done"
copy /y "%~f0" "%SELF_COPY%" >nul 2>nul
if not exist "%SELF_COPY%" goto :main
call "%SELF_COPY%" --relaunched "%DONE_FLAG%"
set "RC=%ERRORLEVEL%"
del "%SELF_COPY%" >nul 2>nul
rem The copy signals a clean finish by creating DONE_FLAG. If it is absent the
rem script died without reaching either exit, so say something rather than
rem letting the window disappear the way the old installer did.
if exist "%DONE_FLAG%" (
    del "%DONE_FLAG%" >nul 2>nul
    exit /b %RC%
)
echo.
echo ============================================
echo   INSTALL STOPPED UNEXPECTEDLY
echo ============================================
echo.
echo The installer quit before it finished, and before it could explain why.
echo Please send this file to whoever shared this app with you:
echo    %USERPROFILE%\WIT-Class-Scheduler\install-log.txt
echo.
pause
exit /b 1

:main
set "DONE_FLAG=%~2"
set "REPO_URL=https://github.com/mukherjeea1atwit/course-scheduler.git"
set "REPO_ZIP=https://github.com/mukherjeea1atwit/course-scheduler/archive/refs/heads/main.zip"
set "INSTALL_DIR=%USERPROFILE%\WIT-Class-Scheduler"
set "APP_DIR=%INSTALL_DIR%\course-scheduler"
set "VENV_PY=%APP_DIR%\venv\Scripts\python.exe"
set "RUN_BAT=%APP_DIR%\windows\run.bat"
set "STAMP=%RANDOM%%RANDOM%"
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"
set "TLS=$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;"
set "FAILMSG="
set "DATA_BACKUP="

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" >nul 2>nul
set "LOG=%INSTALL_DIR%\install-log.txt"
if not exist "%INSTALL_DIR%" set "LOG=%TEMP%\wit-scheduler-install-log.txt"
break > "%LOG%" 2>nul

echo ============================================
echo   WIT Class Scheduler - Installer
echo ============================================
echo.
echo This performs a clean install. Any previous copy is removed first.
echo.
call :log "Install started. Target: %INSTALL_DIR%"

call :find_python
if defined FAILMSG goto :fail

call :stop_running
call :backup_data
call :wipe_previous
if defined FAILMSG goto :fail

call :download
if defined FAILMSG goto :fail

call :make_venv
if defined FAILMSG goto :fail

call :install_deps
if defined FAILMSG goto :fail

call :verify
if defined FAILMSG goto :fail

call :make_shortcut

echo.
echo ============================================
echo   Install complete!
echo ============================================
echo.
if defined SHORTCUT_DIR (
    echo A "WIT Class Scheduler" shortcut was added to your Desktop.
    echo Double-click it any time to start the scheduler.
    echo    Desktop folder: %SHORTCUT_DIR%
) else (
    echo Could not add a Desktop shortcut, but the app is installed.
    echo Start it any time by double-clicking this file:
    echo    %RUN_BAT%
)
echo.
if defined DATA_BACKUP (
    echo NOTE: this was a clean install, so the input files were reset to the
    echo defaults that ship with the project. Your previous input files were
    echo saved here, in case you had edited them:
    echo    %DATA_BACKUP%
    echo To keep your old data, copy those files over the ones in:
    echo    %APP_DIR%\data
    echo.
)
echo Log file: %LOG%
echo.
call :finish
pause
exit /b 0


rem ===========================================================================
rem  Subroutines
rem ===========================================================================

:log
>>"%LOG%" echo(%~1
goto :eof

:say
echo(%~1
>>"%LOG%" echo(%~1
goto :eof


rem --- Locate a usable Python, installing one if necessary -------------------
:find_python
set "PY="
call :try_py "py -3"
call :try_py "python"
call :try_py "python3"
if defined PY goto :found_python

call :say "Python was not found on this computer - installing it now..."
call :say "This is a per-user install; no Administrator rights are needed."
set "PYEXE=%TEMP%\python-installer-%STAMP%.exe"
call :say "Downloading Python from python.org (about 25 MB)..."
%PS% "%TLS% Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%PYEXE%'" >>"%LOG%" 2>&1

if exist "%PYEXE%" (
    call :say "Installing Python quietly..."
    "%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 >>"%LOG%" 2>&1
    del "%PYEXE%" >nul 2>nul
) else (
    call :say "Direct download failed - trying winget instead..."
    where winget >nul 2>nul
    if not errorlevel 1 winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >>"%LOG%" 2>&1
)

rem The py launcher lands in C:\Windows (always on PATH). A plain PATH update
rem is not visible to this already-running shell, so also probe the standard
rem per-user install locations directly.
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts"
call :try_py "py -3"
call :try_py "python"
call :try_py "python3"
if defined PY goto :found_python

set "FAILMSG=Python could not be installed automatically. Install it by hand from https://www.python.org/downloads/ - tick the Add python.exe to PATH box during setup - then re-run this installer."
goto :eof

:found_python
for /f "tokens=2 delims= " %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
call :say "Using Python %PYVER%  (%PY%)"
goto :eof

rem Probe one candidate interpreter. Rejects the Microsoft Store stub (which is
rem on PATH even with no real Python installed) because the stub exits non-zero,
rem and rejects anything older than 3.9.
:try_py
if defined PY goto :eof
%~1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >>"%LOG%" 2>&1
if errorlevel 1 goto :eof
set "PY=%~1"
goto :eof


rem --- Stop any server still running out of the install folder ---------------
rem A running python.exe holds a lock on files inside venv, which would make
rem the delete below fail with "Access is denied".
:stop_running
call :say "Checking for a running copy of the scheduler..."
%PS% "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith('%INSTALL_DIR%', [System.StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >>"%LOG%" 2>&1
goto :eof


rem --- Preserve the user's edited input files before wiping ------------------
:backup_data
if not exist "%APP_DIR%\data" goto :eof
set "DATA_BACKUP=%INSTALL_DIR%\previous-data-%STAMP%"
call :say "Saving a copy of your existing input files..."
xcopy "%APP_DIR%\data" "%DATA_BACKUP%\" /e /i /q /y >>"%LOG%" 2>&1
if errorlevel 1 set "DATA_BACKUP="
if defined DATA_BACKUP call :log "Data backed up to %DATA_BACKUP%"
goto :eof


rem --- Remove every trace of a previous installation -------------------------
:wipe_previous
if not exist "%APP_DIR%" goto :wipe_extras
call :say "Removing the previous installation..."
rem Git marks files under .git\objects read-only; rd /s /q refuses those.
attrib -r -s -h "%APP_DIR%\*" /s /d >nul 2>nul
rd /s /q "%APP_DIR%" >nul 2>nul
if exist "%APP_DIR%" (
    rem Second attempt: a lingering handle sometimes clears a moment later.
    %PS% "Start-Sleep -Milliseconds 1500; Remove-Item -LiteralPath '%APP_DIR%' -Recurse -Force -ErrorAction SilentlyContinue" >>"%LOG%" 2>&1
)
if exist "%APP_DIR%" (
    set "FAILMSG=Could not delete the old installation at %APP_DIR%. Close any window or File Explorer using that folder, then re-run this installer."
    goto :eof
)

:wipe_extras
rem Leftovers from older versions of this installer.
for /d %%d in ("%INSTALL_DIR%\course-scheduler-*") do rd /s /q "%%d" >nul 2>nul
del /q "%INSTALL_DIR%\repo.zip" >nul 2>nul
del /q "%INSTALL_DIR%\python-installer.exe" >nul 2>nul
rd /s /q "%INSTALL_DIR%\_zip" >nul 2>nul

rem Old desktop shortcuts, on both the plain and the OneDrive-redirected Desktop.
%PS% "$ws = New-Object -ComObject WScript.Shell; @($ws.SpecialFolders('Desktop'), (Join-Path $env:USERPROFILE 'Desktop'), (Join-Path $env:USERPROFILE 'OneDrive\Desktop')) | Where-Object { $_ } | Select-Object -Unique | ForEach-Object { Remove-Item -LiteralPath (Join-Path $_ 'WIT Class Scheduler.lnk') -Force -ErrorAction SilentlyContinue }" >>"%LOG%" 2>&1
goto :eof


rem --- Fetch a fresh copy of the project -------------------------------------
:download
call :say "Downloading the project..."
where git >nul 2>nul
if errorlevel 1 goto :download_zip

git clone --depth 1 "%REPO_URL%" "%APP_DIR%" >>"%LOG%" 2>&1
if exist "%APP_DIR%\server.py" goto :download_done
call :say "git clone did not work - falling back to a direct download..."
rd /s /q "%APP_DIR%" >nul 2>nul

:download_zip
set "ZIPFILE=%INSTALL_DIR%\repo-%STAMP%.zip"
set "ZIPDIR=%INSTALL_DIR%\_zip-%STAMP%"
%PS% "%TLS% Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%ZIPFILE%'" >>"%LOG%" 2>&1
if not exist "%ZIPFILE%" (
    set "FAILMSG=Could not download the project. Check your internet connection. If you are on a work network, ask IT to allow github.com, python.org and pypi.org - or try again on a personal hotspot."
    goto :eof
)
%PS% "Expand-Archive -LiteralPath '%ZIPFILE%' -DestinationPath '%ZIPDIR%' -Force" >>"%LOG%" 2>&1
del /q "%ZIPFILE%" >nul 2>nul
for /d %%d in ("%ZIPDIR%\*") do move "%%d" "%APP_DIR%" >>"%LOG%" 2>&1
rd /s /q "%ZIPDIR%" >nul 2>nul

:download_done
if not exist "%APP_DIR%\server.py" (
    set "FAILMSG=The project did not download correctly - server.py is missing. Check your internet connection and re-run this installer."
    goto :eof
)
if not exist "%APP_DIR%\requirements.txt" (
    set "FAILMSG=The project did not download correctly - requirements.txt is missing. Check your internet connection and re-run this installer."
    goto :eof
)
call :say "Project downloaded."
goto :eof


rem --- Build a fresh virtual environment -------------------------------------
rem Always built from scratch: "python -m venv" over an existing venv is a
rem no-op, so a venv left pointing at a Python that has since been upgraded or
rem removed would stay broken forever.
:make_venv
call :say "Setting up a private Python environment..."
rd /s /q "%APP_DIR%\venv" >nul 2>nul
%PY% -m venv "%APP_DIR%\venv" >>"%LOG%" 2>&1
if exist "%VENV_PY%" goto :eof
call :say "Standard setup failed - retrying without pip bootstrap..."
rd /s /q "%APP_DIR%\venv" >nul 2>nul
%PY% -m venv --without-pip "%APP_DIR%\venv" >>"%LOG%" 2>&1
if not exist "%VENV_PY%" (
    set "FAILMSG=Could not create the Python environment. Your Python install may be missing the venv module - reinstall Python from https://www.python.org/downloads/ and re-run this installer."
    goto :eof
)
%PS% "%TLS% Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%APP_DIR%\get-pip.py'" >>"%LOG%" 2>&1
if exist "%APP_DIR%\get-pip.py" "%VENV_PY%" "%APP_DIR%\get-pip.py" >>"%LOG%" 2>&1
del /q "%APP_DIR%\get-pip.py" >nul 2>nul
goto :eof


rem --- Install the project's requirements ------------------------------------
:install_deps
call :say "Installing required packages (this can take a couple of minutes)..."
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel >>"%LOG%" 2>&1
"%VENV_PY%" -m pip install --no-cache-dir -r "%APP_DIR%\requirements.txt"
if not errorlevel 1 goto :eof
call :say "First attempt failed - retrying with a longer timeout..."
"%VENV_PY%" -m pip install --no-cache-dir --retries 5 --timeout 60 -r "%APP_DIR%\requirements.txt"
if not errorlevel 1 goto :eof
set "FAILMSG=Could not install the required packages. If the messages above mention a connection or SSL error, your network is blocking pypi.org - ask IT to allow it, or try again on a different network."
goto :eof


rem --- Prove the install actually works before declaring success -------------
:verify
call :say "Checking the installation..."
"%VENV_PY%" -c "import fastapi, uvicorn, openpyxl" >>"%LOG%" 2>&1
if errorlevel 1 (
    set "FAILMSG=The packages installed but could not be loaded. See the log file for details."
    goto :eof
)
rem Importing server.py exercises the whole stack - fastapi, uvicorn, the
rem multipart handler, openpyxl and main.py - and fails loudly if the web
rem folder is missing. Both modules guard their entry point behind
rem __main__, so nothing actually runs here.
"%VENV_PY%" -c "import sys; sys.path.insert(0, r'%APP_DIR%'); import server" >>"%LOG%" 2>&1
if errorlevel 1 (
    set "FAILMSG=The scheduler code could not be loaded. See the log file for details."
    goto :eof
)
call :say "Installation verified."
goto :eof


rem --- Desktop shortcut ------------------------------------------------------
rem Ask WScript.Shell for its own Desktop path rather than hardcoding
rem %USERPROFILE%\Desktop: OneDrive "Known Folder Move" (common on managed
rem machines) redirects the visible Desktop elsewhere, and writing to the old
rem path silently produces a shortcut the user never sees.
:make_shortcut
set "SHORTCUT_DIR="
for /f "usebackq delims=" %%d in (`%PS% "$ws = New-Object -ComObject WScript.Shell; $d = $ws.SpecialFolders('Desktop'); if (-not $d) { $d = Join-Path $env:USERPROFILE 'Desktop' }; $sc = $ws.CreateShortcut((Join-Path $d 'WIT Class Scheduler.lnk')); $sc.TargetPath = '%RUN_BAT%'; $sc.WorkingDirectory = '%APP_DIR%'; $sc.IconLocation = 'shell32.dll,220'; $sc.Description = 'Start the WIT Class Scheduler'; $sc.Save(); Write-Output $d" 2^>nul`) do set "SHORTCUT_DIR=%%d"
if defined SHORTCUT_DIR call :log "Shortcut created in %SHORTCUT_DIR%"
if not defined SHORTCUT_DIR call :log "Shortcut creation failed"
goto :eof


rem --- Failure exit ----------------------------------------------------------
:fail
echo.
echo ============================================
echo   INSTALL FAILED
echo ============================================
echo.
echo %FAILMSG%
call :log "FAILED: %FAILMSG%"
echo.
echo A detailed log was saved to:
echo    %LOG%
echo Send that file to whoever shared this app with you if you need help.
echo.
call :finish
pause
exit /b 1

rem Signals the outer copy of this script that we reached a real exit and
rem already told the user what happened.
:finish
if defined DONE_FLAG break > "%DONE_FLAG%" 2>nul
goto :eof
