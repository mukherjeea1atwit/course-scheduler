WIT Class Scheduler — Windows Setup
====================================

See the full README.md at the root of the project for detailed,
step-by-step install and usage instructions. Quick version:

First time setup:
  1. Double-click install.bat.
     - If Windows shows a SmartScreen warning, click "More info" then
       "Run anyway" — this is normal for a downloaded script.
     - It installs Python automatically if needed (no admin rights
       required), downloads the project, and installs its packages into
       a private folder — nothing is installed system-wide.
     - A "WIT Class Scheduler" shortcut is added to your Desktop.
  2. If it fails partway through, it prints an [ERROR] line explaining
     what to do; you can safely re-run install.bat as many times as needed.

Running the scheduler:
  - Double-click the "WIT Class Scheduler" shortcut on your Desktop
    (or run.bat in this folder).
  - It starts the local server and opens http://localhost:8000 in your
    browser.
  - To stop it, close the minimized "WIT Class Scheduler - Server" window.

Updating to a newer version:
  - Re-run install.bat; it pulls the latest code and re-installs packages.
