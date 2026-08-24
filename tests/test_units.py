"""Unit tests for the pure helpers in main.py.

These import main.py directly. main.py mutates module-level globals in
load_settings(), so anything that touches settings restores them afterwards.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402


class DayPatterns(unittest.TestCase):
    def test_two_day_keeps_the_canonical_institutional_patterns(self):
        self.assertEqual(main.build_day_patterns(2), [["M", "W"], ["T", "Th"], ["W", "F"]])

    def test_one_day_includes_friday(self):
        # Regression: this returned the grad evening list (M/T/W/Th), which
        # silently made a Friday-only daytime course unrepresentable.
        self.assertEqual(main.build_day_patterns(1), [["M"], ["T"], ["W"], ["Th"], ["F"]])

    def test_three_day_generates_every_combination_best_spread_first(self):
        p = main.build_day_patterns(3)
        self.assertEqual(len(p), 10)                       # 5 choose 3
        self.assertEqual(p[0], ["M", "W", "F"])            # no back-to-back days
        for want in (["M", "T", "Th"], ["T", "Th", "F"], ["T", "W", "F"]):
            self.assertIn(want, p)

    def test_four_day_generates_every_combination(self):
        p = main.build_day_patterns(4)
        self.assertEqual(len(p), 5)                        # 5 choose 4
        self.assertTrue(all(len(x) == 4 for x in p))

    def test_spread_score_prefers_fewer_adjacent_days(self):
        self.assertLess(main._spread_score(["M", "W", "F"]),
                        main._spread_score(["M", "T", "Th"]))


class LectureDays(unittest.TestCase):
    def setUp(self):
        main._DAYS_WARNED.clear()

    def test_honors_the_course_list(self):
        self.assertEqual(main.lecture_days_for("MATH1500", 3), 3)
        self.assertEqual(main.lecture_days_for("MATH1525", 4), 4)

    def test_grad_is_always_one_evening(self):
        # Regression: a grad course declared 3 days produced three 155-minute
        # sessions (7h45/week) and every constraint check still passed.
        self.assertEqual(main.lecture_days_for("MATH5100", 3), 1)
        self.assertEqual(main.lecture_days_for("COMP7800", 2), 1)

    def test_blank_and_garbage_fall_back_to_the_default(self):
        self.assertEqual(main.lecture_days_for("MATH1000", 0), main.DEFAULT_LECTURE_DAYS)
        self.assertEqual(main.lecture_days_for("MATH1000", ""), main.DEFAULT_LECTURE_DAYS)
        self.assertEqual(main.lecture_days_for("MATH1000", "abc"), main.DEFAULT_LECTURE_DAYS)
        self.assertEqual(main.lecture_days_for("MATH1000", -2), main.DEFAULT_LECTURE_DAYS)

    def test_clamped_to_the_length_of_the_week(self):
        self.assertEqual(main.lecture_days_for("MATH1000", 99), len(main.ALL_DAYS))


class SectionIdParsing(unittest.TestCase):
    def test_plain_and_lab_ids(self):
        self.assertEqual(main.split_section_id("COMP1050-3"), ("COMP1050", "3", False))
        self.assertEqual(main.split_section_id("COMP1050-3-LAB"), ("COMP1050", "3", True))

    def test_cross_listed_course_number_survives(self):
        self.assertEqual(main.split_section_id("MATH1876/77-18"), ("MATH1876/77", "18", False))

    def test_hyphenated_course_numbers(self):
        # Regression: sid.split("-")[1] returned "1500" as the section number.
        self.assertEqual(main.split_section_id("MATH-1500-1"), ("MATH-1500", "1", False))
        self.assertEqual(main.split_section_id("MATH-1500-1-LAB"), ("MATH-1500", "1", True))

    def test_id_without_a_section_number(self):
        self.assertEqual(main.split_section_id("COMP1000"), ("COMP1000", "", False))


class SubjectParsing(unittest.TestCase):
    def test_subject_of(self):
        self.assertEqual(main.subject_of("MATH1500"), "MATH")
        self.assertEqual(main.subject_of("COMP1050"), "COMP")
        self.assertEqual(main.subject_of("1776/77"), "")


class MeetingRules(unittest.TestCase):
    def setUp(self):
        main.MEETING_RULES = list(main.DEFAULT_MEETING_RULES)

    def test_subject_rule_beats_an_hours_rule_for_the_same_day_count(self):
        # Both "MATH, 2 days" and "any, 2 days, 4 hours" match MATH1030A.
        # The subject-specific one must win, or math gets COMP's 80 minutes.
        self.assertEqual(main.meeting_minutes("MATH1030A", 4, 2), 105)

    def test_comp_two_day_offerings_are_unchanged(self):
        self.assertEqual(main.meeting_minutes("COMP1000", 3, 2), 90)
        self.assertEqual(main.meeting_minutes("COMP2540", 4, 2), 80)

    def test_three_and_four_day_courses_use_the_seventy_minute_slot(self):
        self.assertEqual(main.meeting_minutes("MATH1500", 4, 3), 70)
        self.assertEqual(main.meeting_minutes("MATH1525", 5, 4), 70)

    def test_grad_ignores_the_table(self):
        self.assertEqual(main.meeting_minutes("COMP5500", 3, 1), main.GRAD_MEETING_MIN)

    def test_lab_minutes_follow_the_matching_rule(self):
        self.assertEqual(main.lab_minutes("COMP1000", 3, 2, 2), 105)
        self.assertEqual(main.lab_minutes("MATH1500", 4, 0, 3), 0)

    def test_unmatched_combination_falls_back_to_dividing_contact_hours(self):
        self.assertEqual(main.meeting_minutes("MATH2100B", 4, 1), 240)


class TeachingDayAllowance(unittest.TestCase):
    def test_default_ceiling(self):
        self.assertEqual(main.teaching_day_allowance(["M", "W"]), main.MAX_TEACHING_DAYS)

    def test_a_course_longer_than_the_ceiling_raises_the_floor(self):
        # Otherwise a 5-day course is unschedulable for anybody.
        self.assertEqual(main.teaching_day_allowance(["M", "T", "W", "Th", "F"]), 5)


class Settings(unittest.TestCase):
    """load_settings mutates module globals; each test restores them."""

    def setUp(self):
        self._saved = (main.MAX_CONCURRENT, main.MAX_DAY_SPAN_HR, main.FACULTY_GAP_MIN,
                       main.MAX_TEACHING_DAYS, main.DEFAULT_FACULTY_LOAD, main.AM_TARGET_RATIO)

    def tearDown(self):
        (main.MAX_CONCURRENT, main.MAX_DAY_SPAN_HR, main.FACULTY_GAP_MIN,
         main.MAX_TEACHING_DAYS, main.DEFAULT_FACULTY_LOAD,
         main.AM_TARGET_RATIO) = self._saved

    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.csv")
            with open(p, "w") as f:
                f.write(text)
            return main.load_settings(p)

    def test_values_are_applied(self):
        self._load("setting,value\nmax_concurrent_sections,25\n")
        self.assertEqual(main.MAX_CONCURRENT, 25)

    def test_every_load_resets_to_defaults_first(self):
        # Regression: a bad value fell back to the *previous run's* value, so in
        # the long-running server a typo left the old number in force forever.
        self._load("setting,value\nmax_concurrent_sections,25\n")
        self._load("setting,value\nmax_concurrent_sections,banana\n")
        self.assertEqual(main.MAX_CONCURRENT,
                         main.SETTING_DEFAULTS["max_concurrent_sections"])

    def test_removing_a_setting_reverts_it(self):
        self._load("setting,value\nmax_concurrent_sections,25\n")
        self._load("setting,value\n")
        self.assertEqual(main.MAX_CONCURRENT,
                         main.SETTING_DEFAULTS["max_concurrent_sections"])

    def test_out_of_range_is_rejected(self):
        self._load("setting,value\nam_target_ratio,7\nmax_daily_span_hours,-3\n")
        self.assertEqual(main.AM_TARGET_RATIO, main.SETTING_DEFAULTS["am_target_ratio"])
        self.assertEqual(main.MAX_DAY_SPAN_HR, main.SETTING_DEFAULTS["max_daily_span_hours"])

    def test_missing_file_uses_defaults(self):
        main.load_settings("/nonexistent/settings.csv")
        self.assertEqual(main.MAX_CONCURRENT,
                         main.SETTING_DEFAULTS["max_concurrent_sections"])


if __name__ == "__main__":
    unittest.main()
