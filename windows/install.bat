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

call :install_cpsat
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
rem PYMAX is the first Python 3.x minor version we know of that has no ortools
rem wheel. ortools is required now (the CP-SAT engine is half the product) and
rem it ships win_amd64 wheels only, for CPython 3.9 - 3.14 (3.14 arrived in
rem ortools 9.15). It publishes no source tarball at all, so pip cannot build
rem it either. A 32-bit or too-new Python therefore cannot run this app, and it
rem is much kinder to notice that here than to let pip fail later with "no
rem matching distribution found".
:find_python
set "PYMAX=15"
set "PY="
call :try_py "py -3"
call :try_py "python"
call :try_py "python3"
if defined PY goto :found_python

call :say "No suitable Python was found on this computer - installing one now..."
call :say "This is a per-user install; no Administrator rights are needed."
set "PYEXE=%TEMP%\python-installer-%STAMP%.exe"
call :say "Downloading Python from python.org (about 25 MB)..."
%PS% "%TLS% Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%PYEXE%'" >>"%LOG%" 2>&1

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
call :try_py "py -3.12"
call :try_py "py -3"
call :try_py "python"
call :try_py "python3"
if defined PY goto :found_python

rem Still nothing - so take a Python newer than this installer knows about, if
rem there is one. By the time such a version is widespread ortools normally has
rem a wheel for it, and if it does not, :install_cpsat says so in plain words
rem rather than us refusing a machine that would have worked. 32-bit builds are
rem still rejected: no ortools wheel exists for those, ever.
set "PYMAX=99"
call :try_py "py -3"
call :try_py "python"
call :try_py "python3"
if defined PY goto :found_python

set "FAILMSG=No usable Python could be installed automatically. This app needs the 64-bit Python 3.12 for Windows. Install it by hand from https://www.python.org/downloads/ - choose the Windows installer (64-bit) and tick the Add python.exe to PATH box during setup - then re-run this installer."
goto :eof

:found_python
for /f "tokens=2 delims= " %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
call :say "Using 64-bit Python %PYVER%  (%PY%)"
goto :eof

rem Probe one candidate interpreter. Rejects the Microsoft Store stub (which is
rem on PATH even with no real Python installed) because the stub exits non-zero,
rem anything older than 3.9 or newer than PYMAX, and any 32-bit build - see the
rem note above :find_python for why the last two matter.
:try_py
if defined PY goto :eof
%~1 -c "import struct, sys; raise SystemExit(0 if sys.version_info >= (3, 9) and sys.version_info < (3, %PYMAX%) and struct.calcsize('P') == 8 else 1)" >>"%LOG%" 2>&1
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

rem Half-finished work from a run that was closed or killed part way through.
rem These carry a %RANDOM% stamp, so they never collide with the run in
rem progress - and the ortools wheel alone is about 100 MB, which is not a
rem thing to leave piling up in the user's home folder.
for /d %%d in ("%INSTALL_DIR%\_zip-*") do rd /s /q "%%d" >nul 2>nul
del /q "%INSTALL_DIR%\repo-*.zip" >nul 2>nul
del /q "%INSTALL_DIR%\ortools-*.whl" >nul 2>nul
del /q "%INSTALL_DIR%\pytag-*.txt" >nul 2>nul

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
%PS% "%TLS% Invoke-WebRequest -UseBasicParsing -Uri '%REPO_ZIP%' -OutFile '%ZIPFILE%'" >>"%LOG%" 2>&1
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
%PS% "%TLS% Invoke-WebRequest -UseBasicParsing -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%APP_DIR%\get-pip.py'" >>"%LOG%" 2>&1
if exist "%APP_DIR%\get-pip.py" "%VENV_PY%" "%APP_DIR%\get-pip.py" >>"%LOG%" 2>&1
del /q "%APP_DIR%\get-pip.py" >nul 2>nul
goto :eof


rem --- Install the project's requirements ------------------------------------
rem Every pip line logs to %LOG% rather than the console: pip's output is long
rem and frightening, and when something does go wrong the whole transcript is
rem in the one file we ask the user to send on.
:install_deps
call :say "Installing required packages (this can take a couple of minutes)..."
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel >>"%LOG%" 2>&1
"%VENV_PY%" -m pip install --no-cache-dir -r "%APP_DIR%\requirements.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :eof
call :say "First attempt failed - retrying with a longer timeout..."
"%VENV_PY%" -m pip install --no-cache-dir --retries 5 --timeout 120 -r "%APP_DIR%\requirements.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :eof
call :say "Retrying against pypi.org directly..."
"%VENV_PY%" -m pip install --no-cache-dir --retries 5 --timeout 120 --index-url https://pypi.org/simple -r "%APP_DIR%\requirements.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :eof
rem Work networks that inspect HTTPS traffic present their own certificate.
rem Windows trusts it, but pip ships its own list of trusted authorities and
rem does not, so every download dies with an SSL error. This last attempt tells
rem pip to trust those two hosts.
call :say "Retrying without the strict certificate check..."
"%VENV_PY%" -m pip install --no-cache-dir --retries 5 --timeout 120 --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org -r "%APP_DIR%\requirements.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :eof
set "FAILMSG=Could not install the required packages. This is nearly always the network: ask IT to allow pypi.org and files.pythonhosted.org from this computer, or try again on a personal hotspot. The exact error is at the end of the log file below."
goto :eof


rem --- The CP-SAT engine's solver: REQUIRED -----------------------------------
rem ortools is not an optional extra any more. CP-SAT is one of the two engines
rem the product ships with, so if ortools will not install then the install has
rem failed and must say so instead of quietly handing over a half-working app.
rem
rem It still gets its own step rather than a line in requirements.txt, for two
rem reasons: it is a ~100 MB binary wheel and by far the most likely package to
rem be refused by a locked-down network, so it needs this escalating series of
rem attempts; and folding it into requirements.txt would mean one blocked
rem download also takes fastapi and the rest of the app down with it, leaving
rem nothing for the fallbacks below to run on top of.
rem
rem Every attempt passes --only-binary=:all: on purpose. ortools publishes no
rem source tarball on PyPI at all, so "fall back to building from source" is
rem not a thing that can succeed here; without the flag, a missing wheel is
rem reported as a confusing build error instead of a plain "no wheel for this
rem Python".
:install_cpsat
if not exist "%APP_DIR%\requirements-cpsat.txt" (
    set "FAILMSG=The project did not download correctly - requirements-cpsat.txt is missing. Check your internet connection and re-run this installer."
    goto :eof
)
call :say "Installing the CP-SAT solver (ortools - this one is large)..."
"%VENV_PY%" -m pip install --no-cache-dir --only-binary=:all: -r "%APP_DIR%\requirements-cpsat.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :cpsat_ok

call :say "First attempt failed - retrying with a longer timeout..."
"%VENV_PY%" -m pip install --no-cache-dir --only-binary=:all: --retries 5 --timeout 120 -r "%APP_DIR%\requirements-cpsat.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :cpsat_ok

rem A company-wide pip.ini pointing pip at an internal mirror that does not
rem carry ortools looks exactly like a network outage. Name pypi.org once.
call :say "Retrying against pypi.org directly..."
"%VENV_PY%" -m pip install --no-cache-dir --only-binary=:all: --retries 5 --timeout 120 --index-url https://pypi.org/simple -r "%APP_DIR%\requirements-cpsat.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :cpsat_ok

rem Same certificate story as in :install_deps above - see the note there.
call :say "Retrying without the strict certificate check..."
"%VENV_PY%" -m pip install --no-cache-dir --only-binary=:all: --retries 5 --timeout 120 --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org -r "%APP_DIR%\requirements-cpsat.txt" >>"%LOG%" 2>&1
if not errorlevel 1 goto :cpsat_ok

rem Last ditch: fetch the wheel with PowerShell and install it off the disk.
rem PowerShell uses the machine's own proxy settings and certificate store, so
rem it gets through where pip cannot, and content filters that time out or
rem refuse this one 100 MB download while letting the small packages past are
rem a real and common failure. Ask PyPI which wheel matches this interpreter
rem (cp312 and so on) rather than hardcoding a version that will rot.
rem
rem It must pick the same wheel pip would have: newest, not yanked, and inside
rem the 9.x pin from requirements-cpsat.txt. Hence the 'ortools-9.*' pattern
rem (the newest release on PyPI will one day be a 10.x that this app's code
rem does not run on) and the sort (PyPI orders the "releases" map as text, so
rem "9.9" sorts after "9.15" and picking the last match would quietly install
rem a two-year-old solver).
rem
rem -UseBasicParsing is required: without it Windows PowerShell 5.1 hands the
rem response to the Internet Explorer engine, which is disabled or absent on
rem current Windows and throws instead of saving the file.
rem
rem The interpreter tag goes through a file rather than a for /f backquote:
rem quoting a program path inside backquotes and then quoting again inside the
rem command is one of the ways cmd.exe silently produces nothing.
call :say "Downloading the solver directly..."
set "PYTAG="
set "TAGFILE=%INSTALL_DIR%\pytag-%STAMP%.txt"
"%VENV_PY%" -c "import sys; print('cp' + str(sys.version_info[0]) + str(sys.version_info[1]))" >"%TAGFILE%" 2>>"%LOG%"
if not exist "%TAGFILE%" goto :cpsat_failed
set /p PYTAG=<"%TAGFILE%"
del /q "%TAGFILE%" >nul 2>nul
if not defined PYTAG goto :cpsat_failed
call :log "Looking for an ortools wheel tagged %PYTAG%"
set "WHEELFILE=%INSTALL_DIR%\ortools-%PYTAG%-%STAMP%.whl"
%PS% "%TLS% $pat = 'ortools-9.*-%PYTAG%-*win_amd64.whl'; $j = Invoke-RestMethod -Uri 'https://pypi.org/pypi/ortools/json'; $all = @($j.urls) + @($j.releases.PSObject.Properties | ForEach-Object { $_.Value }); $f = $all | Where-Object { $_.filename -like $pat -and -not $_.yanked } | Sort-Object { [version]($_.filename.Split('-')[1]) } | Select-Object -Last 1; if (-not $f) { exit 1 }; Invoke-WebRequest -UseBasicParsing -Uri $f.url -OutFile '%WHEELFILE%'" >>"%LOG%" 2>&1
if not exist "%WHEELFILE%" goto :cpsat_failed
rem No --no-index here on purpose: ortools pulls in numpy, pandas and protobuf,
rem which are small enough that pip usually still gets them by itself. Keep the
rem index and certificate flags from the attempts above, though - we only got
rem this far because plain pip could not reach PyPI, and those small downloads
rem are about to go out over the same connection.
"%VENV_PY%" -m pip install --no-cache-dir --retries 5 --timeout 120 --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org "%WHEELFILE%" >>"%LOG%" 2>&1
set "PIPRC=%ERRORLEVEL%"
del /q "%WHEELFILE%" >nul 2>nul
if "%PIPRC%"=="0" goto :cpsat_ok
goto :cpsat_failed

:cpsat_ok
call :say "CP-SAT solver installed."
goto :eof

:cpsat_failed
set "FAILMSG=Could not install ortools, the solver the CP-SAT scheduler runs on. It is a required part of this app, so the install stopped here. This is nearly always a blocked network: ask IT to allow pypi.org and files.pythonhosted.org from this computer, then re-run this installer - or run the installer once on a personal hotspot. It can also mean this computer has a Python that ortools does not support; installing the 64-bit Python 3.12 from https://www.python.org/downloads/ and re-running fixes that. The exact error is at the end of the log file below."
goto :eof


rem --- Prove the install actually works before declaring success -------------
:verify
call :say "Checking the installation..."
"%VENV_PY%" -c "import fastapi, uvicorn, openpyxl" >>"%LOG%" 2>&1
if errorlevel 1 (
    set "FAILMSG=The packages installed but could not be loaded. See the log file for details."
    goto :eof
)
rem A real import of the CP-SAT model class, not just a "does the folder
rem exist" check: ortools is a native extension, and a wheel for the wrong
rem architecture or a half-finished download unpacks fine and only blows up
rem here, at import time.
"%VENV_PY%" -c "import ortools.sat.python.cp_model" >>"%LOG%" 2>&1
if errorlevel 1 (
    set "FAILMSG=The CP-SAT solver (ortools) installed but could not be loaded, so the CP-SAT scheduler would not run. Re-run this installer; if it fails the same way again, send the log file below to whoever shared this app with you."
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
