"""5500 is senior project / senior design in every subject, so it is undergraduate.

Client rule: "all >= 5000 are grad except 5500s, since they will always be
senior project." Every place that decides grad-ness — both engines' is_grad(),
the scheduling tier, the meeting-length / lab / days-per-week rules, both
ConstraintCheckers (C12, C18) and the independent tests/validate_schedule.py —
must treat COMP5500, CYBR5500, DATA5500, ... as undergraduate, while every
other course numbered 5000+ stays graduate.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402
import main_cpsat  # noqa: E402  (imports without ortools; the solver import is lazy)
from helpers import (  # noqa: E402
    DATA, REPO, SchedulerTestCase, course_list, faculty_load, preferences,
)

ENGINES = (("greedy", main), ("cpsat", main_cpsat))


def _has_ortools() -> bool:
    try:
        import ortools  # noqa: F401
        return True
    except ImportError:
        return False


def _settings_no_randomize() -> str:
    text = (DATA / "settings.csv").read_text(encoding="utf-8-sig")
    return text.replace("cpsat_randomize,1", "cpsat_randomize,0")


# ── unit level: the shared rule in both engines ─────────────────────────────

class IsGradBothEngines(unittest.TestCase):
    def test_5500_is_undergraduate_in_every_subject(self):
        for name, mod in ENGINES:
            for cn in ("COMP5500", "CYBR5500", "DATA5500", "AAIN5500", "COMP 5500"):
                with self.subTest(engine=name, course=cn):
                    self.assertFalse(mod.is_grad(cn))

    def test_other_5000_plus_courses_stay_graduate(self):
        for name, mod in ENGINES:
            for cn in ("COMP5000", "MATH5000", "COMP5100", "DATA5501", "COMP5499",
                       "CYBR5600", "COMP6000", "COMP7500"):
                with self.subTest(engine=name, course=cn):
                    self.assertTrue(mod.is_grad(cn))

    def test_below_5000_is_undergraduate(self):
        for name, mod in ENGINES:
            for cn in ("COMP1000", "COMP4999", "DATA3125"):
                with self.subTest(engine=name, course=cn):
                    self.assertFalse(mod.is_grad(cn))

    def test_5500_is_scheduled_in_the_upper_undergraduate_tier(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(mod.course_level_tier("COMP5500"), mod.LEVEL_UPPER_UG)
                self.assertEqual(mod.course_level_tier("COMP4550"), mod.LEVEL_UPPER_UG)
                self.assertEqual(mod.course_level_tier("COMP5100"), mod.LEVEL_GRAD)
                self.assertEqual(mod.course_level_tier("COMP2000"), mod.LEVEL_LOWER_UG)

    def test_5500_follows_meeting_patterns_and_keeps_its_lab(self):
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                # Shipped rows: DATA5500 2,3,2 and CYBR5500 2,4,0.
                self.assertEqual(mod.lecture_days_for("DATA5500", 2), 2)
                self.assertEqual(mod.meeting_minutes("DATA5500", 3, 2), 90)
                self.assertEqual(mod.lab_minutes("DATA5500", 3, 2, 2), 105)
                self.assertEqual(mod.lecture_days_for("CYBR5500", 2), 2)
                self.assertEqual(mod.meeting_minutes("CYBR5500", 4, 2), 80)
                # A real grad course still gets one 155-min evening and no lab.
                self.assertEqual(mod.lecture_days_for("DATA5100", 2), 1)
                self.assertEqual(mod.meeting_minutes("DATA5100", 3, 1), mod.GRAD_MEETING_MIN)
                self.assertEqual(mod.lab_minutes("DATA5100", 3, 2, 1), 0)

    def test_validator_uses_the_same_rule(self):
        # validate_schedule.py runs on import, so check its constants by source.
        src = (REPO / "tests" / "validate_schedule.py").read_text(encoding="utf-8")
        self.assertIn("GRAD_MIN_NUMBER = 5000", src)
        self.assertIn("UNDERGRAD_EXCEPTIONS = frozenset({5500})", src)
        for name, mod in ENGINES:
            with self.subTest(engine=name):
                self.assertEqual(mod.GRAD_MIN_NUMBER, 5000)
                self.assertEqual(set(mod.UNDERGRAD_EXCEPTIONS), {5500})


# ── end to end ───────────────────────────────────────────────────────────────

# DATA5500 / CYBR5500 mirror the shipped rows; MATH5100 is a real grad course
# taught by CYBR5500's professor, so C18 and C12 both have something to judge.
FIXTURE = dict(
    courses=course_list([
        "DATA5500,Senior Design,2,3,2,1,",
        "CYBR5500,Cyber Senior Project,2,4,0,1,",
        "MATH5100,Statistical Thinking,1,4,0,2,",
    ]),
    prefs=preferences([
        "DATA5500,Senior Design,Bob",
        "CYBR5500,Cyber Senior Project,Ann",
        "MATH5100,Statistical Thinking,Ann",
    ]),
    loads=faculty_load(["Ann,3,", "Bob,3,"]),
)


def _events_for(events, course):
    return [e for e in events if e["id"].split("-")[0] == course]


class _EndToEnd:
    """Shared assertions; subclasses supply `events` and `stdout`."""

    events: list
    stdout: str

    def _lectures(self, course):
        return [e for e in _events_for(self.events, course) if not e["isLab"]]

    def test_5500s_are_placed_in_daytime_on_their_declared_days(self):
        for course in ("DATA5500", "CYBR5500"):
            lec = self._lectures(course)
            self.assertTrue(lec, msg=f"{course} missing:\n{self.stdout[-2000:]}")
            for e in lec:
                self.assertFalse(e["unplaced"], msg=f"{course} unplaced:\n{self.stdout[-2000:]}")
                self.assertLess(e["start"], "18:00", msg=f"{course} forced to the evening: {e}")
            self.assertEqual(len({e["day"] for e in lec}), 2, msg=f"{course}: {lec}")

    def test_data5500_lab_is_scheduled(self):
        labs = [e for e in _events_for(self.events, "DATA5500") if e["isLab"]]
        self.assertEqual(len(labs), 1, msg=self.stdout[-2000:])
        self.assertFalse(labs[0]["unplaced"])
        self.assertEqual(labs[0]["prof"], "Bob")
        lec_days = {e["day"] for e in self._lectures("DATA5500")}
        self.assertNotIn(labs[0]["day"], lec_days)

    def test_5500_does_not_count_toward_the_two_grad_section_cap(self):
        # Ann teaches 2 MATH5100 sections (the grad cap) AND CYBR5500. Before
        # the 5500 exception, CYBR5500 was her third "grad" section -> TBA.
        self.assertEqual({e["prof"] for e in self._lectures("CYBR5500")}, {"Ann"})
        self.assertEqual({e["prof"] for e in self._lectures("MATH5100")}, {"Ann"})

    def test_real_grad_course_still_meets_one_evening_at_1800(self):
        by_sec = {}
        for e in self._lectures("MATH5100"):
            by_sec.setdefault(e["id"], []).append(e)
        self.assertEqual(len(by_sec), 2, msg=self.stdout[-2000:])
        for sid, evs in by_sec.items():
            self.assertEqual(len(evs), 1, msg=f"{sid}: {evs}")
            self.assertEqual(evs[0]["start"], "18:00")
        self.assertFalse([e for e in _events_for(self.events, "MATH5100") if e["isLab"]])

    def test_checker_reports_c12_and_c18_pass(self):
        for c in ("C12", "C18"):
            self.assertRegex(self.stdout, rf"PASS\s+{c}\b", msg=self.stdout[-3000:])

    def test_independent_validator_passes(self):
        proc = subprocess.run([sys.executable, str(REPO / "tests" / "validate_schedule.py"),
                               str(self.val_dir)], capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(proc.returncode, 0, msg=out[-4000:])
        self.assertNotIn("DATA5500-1 (grad)", out)
        self.assertNotIn("CYBR5500-1 (grad)", out)


class GreedyEndToEnd(_EndToEnd, SchedulerTestCase):
    @classmethod
    def setUpClass(cls):
        from helpers import run_scheduler
        run = run_scheduler(**FIXTURE)
        cls._workdirs.append(run.workdir)
        assert "Traceback" not in run.stdout and run.returncode == 0, run.stdout[-2000:]
        cls.events, cls.stdout, cls.val_dir = run.events, run.stdout, run.workdir


@unittest.skipUnless(_has_ortools(), "ortools not installed")
class CpsatEndToEnd(_EndToEnd, SchedulerTestCase):
    @classmethod
    def setUpClass(cls):
        from helpers import run_scheduler
        run = run_scheduler(**FIXTURE, overrides={"settings.csv": _settings_no_randomize()})
        cls._workdirs.append(run.workdir)
        work = run.workdir
        shutil.copy2(REPO / "main_cpsat.py", work / "main_cpsat.py")
        proc = subprocess.run([sys.executable, "main_cpsat.py"], cwd=work, timeout=600,
                              capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        assert "Traceback" not in out and proc.returncode == 0, out[-2000:]
        cls.events = json.loads((work / "schedule_cpsat.json").read_text())
        cls.stdout = out
        # The validator reads schedule.json; give it the CP-SAT output instead.
        val = work / "cpsat_validate"
        val.mkdir()
        shutil.copytree(work / "data", val / "data")
        shutil.copy2(work / "schedule_cpsat.json", val / "schedule.json")
        cls.val_dir = val


# ── an unplaceable 5500 is not parked in the grad evening slot ──────────────

class UnplaceableSeniorProjectPlaceholder(SchedulerTestCase):
    """A 5500 declared as 1 day x 3 h (COMP5500's course-list row before it became
    the 1-6-4 offering) needs a 180-min meeting, which no undergraduate slot
    offers. It must be reported UNPLACED, and its invented placeholder time must
    not be the 18:00 grad evening slot (that read as 'still grad')."""

    FIX = dict(
        courses=course_list(["COMP5500,Senior Project,1,3,0,1,"]),
        prefs=preferences(["COMP5500,Senior Project,Ann"]),
        loads=faculty_load(["Ann,3,"]),
        overrides={"settings.csv": _settings_no_randomize()},
    )

    def _check(self, events, out):
        lec = [e for e in events if e["id"].startswith("COMP5500") and not e["isLab"]]
        self.assertTrue(lec, msg=out[-2000:])
        for e in lec:
            self.assertTrue(e["unplaced"], msg=f"expected UNPLACED: {e}")
            self.assertNotEqual(e["start"], "18:00", msg=f"undergrad placeholder in grad slot: {e}")
        self.assertIn("180-minute", out)

    def test_greedy(self):
        run = self.run_scheduler(**self.FIX)
        self.assertNotIn("Traceback", run.stdout)
        self._check(run.events, run.stdout)

    @unittest.skipUnless(_has_ortools(), "ortools not installed")
    def test_cpsat(self):
        run = self.run_scheduler(**self.FIX)
        shutil.copy2(REPO / "main_cpsat.py", run.workdir / "main_cpsat.py")
        proc = subprocess.run([sys.executable, "main_cpsat.py"], cwd=run.workdir, timeout=600,
                              capture_output=True, text=True)
        out = proc.stdout + proc.stderr
        self.assertNotIn("Traceback", out, msg=out[-2000:])
        self.assertEqual(proc.returncode, 0, msg=out[-2000:])
        self._check(json.loads((run.workdir / "schedule_cpsat.json").read_text()), out)


if __name__ == "__main__":
    unittest.main()
