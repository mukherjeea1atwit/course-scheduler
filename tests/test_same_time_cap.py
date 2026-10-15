"""Configurable C14 cap: `max_same_time_sections` in data/settings.csv.

C14 used to hard-code "at most 2 sections of the same course at the same time"
(same course number, same sorted day-set, same start and end). It is now a
setting shared by both engines:

    max_same_time_sections,2,...

The value is either a whole number (`2`, `3`) or a percentage of the course's
"number of sections" (`25%`, ` 25 % `). Percentages round half UP (2.5 -> 3,
never Python's banker's round), and the cap is never below 1. Anything else
(`abc`, `-5`, `150%`) is reported with a [WARN] and the default 2 is used.

Contract exercised here:
  * `same_time_cap(course_sections: int) -> int` exists in BOTH main.py and
    main_cpsat.py and reads the module globals load_settings() sets.
  * Both ConstraintCheckers apply the per-course cap. The checker gets the
    course's section count from the sections dict it is handed (every section
    of a course, placed or not, is in that dict), so run_all's signature does
    not need to change. If the implementation prefers to be told the counts,
    `ConstraintChecker(course_sections={course: n})` is tried first.
  * The greedy placer, the CP-SAT model and tests/validate_schedule.py enforce
    the same cap.

Module globals are snapshotted and restored around every in-process test,
because load_settings() mutates them.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from collections import Counter
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import main_cpsat  # noqa: E402  (imports without ortools; the solver import is lazy)
from helpers import (  # noqa: E402
    REPO, SchedulerTestCase, course_list, faculty_load, preferences, split_days,
)

SETTING = "max_same_time_sections"
ENGINES = (("greedy", main), ("cpsat", main_cpsat))


# ── helpers ─────────────────────────────────────────────────────────────────

def _snapshot(mod) -> dict:
    return {k: v for k, v in vars(mod).items()
            if not k.startswith("__")
            and not isinstance(v, types.ModuleType)
            and not callable(v)}


def _restore(mod, snap: dict) -> None:
    for k, v in snap.items():
        setattr(mod, k, v)


def _load(mod, text: str) -> str:
    """Write `text` as a settings.csv, load it into `mod`, return captured stdout."""
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "settings.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        with contextlib.redirect_stdout(buf):
            mod.load_settings(p)
    return buf.getvalue()


def _settings_with(value) -> str:
    """The shipped settings.csv with `max_same_time_sections` set to `value`
    (or removed when value is None)."""
    lines = (REPO / "data" / "settings.csv").read_text(encoding="utf-8-sig").splitlines()
    keep = [ln for ln in lines if not ln.split(",", 1)[0].strip() == SETTING]
    if value is not None:
        v = f'"{value}"' if ("," in str(value) or str(value) != str(value).strip()) else value
        keep.append(f"{SETTING},{v},test override")
    return "\n".join(keep) + "\n"


class _GlobalsIsolated(unittest.TestCase):
    def setUp(self):
        self._snaps = {name: _snapshot(mod) for name, mod in ENGINES}

    def tearDown(self):
        for name, mod in ENGINES:
            _restore(mod, self._snaps[name])


# ── 1. same_time_cap: pure computation ──────────────────────────────────────

class SameTimeCapFunction(_GlobalsIsolated):
    """same_time_cap(n) in both engines, driven through load_settings()."""

    def _cap(self, mod, value, sections: int) -> int:
        _load(mod, f'setting,value\n{SETTING},"{value}"\n')
        self.assertTrue(hasattr(mod, "same_time_cap"),
                        msg=f"{mod.__name__}.same_time_cap is missing")
        return mod.same_time_cap(sections)

    def _check(self, cases):
        for name, mod in ENGINES:
            for value, n, expected in cases:
                with self.subTest(engine=name, value=value, sections=n):
                    self.assertEqual(self._cap(mod, value, n), expected)

    def test_number_mode_is_the_number(self):
        self._check([("2", 8, 2), ("2", 1, 2), ("3", 8, 3), ("3", 2, 3), ("5", 40, 5)])

    def test_percent_mode_exact(self):
        self._check([("25%", 8, 2), ("50%", 8, 4), ("25%", 4, 1)])

    def test_percent_mode_rounds_half_up_not_bankers(self):
        # Python's round(2.5) == 2 and round(0.5) == 0; the spec wants 3 and 1.
        self._check([("25%", 10, 3),   # 2.5 -> 3
                     ("25%", 6, 2),    # 1.5 -> 2
                     ("50%", 5, 3),    # 2.5 -> 3
                     ("50%", 1, 1),    # 0.5 -> 1
                     ("25%", 18, 5)])  # 4.5 -> 5

    def test_percent_mode_rounds_down_below_half(self):
        self._check([("33%", 3, 1),    # 0.99 -> 1
                     ("30%", 7, 2),    # 2.1 -> 2
                     ("40%", 6, 2)])   # 2.4 -> 2

    def test_cap_is_never_below_one(self):
        self._check([("10%", 3, 1),    # 0.3 -> 1
                     ("0%", 8, 1),
                     ("0", 8, 1),
                     ("1%", 1, 1)])

    def test_hundred_percent_is_every_section(self):
        self._check([("100%", 1, 1), ("100%", 3, 3), ("100%", 12, 12)])

    def test_default_is_two(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                _load(mod, "setting,value\n")
                self.assertEqual(mod.same_time_cap(8), 2)
                self.assertEqual(mod.same_time_cap(1), 2)


# ── 2. load_settings parsing ─────────────────────────────────────────────────

class LoadSettingsParsing(_GlobalsIsolated):

    def _cap_and_out(self, mod, raw_value_cell: str, sections: int = 8):
        out = _load(mod, f"setting,value\n{SETTING},{raw_value_cell}\n")
        return mod.same_time_cap(sections), out

    def test_setting_is_a_known_default_in_both_engines(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertIn(SETTING, mod.SETTING_DEFAULTS)
                self.assertEqual(str(mod.SETTING_DEFAULTS[SETTING]).strip(), "2")

    def test_valid_values_load_without_warning(self):
        for name, mod in ENGINES:
            for cell, n, expected in [("3", 8, 3), ("25%", 8, 2), ('" 25 % "', 8, 2),
                                      ('" 4 "', 8, 4), ("50 %", 10, 5)]:
                with self.subTest(engine=name, cell=cell):
                    cap, out = self._cap_and_out(mod, cell, n)
                    self.assertEqual(cap, expected)
                    self.assertNotIn(SETTING, out,
                                     msg=f"unexpected message about a valid value:\n{out}")

    def test_invalid_values_warn_and_fall_back_to_two(self):
        for name, mod in ENGINES:
            for cell in ["abc", "-5", "150%", "-10%", "%", "2x", "x%"]:
                with self.subTest(engine=name, cell=cell):
                    cap, out = self._cap_and_out(mod, cell, sections=20)
                    self.assertEqual(cap, 2)
                    warn_lines = [ln for ln in out.splitlines()
                                  if "[WARN]" in ln and SETTING in ln]
                    self.assertTrue(warn_lines,
                                    msg=f"no [WARN] naming {SETTING} for {cell!r}:\n{out}")

    def test_bad_value_does_not_inherit_previous_value(self):
        # Mirrors the existing load_settings rule: every load resets to defaults.
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self._cap_and_out(mod, "5")
                cap, _ = self._cap_and_out(mod, "banana")
                self.assertEqual(cap, 2)
                self._cap_and_out(mod, "50%")
                cap, _ = self._cap_and_out(mod, "banana", sections=20)
                self.assertEqual(cap, 2)

    def test_missing_row_means_default_two(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self._cap_and_out(mod, "7")
                out = _load(mod, "setting,value\nmax_concurrent_sections,10\n")
                self.assertEqual(mod.same_time_cap(20), 2)
                self.assertNotIn(SETTING, out)

    def test_missing_file_means_default_two(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self._cap_and_out(mod, "7")
                with contextlib.redirect_stdout(io.StringIO()):
                    mod.load_settings("/nonexistent/settings.csv")
                self.assertEqual(mod.same_time_cap(20), 2)

    def test_not_reported_as_unknown_setting(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                _, out = self._cap_and_out(mod, "2")
                self.assertNotIn("unknown setting", out)


# ── 3. shipped data ─────────────────────────────────────────────────────────

class ShippedSettingsRow(unittest.TestCase):
    def test_settings_csv_ships_the_row_with_value_two(self):
        import csv
        for folder in ("data", "data-defaults"):
            p = REPO / folder / "settings.csv"
            if folder != "data" and not p.exists():
                continue
            with self.subTest(file=str(p)):
                with open(p, newline="", encoding="utf-8-sig") as f:
                    rows = {(r.get("setting") or "").strip(): r for r in csv.DictReader(f)}
                self.assertIn(SETTING, rows, msg=f"{p} lacks {SETTING}")
                self.assertEqual(rows[SETTING]["value"].strip(), "2")
                self.assertTrue((rows[SETTING].get("description") or "").strip(),
                                msg="the row needs a description")


# ── 4a. checker-level behaviour ─────────────────────────────────────────────

def _sec(mod, course: str, n: int, start=(10, 0), end=(11, 20), days=("T", "Th"),
         unplaced=False):
    return mod.ScheduledSection(
        section_id=f"{course}-{n}", course_number=course, course_name=course,
        faculty=f"Prof{course}{n}", room="UNPLACED" if unplaced else f"R{n}",
        days=list(days), start_time=time(*start), end_time=time(*end),
        has_lab=False, days_per_week=len(days))


_SPREAD_HOURS = [8, 9, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def _schedule(mod, plan):
    """plan: list of (course, total_sections, stacked). `stacked` sections share
    T/Th 10:00-11:20; the rest are spread one per distinct start time."""
    secs = {}
    counts = {}
    for course, total, stacked in plan:
        counts[course] = total
        for i in range(1, total + 1):
            if i <= stacked:
                s = _sec(mod, course, i)
            else:
                h = _SPREAD_HOURS[i - stacked - 1]   # one section per distinct hour
                s = _sec(mod, course, i, start=(h, 30), end=(h, 59))
            secs[s.section_id] = s
    return secs, counts


def _c14(mod, plan) -> bool:
    secs, counts = _schedule(mod, plan)
    try:
        checker = mod.ConstraintChecker(course_sections=counts)
    except TypeError:
        checker = mod.ConstraintChecker()
    with contextlib.redirect_stdout(io.StringIO()):
        return checker._c14_time_dupes(secs, {})


class CheckerAppliesCap(_GlobalsIsolated):

    def _set(self, mod, value):
        _load(mod, f'setting,value\n{SETTING},"{value}"\n')

    def _both(self, value, plan, expected: bool):
        for name, mod in ENGINES:
            with self.subTest(engine=name, value=value, plan=plan):
                self._set(mod, value)
                self.assertIs(bool(_c14(mod, plan)), expected)

    def test_default_two_boundary(self):
        self._both("2", [("COMP1000", 6, 2)], True)
        self._both("2", [("COMP1000", 6, 3)], False)

    def test_number_three_allows_three_rejects_four(self):
        self._both("3", [("COMP1000", 6, 3)], True)
        self._both("3", [("COMP1000", 6, 4)], False)

    def test_number_one_rejects_two(self):
        self._both("1", [("COMP1000", 4, 1)], True)
        self._both("1", [("COMP1000", 4, 2)], False)

    def test_percent_boundary(self):
        # 50% of 4 = 2
        self._both("50%", [("COMP1000", 4, 2)], True)
        self._both("50%", [("COMP1000", 4, 3)], False)

    def test_percent_rounds_half_up(self):
        # 50% of 5 = 2.5 -> 3 (banker's round would wrongly give 2)
        self._both("50%", [("COMP1000", 5, 3)], True)
        self._both("50%", [("COMP1000", 5, 4)], False)

    def test_percent_is_per_course(self):
        # 50%: COMP2000 has 8 sections -> cap 4; COMP1000 has 4 -> cap 2.
        self._both("50%", [("COMP2000", 8, 4), ("COMP1000", 4, 2)], True)
        self._both("50%", [("COMP2000", 8, 4), ("COMP1000", 4, 3)], False)
        self._both("50%", [("COMP2000", 8, 5), ("COMP1000", 4, 1)], False)

    def test_unplaced_sections_count_toward_course_size_but_not_the_stack(self):
        # 50% of 6 = 3. Three placed at the same time plus three UNPLACED
        # placeholders also sitting at that time: the stack is 3, not 6.
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self._set(mod, "50%")
                secs = {}
                for i in range(1, 7):
                    s = _sec(mod, "COMP1000", i, unplaced=(i > 3))
                    secs[s.section_id] = s
                try:
                    checker = mod.ConstraintChecker(course_sections={"COMP1000": 6})
                except TypeError:
                    checker = mod.ConstraintChecker()
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertTrue(checker._c14_time_dupes(secs, {}))

    def test_label_no_longer_hardcodes_two(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self._set(mod, "3")
                secs, counts = _schedule(mod, [("COMP1000", 3, 1)])
                try:
                    checker = mod.ConstraintChecker(course_sections=counts)
                except TypeError:
                    checker = mod.ConstraintChecker()
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    checker.run_all(secs, {})
                c14 = [ln for ln in buf.getvalue().splitlines() if "C14" in ln]
                self.assertTrue(c14, msg=buf.getvalue())
                self.assertNotIn("≤ 2", c14[0])


# ── 4b. end-to-end: greedy engine (and CP-SAT if ortools is available) ──────

# One slot, one legal day-set (T/Th 10:00-11:20): every placed section of the
# course necessarily stacks at the same time, so the number placed IS the cap.
ONE_SLOT = ("start_time,stop_time,duration_min,slot_label,evening,Days Allowed\n"
            '10:00:00,11:20:00,80,lec_80,FALSE,"T,Th"\n')
NAMES = ["Ann", "Bob", "Cat", "Dan", "Eve", "Fay"]


def _fixture(n_sections: int, value):
    names = NAMES[:n_sections]
    return dict(
        courses=course_list([f"COMP1000,Intro,2,4,0,{n_sections},"]),
        prefs=preferences([f'COMP1000,Intro,"{", ".join(names)}"']),
        loads=faculty_load([f"{n},1," for n in names]),
        overrides={"timings.csv": ONE_SLOT, "settings.csv": _settings_with(value)},
    )


def _max_stack(rows) -> int:
    placed = [r for r in rows if r.get("Type") == "LEC"
              and (r.get("Status") or "").upper() != "UNPLACED"
              and r.get("Room") != "UNPLACED"]
    c = Counter((r["Course Designation/Number"], "".join(sorted(split_days(r["Days"]))),
                 r["Times"]) for r in placed)
    return max(c.values()) if c else 0


def _placed(rows) -> int:
    return sum(1 for r in rows if r.get("Type") == "LEC"
               and (r.get("Status") or "").upper() != "UNPLACED"
               and r.get("Room") != "UNPLACED")


class GreedyEndToEnd(SchedulerTestCase):

    def _run(self, n, value):
        run = self.run_scheduler(**_fixture(n, value))
        self.assertRanCleanly(run)
        return run

    def test_default_keeps_old_behaviour(self):
        run = self._run(3, None)
        self.assertEqual(_placed(run.rows), 2)
        self.assertEqual(_max_stack(run.rows), 2)
        self.assertTrue(run.constraints.get("C14"))

    def test_number_three_places_all_three_stacked(self):
        run = self._run(3, "3")
        self.assertEqual(run.unplaced, [], msg=run.stdout[-2000:])
        self.assertEqual(_max_stack(run.rows), 3)
        self.assertTrue(run.constraints.get("C14"), msg=run.stdout[-2000:])

    def test_number_one_never_stacks(self):
        run = self._run(3, "1")
        self.assertEqual(_max_stack(run.rows), 1, msg=run.stdout[-2000:])
        self.assertEqual(_placed(run.rows), 1)
        self.assertTrue(run.constraints.get("C14"))

    def test_hundred_percent_places_all(self):
        run = self._run(3, "100%")
        self.assertEqual(_placed(run.rows), 3, msg=run.stdout[-2000:])
        self.assertTrue(run.constraints.get("C14"))

    def test_percent_below_two(self):
        # 34% of 3 = 1.02 -> 1
        run = self._run(3, "34%")
        self.assertEqual(_max_stack(run.rows), 1, msg=run.stdout[-2000:])
        self.assertTrue(run.constraints.get("C14"))

    def test_percent_rounds_half_up_end_to_end(self):
        # 50% of 5 = 2.5 -> 3
        run = self._run(5, "50%")
        self.assertEqual(_placed(run.rows), 3, msg=run.stdout[-2000:])
        self.assertEqual(_max_stack(run.rows), 3)
        self.assertTrue(run.constraints.get("C14"))


def _python_with_ortools():
    if importlib.util.find_spec("ortools") is not None:
        return sys.executable
    for cand in (REPO / "venv" / "bin" / "python", REPO / ".venv" / "bin" / "python",
                 REPO / "venv" / "Scripts" / "python.exe"):
        if cand.exists():
            r = subprocess.run([str(cand), "-c", "import ortools"], capture_output=True)
            if r.returncode == 0:
                return str(cand)
    return None


_ORTOOLS_PY = _python_with_ortools()


@unittest.skipUnless(_ORTOOLS_PY, "ortools not installed")
class CpsatEndToEnd(SchedulerTestCase):
    """Reuses the greedy fixture's work dir (helpers writes the data there),
    then runs main_cpsat.py in the same directory."""

    def _run_cpsat(self, n, value):
        fx = _fixture(n, value)
        fx["overrides"]["settings.csv"] = (
            fx["overrides"]["settings.csv"].replace("cpsat_randomize,1", "cpsat_randomize,0"))
        greedy = self.run_scheduler(**fx)
        work = greedy.workdir
        shutil.copy2(REPO / "main_cpsat.py", work / "main_cpsat.py")
        proc = subprocess.run([_ORTOOLS_PY, "main_cpsat.py"], cwd=work, timeout=600,
                              capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(proc.returncode, 0, msg=out[-2000:])
        import csv
        p = work / "schedule_cpsat_simple.csv"
        self.assertTrue(p.exists(), msg=out[-2000:])
        with open(p, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        c14_pass = any("PASS" in ln and "C14" in ln for ln in out.splitlines())
        return rows, c14_pass, out

    def test_percent_rounds_half_up(self):
        rows, c14, out = self._run_cpsat(5, "50%")   # 2.5 -> 3
        self.assertEqual(_placed(rows), 3, msg=out[-2000:])
        self.assertEqual(_max_stack(rows), 3)
        self.assertTrue(c14, msg=out[-2000:])

    def test_number_one_never_stacks(self):
        rows, c14, out = self._run_cpsat(3, "1")
        self.assertEqual(_max_stack(rows), 1, msg=out[-2000:])
        self.assertTrue(c14, msg=out[-2000:])


# ── 4c. independent validator (tests/validate_schedule.py) ──────────────────

class ValidatorAppliesCap(SchedulerTestCase):
    """Hand-stack three sections of one course in a real run's schedule.json and
    check validate_schedule.py judges it against the configured cap."""

    @classmethod
    def setUpClass(cls):
        # A default run: two sections stacked on T/Th 10:00, the third UNPLACED.
        from helpers import run_scheduler
        run = run_scheduler(**_fixture(3, None))
        cls._workdirs.append(run.workdir)
        cls._base = run

    def _stacked_dir(self, value) -> Path:
        work = Path(tempfile.mkdtemp(prefix="sched-validate-"))
        type(self)._workdirs.append(work)
        shutil.copytree(self._base.workdir / "data", work / "data")
        (work / "data" / "settings.csv").write_text(_settings_with(value), encoding="utf-8")
        events = json.loads((self._base.workdir / "schedule.json").read_text())
        placed = [e for e in events if not e.get("unplaced") and e["room"] != "UNPLACED"]
        self.assertTrue(placed)
        used_rooms = {e["room"] for e in placed}
        rooms = [ln.split(",")[0] for ln in
                 (work / "data" / "rooms.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
                 if ln.strip()]
        free = next(r for r in rooms if r not in used_rooms)
        out = [e for e in events if not (e.get("unplaced") or e["room"] == "UNPLACED")]
        moved = [e for e in events if e.get("unplaced") or e["room"] == "UNPLACED"]
        self.assertTrue(moved, msg="fixture should leave one section unplaced")
        sid, prof = moved[0]["id"], moved[0]["prof"]
        for day in ("T", "Th"):
            out.append({"id": sid, "day": day, "course": sid, "prof": prof, "room": free,
                        "start": "10:00", "end": "11:20", "isLab": False,
                        "unplaced": False, "topup": False})
        (work / "schedule.json").write_text(json.dumps(out), encoding="utf-8")
        return work

    def _validate(self, work: Path):
        proc = subprocess.run([sys.executable, str(REPO / "tests" / "validate_schedule.py"),
                               str(work)], capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        return proc.returncode, out

    def test_three_stacked_fails_default_cap(self):
        code, out = self._validate(self._stacked_dir(None))
        self.assertEqual(code, 1, msg=out[-3000:])

    def test_three_stacked_fails_fifty_percent_of_three(self):
        # 50% of 3 = 1.5 -> 2
        code, out = self._validate(self._stacked_dir("50%"))
        self.assertEqual(code, 1, msg=out[-3000:])

    def test_three_stacked_passes_cap_three(self):
        code, out = self._validate(self._stacked_dir("3"))
        self.assertEqual(code, 0, msg=out[-3000:])

    def test_three_stacked_passes_hundred_percent(self):
        code, out = self._validate(self._stacked_dir("100%"))
        self.assertEqual(code, 0, msg=out[-3000:])


if __name__ == "__main__":
    unittest.main()
