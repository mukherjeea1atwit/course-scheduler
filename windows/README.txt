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
     course-list-*.csv, prof_preferences.csv, faculty_load.csv,
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
  - CP-SAT is not seeded on purpose: running it again gives you a different
    valid timetable, so you can generate a few and pick one.
  - CP-SAT needs one extra package (ortools). install.bat installs it, but if
    that fails - it is large, and some networks block it - the rest of the app
    is unaffected and the Run page will say CP-SAT is unavailable. To add it
    later, run this from the course-scheduler folder:
        venv\Scripts\python.exe -m pip install -r requirements-cpsat.txt

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
