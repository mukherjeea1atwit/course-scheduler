"""WIT Class Scheduler — Web API"""
import asyncio
import contextlib
import csv
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
# Pristine copies of the CSVs the app ships with. data/ is what the user edits
# and can therefore be emptied, corrupted, or moved; data-defaults/ is never
# written to, so it is always available to reseed a missing file from.
DEFAULTS_DIR = BASE_DIR / "data-defaults"
sys.path.insert(0, str(BASE_DIR))

from main import _run  # noqa: E402

app = FastAPI(title="WIT Scheduler API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── File slug → filename map ──────────────────────────────────────────────────
FILE_MAP: Dict[str, str] = {
    "courses":            "course-list-Spring 27(Sheet1) (1).csv",
    "preferences":        "prof_preferences.csv",
    "faculty_load":       "faculty_load.csv",
    "timings":            "timings.csv",
    "rooms":              "rooms.csv",
    "room_preferences":   "room_preferences.csv",
    "non_overlap_groups": "non_overlap_groups.csv",
    "settings":           "settings.csv",
    "meeting_patterns":   "meeting_patterns.csv",
}

# Human-readable names, used in error messages so the dean reads "Settings"
# rather than "settings" or a filename he has never seen. Kept in sync with the
# tab labels in web/inputs.html.
FILE_LABELS: Dict[str, str] = {
    "courses":            "Course List",
    "preferences":        "Faculty Preferences",
    "faculty_load":       "Faculty Load",
    "timings":            "Time Slots",
    "rooms":              "Rooms",
    "room_preferences":   "Room Preferences",
    "non_overlap_groups": "Non-Overlap Groups",
    "settings":           "Settings",
    "meeting_patterns":   "Meeting Lengths",
}

# ── Required columns per file ─────────────────────────────────────────────────
# The rule, deliberately lenient in one direction only:
#
#   * Every column listed here MUST be present. These are exactly the columns
#     main.py indexes without a fallback (r["Course number"], r["slot_label"],
#     ...) plus the handful it reads with .get() but cannot schedule without
#     (Faculty, CS Course Load, setting/value). A file missing one of them
#     crashes or silently mis-schedules, so it is never accepted.
#   * EXTRA columns are allowed and preserved. Users legitimately grow these
#     files — faculty_load.csv gained "Time Preference", courses gained
#     "Preferred Room" — and an exact-set match would reject their own data.
#   * Comparison is case-insensitive and ignores surrounding whitespace, because
#     Excel round-trips routinely change "Course Name" to "course name " and a
#     spreadsheet re-export is not a reason to refuse a save.
#
# This is what stops the Settings tab from being overwritten with the course
# list: the course list has none of settings' columns, so the write is rejected
# before it reaches the disk.
# Spelled the way the shipped files spell them, so error messages quote a name
# the user can find in their spreadsheet; matching folds case/whitespace.
REQUIRED_HEADERS: Dict[str, List[str]] = {
    "courses": ["Course number", "Course Name", "lecture days per week",
                "lecture hours", "lab hours", "number of sections"],
    "preferences": ["Course Number", "Faculty"],
    "faculty_load": ["Faculty", "CS Course Load"],
    "timings": ["start_time", "stop_time", "duration_min", "slot_label",
                "evening", "Days Allowed"],
    "rooms": ["Room", "Type", "Capacity"],
    "room_preferences": ["Course", "Type", "PreferenceRank", "Location"],
    "non_overlap_groups": ["group", "course_number"],
    "settings": ["setting", "value"],
    "meeting_patterns": ["subject", "lecture_days_per_week", "meeting_minutes"],
}


def _norm_header(h: Any) -> str:
    """Fold a column name to the form REQUIRED_HEADERS is written in."""
    return str(h or "").strip().lower()


def _validate_headers(slug: str, headers: List[str]) -> None:
    """Reject a table whose columns are not the ones this file needs.

    Raises HTTPException(422) naming both what arrived and what was expected.
    The message ends with "Did you pick the wrong tab?" because that is the
    cause in practice — a failed load on one tab leaving another tab's rows in
    the editor, then Save sending them to this file's endpoint. Getting this
    wrong destroys real data, so the message has to point at the real mistake.
    """
    required = REQUIRED_HEADERS.get(slug)
    if not required:
        return
    present = {_norm_header(h) for h in headers if _norm_header(h)}
    missing = [c for c in required if _norm_header(c) not in present]
    if not missing:
        return
    label = FILE_LABELS.get(slug, slug)
    got = ", ".join(str(h) for h in headers[:8]) or "(no columns)"
    if len(headers) > 8:
        got += ", …"
    raise HTTPException(
        status_code=422,
        detail=(
            f"This file's columns ({got}) don't match {label}, which expects "
            f"{', '.join(required)}. "
            f"Missing: {', '.join(missing)}. Did you pick the wrong tab?"
        ),
    )


def _ordered_keys(rows: List[Dict[str, Any]]) -> List[str]:
    """Union of every row's keys, in first-seen order.

    Taking the columns from rows[0] alone silently truncated the file whenever
    the first row happened to be missing a key — csv.DictWriter is created with
    extrasaction="ignore", so every other row lost that column too and the
    read-back check (row COUNTS only) still passed. Unioning across all rows
    means a ragged payload widens the header instead of narrowing the file.
    """
    keys: List[str] = []
    seen = set()
    for row in rows:
        for k in row:
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
    return keys


def _repair_wrong_table(slug: str, name: str, target: Path) -> None:
    """Restore a data file that exists but holds the WRONG table.

    Seeding only fills gaps, so it cannot help a file that is present but
    contains another file's data. That is exactly what happened to one install:
    settings.csv was overwritten with the course list, so the Settings tab
    showed a course list and every tunable silently fell back to its default.
    The header check that now guards writes cannot undo damage already on disk.

    The user's file is never deleted — it is backed up first, so if this
    misfires the original is one rename away. Repair only runs when the headers
    match NONE of what this file needs, which a merely customised file (extra
    columns, renamed optional ones) will never trigger.
    """
    required = REQUIRED_HEADERS.get(slug)
    source = DEFAULTS_DIR / name
    if not required or not source.exists():
        return
    try:
        with open(target, newline="", encoding="utf-8-sig") as f:
            headers = [h for h in (csv.DictReader(f).fieldnames or []) if h]
    except OSError as e:
        print(f"[WARN] Could not read {name} to check its columns: {e}")
        return
    if not headers:
        return
    present = {_norm_header(h) for h in headers}
    if any(_norm_header(r) in present for r in required):
        return          # recognisably the right file, however customised
    try:
        backup = _backup_existing(target)
        shutil.copy2(source, target)
    except OSError as e:
        print(f"[WARN] Could not restore {name}: {e}")
        return
    print(f"[FIXED] {name} contained a different file's data "
          f"(columns: {', '.join(headers[:4])}...). Restored the shipped version; "
          f"your file was kept as {backup.name if backup else 'a backup'}.")


def _seed_missing_data_files() -> None:
    """Copy any missing data/*.csv in from data-defaults/ at startup.

    A missing file used to surface as a bare HTTP 500 from FileNotFoundError,
    which tells the user nothing and makes the whole editor look broken. Files
    already present are never touched — this only ever fills gaps, so a user's
    edited data can't be reverted by restarting the server.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[WARN] Could not create the data folder {DATA_DIR}: {e}")
        return
    for slug, name in FILE_MAP.items():
        target = DATA_DIR / name
        if target.exists():
            _repair_wrong_table(slug, name, target)
            continue
        source = DEFAULTS_DIR / name
        if not source.exists():
            print(f"[WARN] {name} is missing from data/ and there is no default "
                  f"to restore it from ({source}).")
            continue
        try:
            shutil.copy2(source, target)
            print(f"[INFO] Restored missing {name} from the shipped defaults.")
        except OSError as e:
            print(f"[WARN] Could not restore {name} from {source}: {e}")


def _require_file(slug: str) -> Path:
    """Resolve a slug to an existing path, or raise an actionable 404.

    _read_csv/_read_headers open the path directly, so without this a deleted
    or renamed file became an unhandled FileNotFoundError and a bare 500
    "Internal Server Error" — no filename, no hint, nothing the user can act on.
    """
    path = _csv_path(slug)
    if path.exists():
        return path
    label = FILE_LABELS.get(slug, slug)
    raise HTTPException(
        status_code=404,
        detail=(
            f"{label} ({path.name}) is missing from your data folder — "
            f"it should be at {path}. Restore it from a backup (look for "
            f"{path.stem}.old1{path.suffix} in that folder), upload a "
            f"replacement on this tab, or restart the server to have the "
            f"shipped default copied back in."
        ),
    )


# ── Scheduler state ───────────────────────────────────────────────────────────
_busy = False
_q: "queue.Queue[str | None]" = queue.Queue()


class _QueueWriter(io.TextIOBase):
    def write(self, s: str) -> int:
        if s and s.strip():
            _q.put(s)
        return len(s)

    def flush(self) -> None:
        pass


def _worker() -> None:
    global _busy
    writer = _QueueWriter()
    with contextlib.redirect_stdout(writer):
        try:
            _run()
        except Exception as exc:
            _q.put(f"[ERROR] Scheduler crashed: {exc}\n")
        finally:
            _busy = False
            _q.put(None)


# ── Excel / CSV parsing ───────────────────────────────────────────────────────

def _parse_excel(content: bytes) -> tuple[List[str], List[Dict[str, str]]]:
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        raise ValueError("Excel file is empty")
    headers = [str(c).strip() for c in all_rows[0] if c is not None and str(c).strip()]
    rows = []
    for raw in all_rows[1:]:
        if all(c is None for c in raw):
            continue
        row = {}
        for i, h in enumerate(headers):
            val = raw[i] if i < len(raw) else None
            row[h] = str(val).strip() if val is not None else ""
        rows.append(row)
    return headers, rows


def _decode_csv_bytes(content: bytes) -> str:
    """Best-effort decode for CSVs exported by Excel/Numbers/Sheets on any OS.

    Plain UTF-8 fails on the "smart" quotes, en-dashes, and non-breaking
    spaces that Excel commonly writes as Windows-1252 bytes. Fall back
    through the encodings people actually export with; latin-1 maps every
    byte 0-255 so it never raises and guarantees a result.
    """
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")


def _parse_csv_bytes(content: bytes) -> tuple[List[str], List[Dict[str, str]]]:
    text = _decode_csv_bytes(content)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    clean = [{k: v for k, v in r.items() if k} for r in rows]
    # Prefer the file's own header line, and fall back to the union of the row
    # keys (not rows[0] alone). csv.DictReader puts unmatched trailing fields
    # under the None key, so a ragged file could otherwise hand back a header
    # list narrower than the data and _write_csv would drop the difference.
    headers = [k for k in (reader.fieldnames or []) if k] or _ordered_keys(clean)
    return headers, clean


def _backup_existing(path: Path) -> Path | None:
    """Preserve the file about to be overwritten as '<name>.oldN<ext>'.

    Never deletes or overwrites a prior backup — each upload bumps N so the
    full history (including the original file the app shipped with) stays
    on disk in the data/ folder.

    If the copy cannot be made, the save is ABORTED with a readable 500 rather
    than proceeding. Overwriting the user's only copy of a file we just failed
    to back up is exactly the outcome this whole function exists to prevent,
    and a folder we cannot copy into is a folder the write would fail in anyway.
    """
    if not path.exists():
        return None
    n = 1
    while True:
        backup_path = path.with_name(f"{path.stem}.old{n}{path.suffix}")
        if not backup_path.exists():
            break
        n += 1
    try:
        shutil.copy2(path, backup_path)
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=(f"Could not save {path.name}: a backup copy could not be written "
                    f"to {path.parent}. Check the folder exists and is writable — "
                    f"nothing was changed. ({e})"),
        )
    return backup_path


# ── CSV disk helpers ──────────────────────────────────────────────────────────

def _csv_path(slug: str) -> Path:
    if slug not in FILE_MAP:
        raise HTTPException(status_code=404, detail=f"Unknown file key: {slug}")
    return DATA_DIR / FILE_MAP[slug]


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [{k: v for k, v in r.items() if k} for r in rows]


def _read_headers(path: Path) -> List[str]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return [k for k in (reader.fieldnames or []) if k]


def _write_csv(path: Path, rows: List[Dict[str, Any]], headers: List[str]) -> None:
    """Write rows to `path` atomically.

    Raises HTTPException(500) with a human-readable reason on failure. Silent
    failure here is the worst outcome: the user edits a table, sees no error,
    and the scheduler keeps reading the old file. The common causes on Windows
    are the CSV being open in Excel (PermissionError) and OneDrive holding a
    lock on the folder, so those get their own message.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=(f"Could not save {path.name}: no temporary file could be created "
                    f"in {DATA_DIR}. Check the folder exists and is writable. ({e})"),
        )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        # mkstemp creates 0600 and os.replace keeps the temp file's mode, so
        # without this every server-written CSV ends up -rw------- while the
        # files git checked out are -rw-r--r--. That difference bites on shared
        # machines where the scheduler runs as another account.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except PermissionError as e:
        _discard(tmp)
        raise HTTPException(
            status_code=500,
            detail=(f"Could not save {path.name} — the file is locked. "
                    f"It is usually open in Excel; close it and try again. ({e})"),
        )
    except OSError as e:
        _discard(tmp)
        raise HTTPException(
            status_code=500,
            detail=(f"Could not save {path.name}: {e}. If this folder is synced "
                    f"by OneDrive, pause syncing and try again."),
        )
    except Exception as e:
        _discard(tmp)
        raise HTTPException(status_code=500, detail=f"Could not save {path.name}: {e}")


def _discard(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/api/data/{slug}/headers")
def get_headers(slug: str):
    return _read_headers(_require_file(slug))


@app.get("/api/data/{slug}")
def get_data(slug: str):
    return _read_csv(_require_file(slug))


@app.put("/api/data/{slug}")
def put_data(slug: str, rows: List[Dict[str, Any]]):
    path = _csv_path(slug)
    if not rows:
        raise HTTPException(status_code=422, detail="Cannot save an empty table")
    # Validate BEFORE touching the disk. This is the boundary that stops one
    # tab's rows being written over another tab's file; nothing downstream can
    # tell the difference once the bytes are written.
    headers = _ordered_keys(rows)
    _validate_headers(slug, headers)
    # Back up before every destructive write, exactly as the upload path does.
    # The customer lost settings.csv because this branch — the one the Save
    # button uses — was the only writer that overwrote without a copy.
    backup_path = _backup_existing(path)
    _write_csv(path, rows, headers)
    # Read back what actually landed on disk. A write that reports success but
    # produces a different row count means something else is touching the file.
    try:
        written = len(_read_csv(path))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Saved {path.name} but could not read it back to verify: {e}",
        )
    if written != len(rows):
        raise HTTPException(
            status_code=500,
            detail=(f"Saved {path.name} but it now holds {written} rows instead of "
                    f"{len(rows)}. Another program may be writing to this file."),
        )
    return {
        "ok": True,
        "rows": written,
        "path": str(path),
        "backup": backup_path.name if backup_path else None,
    }


@app.post("/api/data/{slug}/upload")
async def upload_data(slug: str, file: UploadFile = File(...)):
    path = _csv_path(slug)
    content = await file.read()
    fname = (file.filename or "").lower()

    if fname.endswith((".xlsx", ".xls")):
        try:
            headers, rows = _parse_excel(content)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read Excel file: {e}")
    else:
        try:
            headers, rows = _parse_csv_bytes(content)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read CSV: {e}")

    if not rows:
        raise HTTPException(status_code=422, detail="File has no data rows")

    # Same gate as put_data: picking the course list on the Settings tab is an
    # easy mistake with the file picker, and it is unrecoverable once written.
    _validate_headers(slug, headers)

    backup_path = _backup_existing(path)
    _write_csv(path, rows, headers)
    return {
        "ok": True,
        "rows": len(rows),
        "backup": backup_path.name if backup_path else None,
    }


# ── Scheduler endpoints ───────────────────────────────────────────────────────

@app.post("/api/run")
def start_run():
    global _busy, _q
    if _busy:
        raise HTTPException(status_code=409, detail="Scheduler already running")
    _busy = True
    _q = queue.Queue()
    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started"}


@app.get("/api/run/status")
def run_status():
    return {"running": _busy}


@app.get("/api/run/stream")
async def run_stream():
    async def generator():
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, _q.get)
            if line is None:
                yield "data: __DONE__\n\n"
                break
            text = line.strip().replace("\n", " ")
            if "[WARN]" in text:
                yield f"event: warn\ndata: {text}\n\n"
            elif "[CRITICAL]" in text or "[ERROR]" in text:
                yield f"event: critical\ndata: {text}\n\n"
            elif "✓ PASS" in text or ("PASS" in text and "✗" not in text):
                yield f"event: pass\ndata: {text}\n\n"
            elif "✗ FAIL" in text or ("FAIL" in text and "✓" not in text):
                yield f"event: fail\ndata: {text}\n\n"
            else:
                yield f"data: {text}\n\n"

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Schedule output endpoints ─────────────────────────────────────────────────

@app.get("/api/schedule")
def get_schedule():
    path = BASE_DIR / "schedule.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No schedule yet — run the scheduler first")
    with open(path) as f:
        return json.load(f)


@app.get("/api/schedule/csv")
def get_schedule_csv(format: str = "simple"):
    """Download the schedule as CSV.

    format=simple (default) → Course/Type/Days/Times/Faculty, the compact layout
    format=banner           → the wide Banner-style import sheet
    Served as an attachment so the browser opens its save dialog rather than
    rendering the CSV in the tab.
    """
    if format not in ("simple", "banner"):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown format '{format}'. Use 'simple' or 'banner'.",
        )
    filename = "schedule_simple.csv" if format == "simple" else "schedule.csv"
    path = BASE_DIR / filename
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="No schedule has been generated yet. Run the scheduler first.",
        )
    return FileResponse(
        str(path),
        media_type="text/csv",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/schedule/csv/legacy")
def get_schedule_csv_legacy():
    path = BASE_DIR / "schedule.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No schedule CSV yet")
    return FileResponse(str(path), media_type="text/csv", filename="schedule.csv")


# ── Startup ───────────────────────────────────────────────────────────────────
# Restore any missing data file before the first request can hit it. Done at
# import rather than via an on_event("startup") hook because that hook is
# deprecated in current FastAPI, and because a file restored here is already in
# place by the time the "Waiting for application startup" line is printed.
_seed_missing_data_files()


# ── Static file serving ───────────────────────────────────────────────────────
app.mount("/web", StaticFiles(directory=str(BASE_DIR / "web")), name="web")


@app.get("/")
def index():
    return FileResponse(str(BASE_DIR / "web" / "inputs.html"))


@app.get("/schedule")
def schedule():
    return FileResponse(str(BASE_DIR / "index.html"))


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Plain ASCII: box-drawing characters raise UnicodeEncodeError when stdout
    # is redirected on a Windows machine with a legacy (cp1252/cp437) locale,
    # which kills the server before it starts listening.
    print("==================================================")
    print("  WIT Class Scheduler  ->  http://localhost:8000")
    print("==================================================")
    uvicorn.run(app, host="0.0.0.0", port=8000)
