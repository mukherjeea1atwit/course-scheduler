# WIT Class Scheduler

A local web app that builds a conflict-free class schedule (faculty, rooms,
and times) for COMP/DATA courses from a set of CSV/Excel input files. It
runs entirely on your own computer — nothing is uploaded anywhere.

This guide covers installing it on a Windows computer, either with the
one-click installer or by hand, and how to use it once it's installed.

---

## Option A: Windows installer (recommended)

This is the simplest option and works on a normal work/office Windows
computer — **no Administrator rights and no IT approval needed.** It
installs everything into your own user folder only.

1. Get the `install.bat` file (ask whoever shared this project with you for
   it, or download it from the `windows` folder of the project).
2. Double-click `install.bat`.
3. If Windows shows a blue **"Windows protected your PC"** SmartScreen
   popup, click **More info**, then **Run anyway**. This is expected for any
   script downloaded from the internet — it does not mean anything is wrong.
4. Follow the on-screen messages. The installer will, as needed:
   - Install Python for you automatically if it isn't already on your
     computer (a normal, no-admin, per-user install — you don't need to do
     anything).
   - Download the project code.
   - Install the project's required packages into a private folder just for
     this app (it does not touch anything else on your computer).
   - Add a **"WIT Class Scheduler"** shortcut to your Desktop.
5. When you see **"Install complete!"**, press any key to close the window.

That's it — see [Using the scheduler](#using-the-scheduler) below.

**If it fails partway through:** the installer prints an `[ERROR]` line
explaining what went wrong and what to do next (e.g. no internet, or a
network/firewall blocking downloads — in that case ask your IT department to
allow access to `github.com`, `python.org`, and `pypi.org`, or try on a
different network such as personal hotspot). You can safely re-run
`install.bat` as many times as you need — it picks up where it left off and
won't duplicate anything.

### Updating later

Re-run `install.bat` any time. It pulls the latest project code and
refreshes the installed packages, without needing to redo anything else.

---

## Option B: Manual install (any Windows computer)

Use this if the installer doesn't work for you, or if you'd rather see
every step yourself.

1. **Install Python 3.10 or newer**, if you don't already have it:
   - Go to https://www.python.org/downloads/ and click the yellow
     **"Download Python"** button.
   - Run the installer. On the **first screen**, check the box at the
     bottom that says **"Add python.exe to PATH"** (this step is important —
     if you miss it, the next steps won't work) — then click **Install Now**.
   - This installs Python for your user account only; it does not need
     Administrator rights.

2. **Get the project code**, using either method:
   - **With Git:** open Command Prompt and run:
     ```
     git clone https://github.com/mukherjeea1atwit/course-scheduler.git
     cd course-scheduler
     ```
   - **Without Git:** download the project as a ZIP from GitHub
     (Code → Download ZIP), right-click the downloaded file and choose
     **Extract All**, then open the extracted folder.

3. **Open Command Prompt in that folder.** The easiest way: in File
   Explorer, click the address bar at the top of the folder window, type
   `cmd`, and press Enter.

4. **Install the required packages:**
   ```
   python -m pip install -r requirements.txt
   ```
   If `python` isn't recognized, try `py` instead of `python` in this and
   the next step.

5. **Start the app:**
   ```
   python server.py
   ```
   Leave this window open — it's running the local server. Then open your
   browser to **http://localhost:8000**.

6. To stop the app later, go back to that Command Prompt window and press
   `Ctrl+C`, or just close the window.

Next time, you only need to repeat steps 3–5 (open the folder in Command
Prompt, run `python server.py`, open the browser) — you don't need to
reinstall anything.

---

## Using the scheduler

Once installed (either option above), start the app:

- **Installer method:** double-click the **"WIT Class Scheduler"** Desktop
  shortcut. A small window titled "WIT Class Scheduler - Server" opens
  (minimized) and your browser opens automatically to the app after a few
  seconds.
- **Manual method:** open the project folder in Command Prompt and run
  `python server.py`, then open http://localhost:8000 in your browser.

### Typical workflow

1. **Review/edit input data.** The home page (`http://localhost:8000`) lets
   you view and edit each input file (course list, faculty preferences,
   faculty load targets, room list, room preferences, timings, and
   non-overlap course groups). You can edit rows directly in the browser, or
   upload a replacement CSV/Excel file for any of them.
2. **Run the scheduler.** Go to the **Run Scheduler** page and click the
   run button. Progress is streamed live, including warnings (`[WARN]`)
   and any hard failures (`[ERROR]`/`[CRITICAL]`) so you can see exactly
   what the scheduler did.
3. **View the schedule.** Once a run finishes, go to the schedule view to
   browse the result by Day, Professor, Room, or Section.
4. **Export results.** The generated `schedule.csv` and `schedule.json` are
   written to the project folder, and can be downloaded from the app.

### Stopping the app

- **Installer method:** close the minimized **"WIT Class Scheduler -
  Server"** window.
- **Manual method:** press `Ctrl+C` in the Command Prompt window, or close it.

Your data and any generated schedules stay on your computer between runs —
closing the app doesn't erase anything.

### Getting help

If something doesn't work as expected, check the `[ERROR]`/`[WARN]`
messages shown while the scheduler runs — they describe what went wrong
(e.g. a missing preference, an unassignable section) in plain language.
