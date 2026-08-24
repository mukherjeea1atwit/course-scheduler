"""The INPUT CHECK report, and the two CSV exports."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import SchedulerTestCase, course_list, preferences, faculty_load


class InputCheck(SchedulerTestCase):
    """Cross-file mismatches must be named before the run, not hidden as TBA.

    On the first real maths data set, four typos stranded 39 of 70 sections and
    the only symptom was a TBA count that looked like a capacity problem.
    """

    def test_course_number_typo_is_reported_with_a_suggestion(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1876/77,Calculus 2,3,4,0,4,"]),
            prefs=preferences(['MATH1876/7,Calculus 2,"Ann, Bob"']),   # missing a 7
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertIn("no faculty listed", run.stdout)
        self.assertIn("MATH1876/77", run.stdout)
        self.assertIn("did you mean", run.stdout)

    def test_faculty_name_typo_is_reported_with_a_suggestion(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,1,"]),
            prefs=preferences(["MATH1500,Precalculus,Youssef"]),
            loads=faculty_load(["Youseff,3,"]),                        # one s, two f
        )
        self.assertRanCleanly(run)
        self.assertIn("CANNOT be assigned", run.stdout)
        self.assertIn("did you mean Youseff", run.stdout)

    def test_zero_load_professor_is_called_out(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,1,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Weijie"']),
            loads=faculty_load(["Ann,3,", "Weijie,0,"]),
        )
        self.assertRanCleanly(run)
        self.assertIn("load of 0", run.stdout)
        self.assertIn("Weijie", run.stdout)

    def test_over_subscription_is_called_out(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,10,"]),
            prefs=preferences(["MATH1500,Precalculus,Ann"]),
            loads=faculty_load(["Ann,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertIn("must be TBA", run.stdout)

    def test_clean_inputs_say_so(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,2,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertIn("line up across all input files", run.stdout)


class Exports(SchedulerTestCase):
    def _run(self):
        return self.run_scheduler(
            courses=course_list([
                "MATH1876/77,Calculus 2A/2B,3,4,0,2,",
                "COMP1000,Computer Science 1,2,3,2,1,",
            ]),
            prefs=preferences([
                'MATH1876/77,Calculus 2A/2B,"Ann, Bob"',
                'COMP1000,Computer Science 1,"Ann, Bob"',
            ]),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )

    def test_simple_csv_has_the_agreed_columns_in_order(self):
        run = self._run()
        self.assertRanCleanly(run)
        self.assertEqual(list(run.rows[0].keys()),
                         ["Course Designation/Number", "Type", "Days", "Times",
                          "Faculty", "Section", "Room", "Status"])

    def test_cross_listed_course_number_survives_the_banner_export(self):
        """Regression: only the first run of digits was kept, so every
        MATH1876/77 section exported as plain '1876'."""
        run = self._run()
        crse = {r["Crse"] for r in run.banner if r["Subj"] == "MATH"}
        self.assertEqual(crse, {"1876/77"})

    def test_lab_rows_are_labelled_in_both_exports(self):
        run = self._run()
        self.assertIn("Lab", {r["Type"] for r in run.rows})
        self.assertTrue(any(r["Section"].endswith("L") for r in run.banner))

    def test_section_numbers_are_present(self):
        run = self._run()
        secs = {r["Section"] for r in run.rows
                if r["Course Designation/Number"] == "MATH1876/77"}
        self.assertEqual(secs, {"1", "2"})


if __name__ == "__main__":
    unittest.main()
