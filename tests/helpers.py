"""Shared plumbing for the scheduler test suite.

`main.py` resolves every input path relative to its own file, so a test cannot
point it at a different data set in place. Each end-to-end test therefore builds
a throwaway copy of the app in a temp directory, writes the CSVs it wants, and
runs `python3 main.py` there as a subprocess. That also means the tests can
never touch the real `data/` folder or overwrite the schedule the user is
looking at — a hard requirement, since these run before a push.
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

# Config files a test rarely cares about; copied from the repo unless overridden.
SHARED_INPUTS = {
    "timings.csv": "timings.csv",
    "rooms.csv": "rooms.csv",
    "room_preferences.csv": "room_preferences.csv",
    "settings.csv": "settings.csv",
    "meeting_patterns.csv": "meeting_patterns.csv",
}

COURSE_HEADER = ("Course number,Course Name,lecture days per week,lecture hours,"
                 "lab hours,number of sections,Preferred Room")


def course_list(rows: List[str]) -> str:
    return "\n".join([COURSE_HEADER, *rows]) + "\n"


def preferences(rows: List[str]) -> str:
    return "\n".join(["Course Number,Course Name,Faculty", *rows]) + "\n"


def faculty_load(rows: List[str]) -> str:
    return "\n".join(["Faculty,CS Course Load,Time Preference", *rows]) + "\n"


def groups(rows: Optional[List[str]] = None) -> str:
    return "\n".join(["group,course_number", *(rows or [])]) + "\n"


@dataclass
class Run:
    """The result of one scheduler run, parsed into something assertable."""
    returncode: int
    stdout: str
    workdir: Path
    constraints: Dict[str, bool] = field(default_factory=dict)
    unplaced: List[str] = field(default_factory=list)
    rows: List[Dict[str, str]] = field(default_factory=list)   # schedule_simple.csv
    banner: List[Dict[str, str]] = field(default_factory=list)  # schedule.csv
    events: list = field(default_factory=list)                 # schedule.json

    # ── convenience accessors ────────────────────────────────────────────────
    def failed_constraints(self) -> List[str]:
        return [k for k, ok in self.constraints.items() if not ok]

    def lectures(self, course: str) -> List[Dict[str, str]]:
        return [r for r in self.rows
                if r["Course Designation/Number"] == course and r["Type"] == "LEC"]

    def faculty_of(self, course: str) -> List[str]:
        return [r["Faculty"] for r in self.rows
                if r["Course Designation/Number"] == course]

    def assigned_faculty(self) -> set:
        return {r["Faculty"] for r in self.rows} - {"TBA"}

    def days_of(self, course: str) -> List[str]:
        return [r["Days"] for r in self.lectures(course)]


_DAY_RE = re.compile(r"Th|[MTWF]")


def duration_min(times: str) -> int:
    """'10-11:10' -> 70. Times are 12-hour with no am/pm; classes run 08:00-20:35,
    so an hour below 8 is afternoon."""
    def to_min(part: str) -> int:
        h, _, m = part.partition(":")
        hour = int(h)
        if hour < 8:
            hour += 12
        return hour * 60 + int(m or 0)

    start, _, end = times.partition("-")
    return to_min(end) - to_min(start)


def split_days(days: str) -> List[str]:
    """'MTTh' -> ['M','T','Th'] — 'Th' must be matched before 'T'."""
    return _DAY_RE.findall(days or "")


def run_scheduler(*, courses: str, prefs: str, loads: str,
                  overrides: Optional[Dict[str, str]] = None,
                  timeout: int = 120) -> Run:
    """Build a disposable app + data directory, run the scheduler, parse output."""
    work = Path(tempfile.mkdtemp(prefix="sched-test-"))
    shutil.copy2(REPO / "main.py", work / "main.py")
    (work / "data").mkdir()

    for name, src in SHARED_INPUTS.items():
        src_path = DATA / src
        if src_path.exists():
            shutil.copy2(src_path, work / "data" / name)

    (work / "data" / "course-list-Spring 27(Sheet1) (1).csv").write_text(courses, encoding="utf-8")
    (work / "data" / "prof_preferences.csv").write_text(prefs, encoding="utf-8")
    (work / "data" / "faculty_load.csv").write_text(loads, encoding="utf-8")
    if not (work / "data" / "non_overlap_groups.csv").exists():
        (work / "data" / "non_overlap_groups.csv").write_text(groups(), encoding="utf-8")

    for name, content in (overrides or {}).items():
        (work / "data" / name).write_text(content, encoding="utf-8")

    proc = subprocess.run([sys.executable, "main.py"], cwd=work, timeout=timeout,
                          capture_output=True, text=True)
    return _parse(proc, work)


def _parse(proc, work: Path) -> Run:
    out = proc.stdout + proc.stderr
    run = Run(returncode=proc.returncode, stdout=out, workdir=work)

    for line in out.splitlines():
        m = re.match(r"\s*[✓✗]\s+(PASS|FAIL)\s+(C\d+)", line)
        if m:
            run.constraints[m.group(2)] = (m.group(1) == "PASS")
        m = re.match(r"\s*\[CRITICAL\]\s+(\S+)", line)
        if m:
            run.unplaced.append(m.group(1).rstrip(":"))

    def read_csv(name: str) -> List[Dict[str, str]]:
        p = work / name
        if not p.exists():
            return []
        with open(p, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    run.rows = read_csv("schedule_simple.csv")
    run.banner = read_csv("schedule.csv")

    jpath = work / "schedule.json"
    if jpath.exists():
        import json
        run.events = json.loads(jpath.read_text())
    return run


class SchedulerTestCase(unittest.TestCase):
    """Adds the assertions every end-to-end test wants."""

    _workdirs: List[Path] = []

    @classmethod
    def tearDownClass(cls):
        for w in cls._workdirs:
            shutil.rmtree(w, ignore_errors=True)
        cls._workdirs = []

    def run_scheduler(self, **kw) -> Run:
        run = run_scheduler(**kw)
        type(self)._workdirs.append(run.workdir)
        return run

    def assertRanCleanly(self, run: Run):
        """A non-zero exit or a traceback must fail loudly.

        This exists because a real regression was missed by grepping output for
        '✗ FAIL': a run that dies with a NameError produces zero FAIL lines and
        reads as a perfect pass.
        """
        self.assertNotIn("Traceback", run.stdout,
                         msg=f"scheduler raised:\n{run.stdout[-2000:]}")
        self.assertEqual(run.returncode, 0,
                         msg=f"exit {run.returncode}:\n{run.stdout[-2000:]}")
        self.assertTrue(run.constraints,
                        msg=f"no constraint report was produced:\n{run.stdout[-2000:]}")

    def assertAllConstraintsPass(self, run: Run, allow: tuple = ()):
        self.assertRanCleanly(run)
        bad = [c for c in run.failed_constraints() if c not in allow]
        self.assertEqual(bad, [], msg=f"failing constraints {bad}:\n{run.stdout[-3000:]}")
