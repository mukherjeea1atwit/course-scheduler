"""End-to-end scheduling behaviour. One test per defect we have actually hit."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (SchedulerTestCase, course_list, preferences, faculty_load,
                     split_days, duration_min)


class DaysPerWeek(SchedulerTestCase):
    """The headline bug: 'lecture days per week' was read and then ignored."""

    def test_three_day_course_meets_three_days(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,4,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertAllConstraintsPass(run)
        days = run.days_of("MATH1500")
        self.assertEqual(len(days), 4)
        for d in days:
            self.assertEqual(len(split_days(d)), 3, msg=f"{d} is not 3 days")

    def test_four_day_course_meets_four_days(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1525,Foundations of Calculus,4,5,0,2,"]),
            prefs=preferences(['MATH1525,Foundations of Calculus,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertAllConstraintsPass(run)
        for d in run.days_of("MATH1525"):
            self.assertEqual(len(split_days(d)), 4, msg=f"{d} is not 4 days")

    def test_two_day_comp_course_is_unchanged(self):
        run = self.run_scheduler(
            courses=course_list(["COMP1000,Computer Science 1,2,3,2,2,"]),
            prefs=preferences(['COMP1000,Computer Science 1,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertAllConstraintsPass(run, allow=("C16",))  # 2 sections: balance is noise
        for d in run.days_of("COMP1000"):
            self.assertIn(d, ("MW", "TTh", "WF"))

    def test_three_day_patterns_are_not_all_the_same(self):
        """Any 3 days is acceptable; they should not all pile onto one pattern."""
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,8,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Bob, Cara, Dan"']),
            loads=faculty_load(["Ann,3,", "Bob,3,", "Cara,3,", "Dan,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertGreater(len(set(run.days_of("MATH1500"))), 1)


class MeetingLength(SchedulerTestCase):
    def test_three_day_math_gets_seventy_minute_meetings(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,1,"]),
            prefs=preferences(["MATH1500,Precalculus,Ann"]),
            loads=faculty_load(["Ann,3,"]),
        )
        self.assertAllConstraintsPass(run, allow=("C16",))  # 1 section: balance is noise
        self.assertEqual(duration_min(run.rows[0]["Times"]), 70)

    def test_two_day_math_gets_one_hundred_and_five_minute_meetings(self):
        run = self.run_scheduler(
            courses=course_list(["MATH2300,Discrete Mathematics,2,4,0,1,"]),
            prefs=preferences(["MATH2300,Discrete Mathematics,Ann"]),
            loads=faculty_load(["Ann,3,"]),
        )
        self.assertAllConstraintsPass(run, allow=("C16",))  # 1 section: balance is noise
        self.assertEqual(duration_min(run.rows[0]["Times"]), 105)


class GradCourses(SchedulerTestCase):
    def test_grad_declared_three_days_still_meets_one_evening(self):
        run = self.run_scheduler(
            courses=course_list(["MATH5100,Statistical Thinking,3,4,0,2,"]),
            prefs=preferences(['MATH5100,Statistical Thinking,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertIn("graduate courses meet one", run.stdout)
        for d in run.days_of("MATH5100"):
            self.assertEqual(len(split_days(d)), 1, msg=f"{d} is not a single evening")

    def test_no_professor_gets_more_than_two_grad_sections_via_topup(self):
        """The preferred-first top-up pass did not enforce C18."""
        run = self.run_scheduler(
            courses=course_list([
                "MATH5000,Design I,1,4,0,3,",
                "MATH5100,Statistical Thinking,1,4,0,3,",
            ]),
            prefs=preferences([
                'MATH5000,Design I,"Ann, Bob"',
                'MATH5100,Statistical Thinking,"Ann, Bob"',
            ]),
            loads=faculty_load(["Ann,4,", "Bob,4,"]),
        )
        self.assertRanCleanly(run)
        self.assertTrue(run.constraints.get("C18"), msg=run.stdout[-3000:])
        for fac in ("Ann", "Bob"):
            n = sum(1 for r in run.rows if r["Faculty"] == fac)
            self.assertLessEqual(n, 2, msg=f"{fac} has {n} grad sections")


class FacultyRoster(SchedulerTestCase):
    def test_a_name_only_in_preferences_is_never_assigned(self):
        """faculty_load.csv is the roster. A preferences-only name used to be
        given an invented load of 3 — that is how CS staff ended up teaching
        maths courses, and how a misspelling taught real sections."""
        run = self.run_scheduler(
            courses=course_list(["MATH1900,Operations Research,3,4,0,1,"]),
            prefs=preferences(['MATH1900,Operations Research,"Ghost, Ann"']),
            loads=faculty_load(["Ann,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertNotIn("Ghost", run.assigned_faculty())
        self.assertIn("CANNOT be assigned", run.stdout)

    def test_a_zero_load_professor_is_never_assigned(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,1,"]),
            prefs=preferences(['MATH1500,Precalculus,"Idle, Ann"']),
            loads=faculty_load(["Idle,0,", "Ann,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertNotIn("Idle", run.assigned_faculty())

    def test_contended_course_is_staffed_before_an_easy_one(self):
        """CYBR2500: a course with one eligible professor must be placed before a
        course that has several, or its only candidate is already full."""
        run = self.run_scheduler(
            courses=course_list([
                "MATH4550,Scarce,3,4,0,2,",      # only Ann can teach it
                "MATH1500,Plentiful,3,4,0,6,",   # Ann, Bob, Cara can
            ]),
            prefs=preferences([
                "MATH4550,Scarce,Ann",
                'MATH1500,Plentiful,"Ann, Bob, Cara"',
            ]),
            loads=faculty_load(["Ann,3,", "Bob,3,", "Cara,3,"]),
        )
        self.assertRanCleanly(run)
        self.assertNotIn("TBA", run.faculty_of("MATH4550"))


class UnplacedSections(SchedulerTestCase):
    def _unplaceable(self):
        # 1 day/week with 4 lecture hours = one 240-minute meeting; no such slot.
        return self.run_scheduler(
            courses=course_list(["MATH2100B,Probability,1,4,0,1,"]),
            prefs=preferences(["MATH2100B,Probability,Ann"]),
            loads=faculty_load(["Ann,3,"]),
        )

    def test_it_is_reported_not_silently_placed(self):
        run = self._unplaceable()
        self.assertRanCleanly(run)
        self.assertIn("MATH2100B-1", run.unplaced)
        self.assertIn("COULD NOT BE PLACED", run.stdout.upper())

    def test_it_is_flagged_in_the_json_the_ui_reads(self):
        run = self._unplaceable()
        self.assertTrue(any(e.get("unplaced") for e in run.events), msg=run.events)

    def test_it_is_flagged_in_both_csv_exports(self):
        """The Banner CSV is the file people import; an unplaced section must not
        look like a normal row there."""
        run = self._unplaceable()
        self.assertTrue(any(r.get("Status") == "UNPLACED" for r in run.rows))
        self.assertTrue(any(r.get("Status") == "UNPLACED" for r in run.banner))

    def test_no_placeholder_room_name_leaks_out(self):
        run = self._unplaceable()
        self.assertNotIn("FORCE_ASSIGN_ROOM", run.stdout)


class ConcurrencyCap(SchedulerTestCase):
    def test_forced_sections_spread_across_patterns_instead_of_stacking(self):
        """Regression: once no slot had room, every forced section landed on the
        first day pattern — 30 on Monday instead of spread over four evenings."""
        run = self.run_scheduler(
            courses=course_list(["MATH5100,Statistical Thinking,1,4,0,60,"]),
            prefs=preferences(["MATH5100,Statistical Thinking,Ann"]),
            loads=faculty_load(["Ann,3,"]),
        )
        self.assertRanCleanly(run)
        counts = {}
        for e in run.events:
            counts[e["day"]] = counts.get(e["day"], 0) + 1
        self.assertGreaterEqual(len(counts), 4, msg=f"only used {counts}")
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 2,
                             msg=f"uneven spread: {counts}")

    def test_the_cap_is_read_from_settings(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,4,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Bob"']),
            loads=faculty_load(["Ann,3,", "Bob,3,"]),
            overrides={"settings.csv": "setting,value\nmax_concurrent_sections,3\n"},
        )
        self.assertRanCleanly(run)
        self.assertIn("C11 ≤ 3 concurrent", run.stdout)


class SameCourseSameTime(SchedulerTestCase):
    """C14: at most 2 sections of one course may share a time.

    Three sections of a course at the same hour on the same days means a student
    with a clash has no third option. This was validated after the run but never
    enforced during it, so the checker could only ever report the problem.

    The fixture deliberately starves the timetable — one course, many sections,
    only two start times — so the collision is forced rather than incidental.
    """

    NARROW_TIMINGS = (
        "start_time,stop_time,duration_min,slot_label,evening,Days Allowed\n"
        '08:00:00,09:10:00,70,lec_70,FALSE,"M,T,W,Th,F"\n'
        '10:00:00,11:10:00,70,lec_70,FALSE,"M,T,W,Th,F"\n'
    )

    def test_no_more_than_two_sections_share_a_time(self):
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,3,4,0,9,"]),
            prefs=preferences(['MATH1500,Precalculus,"Ann, Bob, Cara"']),
            loads=faculty_load(["Ann,3,", "Bob,3,", "Cara,3,"]),
            overrides={"timings.csv": self.NARROW_TIMINGS},
        )
        self.assertRanCleanly(run)

        # Assert on the schedule itself, not just the checker's verdict — the
        # point is that the scheduler avoided it, not that it noticed afterwards.
        seen = {}
        for r in run.lectures("MATH1500"):
            key = (r["Days"], r["Times"])
            seen[key] = seen.get(key, 0) + 1
        worst = max(seen.values())
        self.assertLessEqual(worst, 2, msg=f"{worst} sections share a slot: {seen}")
        self.assertTrue(run.constraints.get("C14"), msg=run.stdout[-2000:])


class TeachingDays(SchedulerTestCase):
    def test_a_five_day_course_does_not_fail_the_four_day_rule(self):
        """The scheduler allowed it and the checker failed it unconditionally."""
        run = self.run_scheduler(
            courses=course_list(["MATH1500,Precalculus,5,4,0,1,"]),
            prefs=preferences(["MATH1500,Precalculus,Ann"]),
            loads=faculty_load(["Ann,3,"]),
            overrides={"meeting_patterns.csv":
                       "subject,lecture_days_per_week,lecture_hours,meeting_minutes,lab_minutes\n"
                       "*,5,,70,0\n"},
        )
        self.assertRanCleanly(run)
        self.assertTrue(run.constraints.get("C4"), msg=run.stdout[-2000:])
        self.assertEqual(len(split_days(run.days_of("MATH1500")[0])), 5)


if __name__ == "__main__":
    unittest.main()
