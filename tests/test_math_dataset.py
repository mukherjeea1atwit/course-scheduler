"""The maths department's real files, if they are present.

math-test-data/ is not committed (it is a user's real data), so these skip when
it is absent. When it is there they are the highest-value tests in the suite:
that data set is the only one that exercises 3-day courses, 4-day courses,
cross-listed course numbers, and every cross-file mismatch we have seen.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MATH = REPO / "math-test-data"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import _parse, split_days  # noqa: E402


def _find(*names):
    for n in names:
        hits = sorted(MATH.glob(n))
        if hits:
            return hits[0]
    return None


@unittest.skipUnless(MATH.is_dir(), "math-test-data/ not present")
class MathDataset(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        courses = _find("course-list*.csv")
        prefs = _find("prof_preferences*.csv")
        loads = _find("faculty_load*.csv")
        grp = _find("non_overlap_groups*.csv")
        if not (courses and prefs and loads):
            raise unittest.SkipTest("math-test-data/ is missing expected files")

        cls.work = Path(tempfile.mkdtemp(prefix="sched-math-"))
        shutil.copy2(REPO / "main.py", cls.work / "main.py")
        shutil.copytree(REPO / "data", cls.work / "data")
        d = cls.work / "data"
        shutil.copy2(courses, d / "course-list-Spring 27(Sheet1) (1).csv")
        shutil.copy2(prefs, d / "prof_preferences.csv")
        shutil.copy2(loads, d / "faculty_load.csv")
        if grp:
            shutil.copy2(grp, d / "non_overlap_groups.csv")

        proc = subprocess.run([sys.executable, "main.py"], cwd=cls.work,
                              capture_output=True, text=True, timeout=180)
        cls.result = _parse(proc, cls.work)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "work", "/nonexistent"), ignore_errors=True)

    def test_it_runs_without_crashing(self):
        self.assertNotIn("Traceback", self.result.stdout, msg=self.result.stdout[-2000:])
        self.assertEqual(self.result.returncode, 0)

    def test_every_constraint_passes(self):
        self.assertEqual(self.result.failed_constraints(), [],
                         msg=self.result.stdout[-3000:])

    def test_every_lecture_meets_its_declared_days_per_week(self):
        """The headline requirement: 'they need to be 3 days'."""
        import csv
        declared = {}
        with open(self.work / "data" / "course-list-Spring 27(Sheet1) (1).csv",
                  newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                num = (r.get("Course number") or "").strip()
                if num:
                    declared[num.replace(" ", "")] = int(r["lecture days per week"] or 0)

        checked = 0
        for row in self.result.rows:
            if row["Type"] != "LEC":
                continue
            course = row["Course Designation/Number"].replace(" ", "")
            want = declared.get(course)
            if not want or course.upper().startswith(("MATH5", "MATH6", "MATH7")):
                continue          # grad is one evening by rule, covered elsewhere
            checked += 1
            self.assertEqual(len(split_days(row["Days"])), want,
                             msg=f"{course} meets {row['Days']}, declared {want}")
        self.assertGreater(checked, 30, "expected to check most of the term")

    def test_three_day_courses_use_more_than_one_pattern(self):
        patterns = {r["Days"] for r in self.result.rows
                    if r["Type"] == "LEC" and len(split_days(r["Days"])) == 3}
        self.assertGreater(len(patterns), 3, msg=f"only {patterns}")

    def test_known_data_mismatches_are_all_reported(self):
        """Every mismatch that silently stranded 39 sections must be named."""
        out = self.result.stdout
        for token in ("MATH1876/77", "did you mean", "CANNOT be assigned", "Youseff"):
            self.assertIn(token, out, msg=token)

    def test_no_phantom_faculty_are_assigned(self):
        """Names present only in prof_preferences.csv must teach nothing."""
        assigned = {r["Faculty"] for r in self.result.rows}
        for ghost in ("Abdullah", "Salem", "Youssef", "(Lauren)"):
            self.assertNotIn(ghost, assigned)


if __name__ == "__main__":
    unittest.main()
