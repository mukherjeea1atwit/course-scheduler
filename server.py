"""WIT Class Scheduler — Web API"""
import asyncio
import contextlib
import csv
import io
import json
import os
import queue
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
}

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


def _parse_csv_bytes(content: bytes) -> tuple[List[str], List[Dict[str, str]]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    headers = [k for k in (rows[0].keys() if rows else reader.fieldnames or []) if k]
    clean = [{k: v for k, v in r.items() if k} for r in rows]
    return headers, clean


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
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Data endpoints ────────────────────────────────────────────────────────────

@app.get("/api/data/{slug}/headers")
def get_headers(slug: str):
    return _read_headers(_csv_path(slug))


@app.get("/api/data/{slug}")
def get_data(slug: str):
    return _read_csv(_csv_path(slug))


@app.put("/api/data/{slug}")
def put_data(slug: str, rows: List[Dict[str, Any]]):
    path = _csv_path(slug)
    if not rows:
        raise HTTPException(status_code=422, detail="Cannot save an empty table")
    _write_csv(path, rows, list(rows[0].keys()))
    return {"ok": True, "rows": len(rows)}


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
    _write_csv(path, rows, headers)
    return {"ok": True, "rows": len(rows)}


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
def get_schedule_csv():
    path = BASE_DIR / "schedule.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No schedule CSV yet")
    return FileResponse(str(path), media_type="text/csv", filename="schedule.csv")


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
    print("╔══════════════════════════════════════════════╗")
    print("║  WIT Class Scheduler  →  http://localhost:8000  ║")
    print("╚══════════════════════════════════════════════╝")
    uvicorn.run(app, host="0.0.0.0", port=8000)
