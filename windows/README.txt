WIT Class Scheduler — Windows Setup
====================================

First time setup:
  1. Download install.bat from this folder (or the whole repo).
  2. Double-click install.bat.
     - It downloads the project (needs internet access).
     - It installs Python packages into a private folder — nothing is
       installed system-wide.
     - Python 3.10+ must already be installed and on your PATH. If it
       isn't, the installer will tell you where to get it.
     - A "WIT Class Scheduler" shortcut is added to your Desktop.

Running the scheduler:
  - Double-click the "WIT Class Scheduler" shortcut on your Desktop
    (or run.bat in this folder).
  - It starts the local server and opens http://localhost:8000 in your
    browser.
  - To stop it, close the minimized "WIT Class Scheduler - Server" window.

Updating to a newer version:
  - Re-run install.bat; it pulls the latest code and re-installs packages.
