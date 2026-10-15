WIT Class Scheduler - Windows Setup
===================================

See the full README.md at the root of the project for detailed,
step-by-step install and usage instructions. Quick version:

Installing (and re-installing / updating):
  1. Double-click install.bat.
     - If Windows shows a SmartScreen warning, click "More info" then
       "Run anyway" - this is normal for a downloaded script.
     - It installs Python automatically if needed (no admin rights
       required), downloads the project, and installs its packages into
       a private folder - nothing is installed system-wide.
     - A "WIT Class Scheduler" shortcut is added to your Desktop.
  2. If it fails, it prints an [ERROR] line explaining what to do, and
     writes a full log to:
        %USERPROFILE%\WIT-Class-Scheduler\install-log.txt
     Send that log to whoever shared this app with you if you get stuck.

  install.bat always does a CLEAN install: it stops any running copy,
  deletes the previous installation and its Python environment, then
  installs everything again from scratch. Run it as many times as you
  like - to update to a newer version, or to repair a broken install.

  Your existing input files (the data folder) are copied to
     %USERPROFILE%\WIT-Class-Scheduler\previous-data-<number>
  before the old copy is removed, and the installer prints that path at
  the end. The fresh install starts from the default input files, so if
  you had edited yours, copy them back from that folder.

  IMPORTANT - do not copy back all of them. Copy back only the files
  holding your own data:
     course-list.csv, prof_preferences.csv, faculty_load.csv,
     rooms.csv, room_preferences.csv, non_overlap_groups.csv

  Leave the NEW versions of these in place:
     timings.csv           new versions add class times the scheduler
                           needs; an old copy removes them
     meeting_patterns.csv  how long a class is for a given number of
                           days per week
     settings.csv          tunables, e.g. parallel classes

  If you had edited timings.csv yourself, re-apply your changes to the
  new file instead of replacing it.

  If sections come out marked UNPLACED right after an update, an old
  timings.csv is the first thing to check.

Two schedulers:
  - The Run page has two engines. The Greedy scheduler is the original one
    and finishes instantly. The CP-SAT scheduler solves the whole timetable
    at once, takes tens of seconds (occasionally a couple of minutes, with an
    8 minute maximum), and often leaves fewer sections unstaffed.
  - They write different files and never overwrite each other, so you can
    start CP-SAT and keep reading, viewing and exporting the greedy schedule
    while it works. The Schedule page has a dropdown to pick which one you
    are looking at.
  - By default CP-SAT gives you a DIFFERENT valid timetable every time you run
    it, so you can generate a few and pick the one you like. Untick "Vary the
    schedule each run" on the Run page to turn that off: the same inputs then
    always produce the same schedule, which is what you want when you are
    comparing two sets of inputs or reproducing a schedule you already sent
    someone. That mode is slower - several minutes rather than under one.
  - CP-SAT runs on a package called ortools, which install.bat installs for
    you. It is required, not optional: if it cannot be installed the installer
    stops and tells you why, rather than leaving you with half an app. It is a
    large download (about 100 MB) and some work networks block it, so if the
    install fails on this step, the log file names the two addresses to ask IT
    to allow (pypi.org and files.pythonhosted.org). Running the installer once
    on a phone hotspot also works.
  - If the Run page ever says CP-SAT is unavailable, the install is damaged.
    Re-run install.bat to repair it. To patch it in place instead, run this
    from the course-scheduler folder:
        venv\Scripts\python.exe -m pip install -r requirements-cpsat.txt
  - ortools is 64-bit only, and needs Python 3.9 or newer. install.bat checks
    for that and installs its own 64-bit Python 3.12 if this computer's Python
    is not suitable.

Running the scheduler:
  - Double-click the "WIT Class Scheduler" shortcut on your Desktop
    (or run.bat in this folder).
  - It starts the local server and opens http://localhost:8000 in your
    browser once the server is actually ready.
  - To stop it, close the minimized "WIT Class Scheduler - Server" window.

Notes for whoever maintains this:
  - install.bat and run.bat must stay plain ASCII with CRLF line endings.
    .gitattributes enforces this. cmd.exe is unreliable with LF-only batch
    files, and non-ASCII bytes get mangled under legacy console codepages.
  - install.bat re-runs itself from a copy in %TEMP%. Do not remove that:
    cmd.exe reads a batch file by byte offset while executing it, so
    re-downloading the project on top of the running install.bat used to
    make the window close instantly with no error message.
