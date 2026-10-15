"""Guards on what is actually in the repo: the shipped data files and the UI.

These catch the class of mistake that only shows up on the user's machine — a
data file edited into an invalid state, or JS wired to a name that no longer
exists.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _parse  # noqa: E402


class ShippedDataRuns(unittest.TestCase):
    """The default data set in data/ must schedule cleanly.

    Run in a copy so the schedule the user is looking at is never overwritten.
    """

    @classmethod
    def setUpClass(cls):
        work = Path(tempfile.mkdtemp(prefix="sched-shipped-"))
        shutil.copy2(REPO / "main.py", work / "main.py")
        shutil.copytree(REPO / "data", work / "data")
        proc = subprocess.run([sys.executable, "main.py"], cwd=work,
                              capture_output=True, text=True, timeout=180)
        cls.result = _parse(proc, work)
        cls.work = work

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_it_completes_without_crashing(self):
        self.assertNotIn("Traceback", self.result.stdout,
                         msg=self.result.stdout[-2000:])
        self.assertEqual(self.result.returncode, 0, msg=self.result.stdout[-2000:])

    def test_every_constraint_passes(self):
        self.assertTrue(self.result.constraints, msg="no constraint report produced")
        self.assertEqual(self.result.failed_constraints(), [],
                         msg=self.result.stdout[-3000:])

    # Known, expected gap — remove once COMP5500 is restructured. COMP5500 is
    # senior project, which is undergraduate (5500 is the exception to "5000+
    # is grad"), so it no longer gets the 155-min grad evening. Its shipped
    # course-list row (1 day/week, 3 lecture hours, no lab) then needs a
    # 180-minute daytime meeting, which no timings.csv slot offers. Its real
    # structure (1-hr lecture + three 2-hr labs) is being implemented separately.
    KNOWN_UNPLACEABLE = {"COMP5500"}

    def test_nothing_is_unplaced(self):
        unexpected = [sid for sid in self.result.unplaced
                      if sid.rsplit("-", 1)[0] not in self.KNOWN_UNPLACEABLE]
        self.assertEqual(unexpected, [], msg=self.result.stdout[-2000:])

    def test_known_unplaceable_sections_fail_only_for_the_slot_length(self):
        for sid in self.result.unplaced:
            if sid.rsplit("-", 1)[0] in self.KNOWN_UNPLACEABLE:
                self.assertRegex(self.result.stdout,
                                 rf"\[CRITICAL\] {re.escape(sid)} .*needs a 180-minute meeting",
                                 msg=self.result.stdout[-2000:])

    def test_all_three_output_files_are_written(self):
        for name in ("schedule.json", "schedule.csv", "schedule_simple.csv"):
            self.assertTrue((self.work / name).exists(), msg=name)


class ShippedDataFiles(unittest.TestCase):
    def test_settings_names_are_all_recognised(self):
        """Every shipped setting must be acted on by one of the two engines.

        settings.csv is a single file shared by both, so a name only the CP-SAT
        engine knows is legitimate. It still has to be known SOMEWHERE — the
        point of this test is to catch a typo shipped in the default data, and a
        typo looks exactly like a key neither engine reads.
        """
        import csv
        import main
        import main_cpsat
        with open(REPO / "data" / "settings.csv", newline="", encoding="utf-8-sig") as f:
            names = [r["setting"].strip() for r in csv.DictReader(f) if r.get("setting")]
        self.assertTrue(names)
        known = set(main.SETTING_DEFAULTS) | set(main_cpsat.SETTING_DEFAULTS)
        unknown = [n for n in names if n not in known]
        self.assertEqual(unknown, [], msg=f"unknown settings shipped: {unknown}")

    def test_foreign_settings_are_real_cpsat_settings(self):
        """main.FOREIGN_SETTINGS suppresses the greedy engine's unknown-setting
        warning. If a name in it is not actually a CP-SAT setting, it is
        suppressing a warning about a genuine typo — the exact thing the warning
        exists to catch."""
        import main
        import main_cpsat
        bogus = sorted(set(main.FOREIGN_SETTINGS) - set(main_cpsat.SETTING_DEFAULTS))
        self.assertEqual(bogus, [], msg=f"not CP-SAT settings: {bogus}")

    def test_engines_do_not_both_claim_the_same_setting_differently(self):
        """A key both engines define must mean the same thing in both, or the
        two schedulers silently disagree about the user's own settings file."""
        import main
        import main_cpsat
        shared = set(main.SETTING_DEFAULTS) & set(main_cpsat.SETTING_DEFAULTS)
        mismatched = sorted(k for k in shared
                            if main.SETTING_DEFAULTS[k] != main_cpsat.SETTING_DEFAULTS[k])
        self.assertEqual(mismatched, [], msg=f"defaults differ: {mismatched}")

    def test_meeting_patterns_cover_two_three_and_four_day_courses(self):
        import main
        rules = main.load_meeting_patterns(str(REPO / "data" / "meeting_patterns.csv"))
        covered = {r.days_per_week for r in rules}
        for n in (2, 3, 4):
            self.assertIn(n, covered, msg=f"no rule for {n}-day courses")

    def test_timings_offers_every_duration_the_rules_ask_for(self):
        """A meeting length with no matching slot means those courses can never
        be placed — the MATH1525 failure in a different disguise."""
        import main
        rules = main.load_meeting_patterns(str(REPO / "data" / "meeting_patterns.csv"))
        slots = main.load_timeslots(str(REPO / "data" / "timings.csv"))
        available = {s.duration_min for s in slots}
        for r in rules:
            self.assertIn(r.meeting_minutes, available,
                          msg=f"meeting_patterns wants {r.meeting_minutes} min; "
                              f"timings.csv offers {sorted(available)}")
            if r.lab_minutes:
                self.assertIn(r.lab_minutes, available,
                              msg=f"no {r.lab_minutes}-minute lab slot")

    def test_the_grad_evening_slot_excludes_friday(self):
        """GRAD_SINGLE_DAY_PATTERNS omits Friday because no Friday grad evening
        exists. If a Friday grad slot were ever added, grad courses could never
        use it and nothing would say why.

        Note this is only about the grad (18:00) session — the 17:15 daytime
        slots deliberately do run on Friday.
        """
        import main
        grad = [s for s in main.load_timeslots(str(REPO / "data" / "timings.csv"))
                if s.start.hour >= main.GRAD_START_HR]
        self.assertTrue(grad, "no grad evening slot is defined")
        for s in grad:
            self.assertNotIn("F", s.days_allowed or [],
                             msg=f"{s.label} {s.start} allows Friday")

    def test_the_seventy_minute_evening_slot_excludes_friday(self):
        """Mike asked for 70-minute three-day courses to fall back to M,W,Th at
        5:15 rather than M,W,F. That is expressed purely as data."""
        import main
        for s in main.load_timeslots(str(REPO / "data" / "timings.csv")):
            if s.duration_min == 70 and s.start.hour >= 17:
                self.assertNotIn("F", s.days_allowed or [],
                                 msg=f"{s.label} {s.start} allows Friday")


class UiWiring(unittest.TestCase):
    """Cheap static checks on index.html — no browser needed."""

    @classmethod
    def setUpClass(cls):
        cls.html = (REPO / "index.html").read_text(encoding="utf-8")

    def test_no_blocking_alert_dialogs(self):
        """A JS alert() freezes the page and blocks everything after it."""
        self.assertNotIn("alert(", self.html)

    def test_course_view_is_wired_end_to_end(self):
        for token in ('data-tab="course"', 'id="panel-course"', 'id="courseTableBody"',
                      "function renderCourse", "courseSearch"):
            self.assertIn(token, self.html, msg=token)

    def test_concurrency_cap_is_read_from_settings_not_hardcoded(self):
        self.assertIn("loadTracksSetting", self.html)
        self.assertIn("max_concurrent_sections", self.html)
        self.assertNotIn("const TRACKS_DAY = 10", self.html)

    def test_grid_can_scroll_horizontally(self):
        self.assertIn("applyGridWidth", self.html)
        self.assertIn("overflow-x: auto", self.html)

    def test_unplaced_sections_are_styled_and_listed(self):
        for token in ("isUnplaced", "buildUnplacedChip", 'data-unplaced'):
            self.assertIn(token, self.html, msg=token)

    def test_faculty_dropdown_uses_the_roster(self):
        self.assertIn("loadFacultyRoster", self.html)
        self.assertIn("(no sections)", self.html)

    def test_csv_download_button_is_wired(self):
        self.assertIn('id="downloadBtn"', self.html)
        self.assertIn("downloadScheduleCsv", self.html)

    def test_inputs_page_exposes_the_new_tables(self):
        inputs = (REPO / "web" / "inputs.html").read_text(encoding="utf-8")
        self.assertIn("'settings'", inputs)
        self.assertIn("'meeting_patterns'", inputs)


if __name__ == "__main__":
    unittest.main()
