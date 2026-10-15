"""Courses with more than one lab meeting a week (the 1-6-4 senior project).

COMP5500 Senior Project is one 60-minute lecture plus three 120-minute labs a
week, per section. It is expressed purely as data:

    course-list.csv       COMP5500,Senior Project,1,1,6,6,
    meeting_patterns.csv  *,1,1,60,120,3,...   (lab_meetings_per_week = 3)

`lab_meetings_per_week` is an optional column: a file without it, or a blank
cell, means the historical single weekly lab. Each lab meeting falls on its own
weekday and never on a lecture day; the section keeps ONE "-LAB" entry whose
`days` hold every lab meeting (all at one time, like a multi-day lecture), the
same professor teaches lecture and labs, and the whole section counts once
toward faculty load. C17 (lab starts with its lecture) applies only to the
single-lab offering.

Covered here: the loader, both engines end to end, both ConstraintCheckers, the
independent tests/validate_schedule.py, and that single-lab courses keep their
ids and shape. CP-SAT end-to-end tests are skipped only if ortools is missing.
"""
from __future__ import annotations

import ast
import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import main_cpsat  # noqa: E402  (imports without ortools; the solver import is lazy)
from helpers import (  # noqa: E402
    DATA, REPO, SchedulerTestCase, course_list, faculty_load, preferences,
    run_scheduler, split_days,
)

ENGINES = (("greedy", main), ("cpsat", main_cpsat))
DEFAULTS = REPO / "data-defaults"


def _has_ortools() -> bool:
    try:
        import ortools  # noqa: F401
        return True
    except ImportError:
        return False


def _settings_no_randomize() -> str:
    text = (DATA / "settings.csv").read_text(encoding="utf-8-sig")
    return text.replace("cpsat_randomize,1", "cpsat_randomize,0")


def _quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a, **kw)
    return out, buf.getvalue()


# ── 1. loader and rule lookup ────────────────────────────────────────────────

class Loader(unittest.TestCase):
    """The column is optional, defaults to 1, and bad values fall back to 1."""

    def setUp(self):
        self._saved = {name: mod.MEETING_RULES for name, mod in ENGINES}
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        for name, mod in ENGINES:
            mod.MEETING_RULES = self._saved[name]
        self._dir.cleanup()

    def _load(self, mod, text):
        p = os.path.join(self._dir.name, "meeting_patterns.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return _quiet(mod.load_meeting_patterns, p)

    def test_file_without_the_column_means_one_lab_meeting(self):
        text = ("subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes,notes\n"
                "*,2,3,90,105,3-2-4\n"
                "*,1,1,60,120,would be 1-6-4 but has no count column\n")
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                rules, out = self._load(mod, text)
                self.assertEqual([r.lab_meetings for r in rules], [1, 1])
                self.assertNotIn("[WARN]", out)

    def test_column_is_read_and_blank_means_one(self):
        text = ("subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes,"
                "lab_meetings_per_week,notes\n"
                "*,2,3,90,105,,blank\n"
                "*,2,4,80,0,1,explicit one\n"
                "*,1,1,60,120,3,1-6-4\n")
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                rules, out = self._load(mod, text)
                self.assertEqual([r.lab_meetings for r in rules], [1, 1, 3])
                self.assertNotIn("[WARN]", out)

    def test_unusable_values_warn_and_fall_back_to_one(self):
        for bad in ("0", "-2", "abc", "6", "2.5"):
            text = ("subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes,"
                    f"lab_meetings_per_week\n*,1,1,60,120,{bad}\n")
            for name, mod in ENGINES:
                with self.subTest(engine=name, value=bad):
                    rules, out = self._load(mod, text)
                    self.assertEqual(rules[0].lab_meetings, 1)
                    self.assertIn("lab_meetings_per_week", out)

    def test_lab_meetings_per_week_lookup(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                mod.MEETING_RULES = list(mod.DEFAULT_MEETING_RULES)
                # 1-6-4: 60-min lecture, three 120-min labs.
                self.assertEqual(mod.meeting_minutes("COMP5500", 1, 1), 60)
                self.assertEqual(mod.lab_minutes("COMP5500", 1, 6, 1), 120)
                self.assertEqual(mod.lab_meetings_per_week("COMP5500", 1, 6, 1), 3)
                # The single-lab 3-2-4 offering is unchanged.
                self.assertEqual(mod.lab_minutes("COMP1000", 3, 2, 2), 105)
                self.assertEqual(mod.lab_meetings_per_week("COMP1000", 3, 2, 2), 1)
                # No lab, or a graduate course: no lab meetings at all.
                self.assertEqual(mod.lab_meetings_per_week("COMP2000", 4, 0, 2), 0)
                self.assertEqual(mod.lab_meetings_per_week("COMP5100", 1, 6, 1), 0)
                # Lab hours but no matching rule: one lab, as before.
                self.assertEqual(mod.lab_meetings_per_week("COMP3000", 7, 2, 2), 1)

    def test_lab_day_patterns(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(mod.lab_day_patterns(1, ["M", "W"]), [["T"], ["Th"], ["F"]])
                three = mod.lab_day_patterns(3, ["T"])
                self.assertEqual(len(three), 4)                 # C(4, 3)
                for p in three:
                    self.assertEqual(len(set(p)), 3)
                    self.assertNotIn("T", p)
                self.assertEqual(mod.lab_day_patterns(5, ["M"]), [])
                self.assertEqual(mod.lab_max_minutes(105), 105)
                self.assertEqual(mod.lab_max_minutes(120), 120)


class ShippedData(unittest.TestCase):
    def test_senior_project_is_1_6_4_in_data_and_defaults(self):
        for d in (DATA, DEFAULTS):
            with self.subTest(dir=d.name):
                with open(d / "course-list.csv", newline="", encoding="utf-8-sig") as f:
                    row = next(r for r in csv.DictReader(f) if r["Course number"] == "COMP5500")
                self.assertEqual((row["lecture days per week"], row["lecture hours"],
                                  row["lab hours"], row["number of sections"]),
                                 ("1", "1", "6", "6"))
                rules = main.load_meeting_patterns(str(d / "meeting_patterns.csv"))
                saved = main.MEETING_RULES
                try:
                    main.MEETING_RULES = rules
                    self.assertEqual(main.meeting_minutes("COMP5500", 1, 1), 60)
                    self.assertEqual(main.lab_minutes("COMP5500", 1, 6, 1), 120)
                    self.assertEqual(main.lab_meetings_per_week("COMP5500", 1, 6, 1), 3)
                finally:
                    main.MEETING_RULES = saved

    def test_timings_offer_60_and_120_minute_daytime_slots_every_weekday(self):
        slots = main.load_timeslots(str(DATA / "timings.csv"))
        for dur in (60, 120):
            daytime = [s for s in slots if s.duration_min == dur and not s.evening]
            self.assertTrue(daytime, msg=f"no {dur}-minute slot")
            days = set().union(*(set(s.days_allowed) for s in daytime))
            self.assertEqual(days, set(main.ALL_DAYS), msg=f"{dur}-min slots miss a day")

    def test_server_header_check_accepts_both_file_shapes(self):
        """server.py's REQUIRED_HEADERS must accept the new column and a file
        without it. Read by AST so this runs without fastapi installed."""
        tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
        required = None
        for node in ast.walk(tree):
            target = getattr(node, "target", None)
            if isinstance(node, ast.AnnAssign) and getattr(target, "id", "") == "REQUIRED_HEADERS":
                required = ast.literal_eval(node.value)["meeting_patterns"]
        self.assertIsNotNone(required)
        with open(DATA / "meeting_patterns.csv", newline="", encoding="utf-8-sig") as f:
            new_header = next(csv.reader(f))
        self.assertIn("lab_meetings_per_week", new_header)
        old_header = [h for h in new_header if h != "lab_meetings_per_week"]
        for header in (new_header, old_header):
            norm = {h.strip().lower() for h in header}
            self.assertEqual([c for c in required if c.lower() not in norm], [])


# ── 2. ConstraintChecker (both engines) ──────────────────────────────────────

def _ss(mod, sid, days, start, end, *, fac="Ann", is_lab=False, dpw=0, meeting_min=0,
        course="COMP5500"):
    return mod.ScheduledSection(
        section_id=sid, course_number=course, course_name=course, faculty=fac,
        room="R1", days=list(days), start_time=time(*start), end_time=time(*end),
        has_lab=not is_lab, is_lab=is_lab, days_per_week=dpw, meeting_min=meeting_min)


def _senior(mod, *, lab_days=("M", "W", "F"), lab_start=(13, 0), lab_end=(15, 0),
            lab_fac="Ann", lec_day="T"):
    return {
        "COMP5500-1": _ss(mod, "COMP5500-1", [lec_day], (10, 0), (11, 0), dpw=1),
        "COMP5500-1-LAB": _ss(mod, "COMP5500-1-LAB", lab_days, lab_start, lab_end,
                              fac=lab_fac, is_lab=True, dpw=3, meeting_min=120),
    }


def _checker(mod):
    slots = [mod.TimeSlot(time(10, 0), time(11, 0), 60, "lec_60", False, []),
             mod.TimeSlot(time(13, 0), time(15, 0), 120, "lab_120", False, []),
             mod.TimeSlot(time(13, 0), time(14, 45), 105, "lab_105", False, []),
             mod.TimeSlot(time(13, 0), time(14, 30), 90, "lec_90", False, [])]
    return mod.ConstraintChecker({"COMP5500": ["Ann"], "COMP1000": ["Ann"]}, slots)


class Checker(unittest.TestCase):
    def _run(self, mod, method, secs):
        ok, _out = _quiet(getattr(_checker(mod), method), secs, {"Ann": 3})
        return ok

    def test_a_valid_1_6_4_section_passes_every_lab_check(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                secs = _senior(mod)
                for m in ("_c4_days", "_c7_durations", "_c9_lab_day", "_c10_lab_one_day",
                          "_c13_lab_faculty", "_c17_lab_same_start", "_c1_load"):
                    self.assertTrue(self._run(mod, m, secs), msg=m)

    def test_two_labs_on_the_same_day_fail_c10(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertFalse(self._run(mod, "_c10_lab_one_day",
                                           _senior(mod, lab_days=("M", "M", "W"))))

    def test_too_few_lab_days_fail_c10(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertFalse(self._run(mod, "_c10_lab_one_day",
                                           _senior(mod, lab_days=("M", "W"))))

    def test_a_lab_on_the_lecture_day_fails_c9(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertFalse(self._run(mod, "_c9_lab_day",
                                           _senior(mod, lab_days=("M", "T", "F"))))

    def test_labs_by_another_professor_fail_c13(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertFalse(self._run(mod, "_c13_lab_faculty", _senior(mod, lab_fac="Bob")))

    def test_wrong_lab_length_fails_c7(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertFalse(self._run(mod, "_c7_durations",
                                           _senior(mod, lab_end=(14, 45))))

    def test_c17_exempts_multi_lab_but_still_binds_a_single_lab(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                # Multi-lab block at 13:00, lecture at 10:00: exempt.
                self.assertTrue(self._run(mod, "_c17_lab_same_start", _senior(mod)))
                single = {
                    "COMP1000-1": _ss(mod, "COMP1000-1", ["M", "W"], (10, 0), (11, 30),
                                      course="COMP1000", dpw=2),
                    "COMP1000-1-LAB": _ss(mod, "COMP1000-1-LAB", ["F"], (13, 0), (14, 45),
                                          course="COMP1000", is_lab=True),
                }
                self.assertFalse(self._run(mod, "_c17_lab_same_start", single))
                self.assertTrue(self._run(mod, "_c10_lab_one_day", single))

    def test_load_counts_the_section_once_and_c4_counts_lab_days(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                secs = _senior(mod)
                ok, out = _quiet(_checker(mod)._c1_load, secs, {"Ann": 1})
                self.assertTrue(ok, msg=out)            # 1 course, not 4
                # T + M/W/F = 4 days: fine. A second course on Th makes it 5.
                secs["COMP1000-1"] = _ss(mod, "COMP1000-1", ["Th"], (8, 0), (9, 30),
                                         course="COMP1000", dpw=1)
                self.assertFalse(self._run(mod, "_c4_days", secs))


# ── 3. end to end, both engines ──────────────────────────────────────────────

# Two senior-project sections taught by Ann, whose load is exactly 2: if the
# labs were counted toward load, C1 would report her overloaded. COMP1000 is an
# ordinary 3-2-4 course with a single lab, which must keep its historical shape.
FIXTURE = dict(
    courses=course_list([
        "COMP5500,Senior Project,1,1,6,2,",
        "COMP1000,Computer Science I,2,3,2,1,",
    ]),
    prefs=preferences([
        "COMP5500,Senior Project,Ann",
        "COMP1000,Computer Science I,Bob",
    ]),
    loads=faculty_load(["Ann,2,", "Bob,3,"]),
)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


class _EndToEnd:
    events: list
    stdout: str
    rows: list
    banner: list

    def _by_id(self):
        out = {}
        for e in self.events:
            out.setdefault(e["id"], []).append(e)
        return out

    def test_every_section_is_placed(self):
        self.assertFalse([e for e in self.events if e["unplaced"]], msg=self.stdout[-2000:])
        self.assertNotIn("[CRITICAL]", self.stdout)

    def test_every_hard_constraint_passes(self):
        # C16 (weekday balance) is a soft, report-only goal (SCHEDULING_
        # CONSTRAINTS.md §2) that an 11-meeting fixture cannot be expected to meet.
        for line in self.stdout.splitlines():
            if "C16" not in line:
                self.assertNotRegex(line, r"✗ FAIL\s+C\d+", msg=self.stdout[-3000:])
        self.assertRegex(self.stdout, r"PASS\s+C10\b")
        self.assertRegex(self.stdout, r"PASS\s+C1\b")

    def test_three_120_minute_labs_on_distinct_non_lecture_days(self):
        by_id = self._by_id()
        for sec in ("COMP5500-1", "COMP5500-2"):
            lec, lab = by_id.get(sec, []), by_id.get(f"{sec}-LAB", [])
            self.assertEqual(len(lec), 1, msg=lec)
            self.assertEqual(_minutes(lec[0]["end"]) - _minutes(lec[0]["start"]), 60)
            self.assertEqual(len(lab), 3, msg=lab)
            lab_days = [e["day"] for e in lab]
            self.assertEqual(len(set(lab_days)), 3, msg=lab)
            self.assertNotIn(lec[0]["day"], lab_days)
            for e in lab:
                self.assertTrue(e["isLab"])
                self.assertEqual(_minutes(e["end"]) - _minutes(e["start"]), 120, msg=e)
            self.assertEqual({e["prof"] for e in lec + lab}, {"Ann"})
            # No lab meeting overlaps the lecture (different days) — and no two
            # of Ann's meetings overlap each other on any day.
        ann = [e for e in self.events if e["prof"] == "Ann"]
        for a in ann:
            for b in ann:
                if a is not b and a["day"] == b["day"]:
                    self.assertTrue(_minutes(a["end"]) + 15 <= _minutes(b["start"])
                                    or _minutes(b["end"]) + 15 <= _minutes(a["start"]),
                                    msg=f"{a} / {b}")

    def test_single_lab_course_is_unchanged(self):
        by_id = self._by_id()
        self.assertEqual(sorted(k for k in by_id if k.startswith("COMP1000")),
                         ["COMP1000-1", "COMP1000-1-LAB"])
        lec, lab = by_id["COMP1000-1"], by_id["COMP1000-1-LAB"]
        self.assertEqual(len(lec), 2)
        self.assertEqual(len(lab), 1)
        self.assertEqual(lab[0]["start"], lec[0]["start"])               # C17
        self.assertEqual(_minutes(lab[0]["end"]) - _minutes(lab[0]["start"]), 105)
        self.assertNotIn(lab[0]["day"], {e["day"] for e in lec})

    def test_exports_carry_one_lab_row_with_all_three_days(self):
        labs = [r for r in self.rows
                if r["Course Designation/Number"] == "COMP5500" and r["Type"] == "Lab"]
        self.assertEqual(len(labs), 2, msg=labs)
        for r in labs:
            self.assertEqual(len(split_days(r["Days"])), 3, msg=r)
            self.assertEqual(r["Faculty"], "Ann")
        banner = [r for r in self.banner if r["Subj"] == "COMP" and r["Crse"] == "5500"]
        self.assertEqual(sorted(r["Section"] for r in banner), ["1", "1L", "2", "2L"])
        for r in banner:
            if r["Section"].endswith("L"):
                self.assertEqual(len(split_days(r["Days"])), 3, msg=r)

    def test_independent_validator_passes(self):
        proc = subprocess.run([sys.executable, str(REPO / "tests" / "validate_schedule.py"),
                               str(self.val_dir)], capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(proc.returncode, 0, msg=out[-4000:])
        self.assertIn("multi-lab sections (2)", out)


class GreedyEndToEnd(_EndToEnd, SchedulerTestCase):
    @classmethod
    def setUpClass(cls):
        run = run_scheduler(**FIXTURE)
        cls._workdirs.append(run.workdir)
        assert "Traceback" not in run.stdout and run.returncode == 0, run.stdout[-2000:]
        cls.events, cls.stdout, cls.val_dir = run.events, run.stdout, run.workdir
        cls.rows, cls.banner = run.rows, run.banner


def _read_csv(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _run_cpsat(work: Path):
    shutil.copy2(REPO / "main_cpsat.py", work / "main_cpsat.py")
    proc = subprocess.run([sys.executable, "main_cpsat.py"], cwd=work, timeout=600,
                          capture_output=True, text=True)
    return proc.stdout + proc.stderr, proc.returncode


@unittest.skipUnless(_has_ortools(), "ortools not installed")
class CpsatEndToEnd(_EndToEnd, SchedulerTestCase):
    @classmethod
    def setUpClass(cls):
        run = run_scheduler(**FIXTURE, overrides={"settings.csv": _settings_no_randomize()})
        cls._workdirs.append(run.workdir)
        work = run.workdir
        out, rc = _run_cpsat(work)
        assert "Traceback" not in out and rc == 0, out[-2000:]
        cls.events = json.loads((work / "schedule_cpsat.json").read_text())
        cls.stdout = out
        cls.rows = _read_csv(work / "schedule_cpsat_simple.csv")
        cls.banner = _read_csv(work / "schedule_cpsat.csv")
        val = work / "cpsat_validate"
        val.mkdir()
        shutil.copytree(work / "data", val / "data")
        shutil.copy2(work / "schedule_cpsat.json", val / "schedule.json")
        cls.val_dir = val


class GreedySparesSoleTeachers(SchedulerTestCase):
    """A multi-lab section uses four of a professor's four teaching days, so the
    greedy engine offers it to the sole qualified professor of another course
    only after everyone else (the COMP5500 / Memo / COMP6760 case)."""

    def test_multi_lab_section_goes_to_the_professor_with_no_sole_duty(self):
        run = self.run_scheduler(
            courses=course_list(["COMP5500,Senior Project,1,1,6,1,",
                                 "COMP6760,Computer Vision,1,3,0,1,"]),
            # Memo is listed first and is equally loaded, so without the rule
            # he would be tried first.
            prefs=preferences(['COMP5500,Senior Project,"Memo, Ann"',
                               "COMP6760,Computer Vision,Memo"]),
            loads=faculty_load(["Memo,2,", "Ann,2,"]),
        )
        self.assertRanCleanly(run)
        self.assertEqual(set(run.faculty_of("COMP5500")), {"Ann"}, msg=run.stdout[-2000:])
        self.assertEqual(set(run.faculty_of("COMP6760")), {"Memo"}, msg=run.stdout[-2000:])


class TooManyLabDaysIsReportedNotCrashed(SchedulerTestCase):
    """2 lecture days + 4 lab meetings needs 6 weekdays. Both engines must report
    the section UNPLACED with that reason rather than crash or invent a day."""

    PATTERNS = ("subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes,"
                "lab_meetings_per_week,notes\n"
                "*,2,1,60,120,4,impossible on purpose\n")
    FIX = dict(
        courses=course_list(["COMP4999,Impossible Labs,2,1,8,1,"]),
        prefs=preferences(["COMP4999,Impossible Labs,Ann"]),
        loads=faculty_load(["Ann,3,"]),
        overrides={"meeting_patterns.csv": PATTERNS, "settings.csv": _settings_no_randomize()},
    )

    def _check(self, events, out):
        self.assertTrue(events, msg=out[-2000:])
        self.assertTrue(all(e["unplaced"] for e in events), msg=events)
        self.assertIn("the week has 5", out)

    def test_greedy(self):
        run = self.run_scheduler(**self.FIX)
        self.assertNotIn("Traceback", run.stdout, msg=run.stdout[-2000:])
        self.assertEqual(run.returncode, 0)
        self._check(run.events, run.stdout)

    @unittest.skipUnless(_has_ortools(), "ortools not installed")
    def test_cpsat(self):
        run = self.run_scheduler(**self.FIX)
        out, rc = _run_cpsat(run.workdir)
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(rc, 0)
        self._check(json.loads((run.workdir / "schedule_cpsat.json").read_text()), out)


class NoLegalLabBlockIsATimingProblem(SchedulerTestCase):
    """When timings.csv allows the 120-minute slot on only two weekdays, no
    three-day lab block exists at all. Greedy must say that (TIMING), not blame
    the concurrency ceiling, which is what an empty all() used to report."""

    def test_greedy(self):
        with open(DATA / "timings.csv", newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            if r["slot_label"] == "lab_120":
                r["Days Allowed"] = "M,T"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        timings = buf.getvalue()
        run = self.run_scheduler(
            courses=course_list(["COMP5500,Senior Project,1,1,6,1,"]),
            prefs=preferences(["COMP5500,Senior Project,Ann"]),
            loads=faculty_load(["Ann,1,"]),
            overrides={"timings.csv": timings})
        self.assertRanCleanly(run)
        self.assertIn("COMP5500-1", run.unplaced, msg=run.stdout[-2000:])
        self.assertRegex(run.stdout, r"\[CRITICAL\] COMP5500-1 .*TIMING .*'Days Allowed'",
                         msg=run.stdout[-2000:])


class LongSingleLabPassesC7(SchedulerTestCase):
    """lab_max_minutes() lets a SINGLE weekly lab longer than 105 minutes take a
    slot of its own length (a 120-minute lab, now that timings.csv has them).
    C7 must then hold it to that length, not to the 100-110 window of the 3-2-4
    offering — otherwise the run reports its own placement as a violation."""

    PATTERNS = ("subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes,"
                "lab_meetings_per_week,notes\n"
                "*,2,3,90,120,1,one 120-minute lab\n")
    FIX = dict(
        courses=course_list(["COMP3000,Long Lab,2,3,2,1,"]),
        prefs=preferences(["COMP3000,Long Lab,Ann"]),
        loads=faculty_load(["Ann,1,"]),
        overrides={"meeting_patterns.csv": PATTERNS, "settings.csv": _settings_no_randomize()},
    )

    def _check(self, events, out):
        lab = [e for e in events if e["isLab"]]
        self.assertEqual(len(lab), 1, msg=events)
        self.assertFalse(lab[0]["unplaced"], msg=lab)
        h0, m0 = map(int, lab[0]["start"].split(":"))
        h1, m1 = map(int, lab[0]["end"].split(":"))
        self.assertEqual((h1 * 60 + m1) - (h0 * 60 + m0), 120, msg=lab)
        self.assertRegex(out, r"✓ PASS\s+C7 ", msg=out[-3000:])
        self.assertRegex(out, r"✓ PASS\s+C17 ", msg=out[-3000:])

    def test_greedy(self):
        run = self.run_scheduler(**self.FIX)
        self.assertRanCleanly(run)
        self._check(run.events, run.stdout)

    @unittest.skipUnless(_has_ortools(), "ortools not installed")
    def test_cpsat(self):
        run = self.run_scheduler(**self.FIX)
        out, rc = _run_cpsat(run.workdir)
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(rc, 0)
        self._check(json.loads((run.workdir / "schedule_cpsat.json").read_text()), out)

    def test_checker_holds_a_long_single_lab_to_its_own_length(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(mod.lab_meeting_min(120, 1), 120)
                self.assertEqual(mod.lab_meeting_min(105, 1), 0)    # historical window
                self.assertEqual(mod.lab_meeting_min(120, 3), 120)


# ── 4. the independent validator catches violations ──────────────────────────

def _ev(sid, day, start, end, *, lab=False, prof="Ann", room="ANXCN 005"):
    return {"id": sid, "day": day, "course": sid.rsplit("-", 1)[0] if not lab else sid,
            "prof": prof, "room": room, "start": start, "end": end, "isLab": lab,
            "unplaced": False, "topup": False}


def _good_events():
    return [_ev("COMP5500-1", "T", "10:00", "11:00")] + [
        _ev("COMP5500-1-LAB", d, "13:00", "15:00", lab=True) for d in ("M", "W", "F")]


class ValidatorCatchesLabViolations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="sched-multilab-val-"))
        shutil.copytree(DATA, cls.dir / "data")
        (cls.dir / "data" / "course-list.csv").write_text(
            course_list(["COMP5500,Senior Project,1,1,6,1,"]), encoding="utf-8")
        (cls.dir / "data" / "prof_preferences.csv").write_text(
            preferences(["COMP5500,Senior Project,Ann"]), encoding="utf-8")
        (cls.dir / "data" / "faculty_load.csv").write_text(
            faculty_load(["Ann,1,"]), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def _validate(self, events):
        (self.dir / "schedule.json").write_text(json.dumps(events), encoding="utf-8")
        proc = subprocess.run([sys.executable, str(REPO / "tests" / "validate_schedule.py"),
                               str(self.dir)], capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        return proc.returncode, out

    def test_a_correct_section_passes(self):
        rc, out = self._validate(_good_events())
        self.assertEqual(rc, 0, msg=out[-3000:])

    def test_two_labs_on_the_same_day(self):
        evs = _good_events()
        evs[-1]["day"] = "M"
        rc, out = self._validate(evs)
        self.assertEqual(rc, 1)
        self.assertIn("two lab meetings on the same day", out)

    def test_a_missing_lab_meeting(self):
        rc, out = self._validate(_good_events()[:-1])
        self.assertEqual(rc, 1)
        self.assertIn("meeting_patterns.csv says 3", out)

    def test_a_lab_on_the_lecture_day(self):
        evs = _good_events()
        evs[-1]["day"] = "T"
        rc, out = self._validate(evs)
        self.assertEqual(rc, 1)
        self.assertIn("lab meets on lecture day", out)

    def test_a_lab_of_the_wrong_length(self):
        evs = [e if not e["isLab"] else dict(e, end="14:45") for e in _good_events()]
        rc, out = self._validate(evs)
        self.assertEqual(rc, 1)
        self.assertIn("lab meetings last [105] min", out)

    def test_labs_taught_by_someone_else(self):
        evs = _good_events()
        evs[-1]["prof"] = "TBA"
        rc, out = self._validate(evs)
        self.assertEqual(rc, 1)
        self.assertIn("different people", out)


if __name__ == "__main__":
    unittest.main()
