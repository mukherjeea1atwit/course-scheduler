"""The greedy top-up must never staff an UNPLACED placeholder section.

A section the scheduler could not place is a placeholder: invented time, room
"UNPLACED", faculty TBA. The underload top-up (preferred-first pass and the
foundation exception) used to treat it like any other leftover TBA section and
hand it to a real professor. That professor's load was then spent on a class
that is not on the timetable, and a section that really was scheduled could no
longer reach them. CP-SAT already excludes placeholders; this pins greedy.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (  # noqa: E402
    SchedulerTestCase, course_list, faculty_load, preferences,
)


class TopupSkipsPlaceholders(SchedulerTestCase):
    """Alice (load 1) is the only name for COMP3999, which meets 1 day x 3 h —
    a 180-minute meeting no timings.csv slot offers, so it is UNPLACED. COMP1000
    has nobody listed, so it is placed with a TBA professor and is exactly what
    the foundation top-up is for. Alice's one unit of load must go to the real
    COMP1000 section, not to the COMP3999 placeholder."""

    @classmethod
    def setUpClass(cls):
        cls.run_ = cls().run_scheduler(
            courses=course_list([
                "COMP3999,Unplaceable,1,3,0,1,",
                "COMP1000,Computer Science I,2,4,0,1,",
            ]),
            prefs=preferences(["COMP3999,Unplaceable,Alice"]),
            loads=faculty_load(["Alice,1,"]),
        )

    def test_unplaceable_section_is_reported_and_stays_tba(self):
        r = self.run_
        self.assertNotIn("Traceback", r.stdout, msg=r.stdout[-2000:])
        self.assertIn("COMP3999-1", r.unplaced, msg=r.stdout[-2000:])
        self.assertEqual(r.faculty_of("COMP3999"), ["TBA"], msg=r.stdout[-2000:])
        self.assertNotRegex(r.stdout, r"\[TOPUP\] COMP3999-1 ")

    def test_professor_load_goes_to_a_real_section(self):
        r = self.run_
        self.assertEqual(r.faculty_of("COMP1000"), ["Alice"], msg=r.stdout[-3000:])
        for e in r.events:
            if e["id"].startswith("COMP1000-"):
                self.assertNotEqual(e.get("room"), "UNPLACED", msg=e)

    def test_hard_constraints_pass(self):
        self.assertAllConstraintsPass(self.run_, allow=("C16",))  # 2 sections cannot balance 5 days


if __name__ == "__main__":
    import unittest
    unittest.main()


# ── sole-teacher protection in the greedy main loop ─────────────────────────
# Upper-level undergraduate sections are placed before graduate ones, so a
# professor who is the ONLY name for a graduate course could have their load (or
# their evenings) spent on an undergraduate course someone else could teach.
# That is how CYBR6520 (only Abdullah) went TBA on the shipped data while Leon,
# also qualified for CYBR3590, sat under load. Such professors are now tried
# last for other courses while their sole-duty sections are still pending.

class _SoleTeacher:
    LOAD_ABBY = "1"

    @classmethod
    def setUpClass(cls):
        cls.run_ = cls().run_scheduler(
            courses=course_list([
                "COMP3100,Shared Course,2,3,0,1,",
                "COMP6100,Abby Only,1,3,0,1,",
            ]),
            prefs=preferences([
                'COMP3100,Shared Course,"Abby, Bob"',     # Abby first: wins ties
                "COMP6100,Abby Only,Abby",
            ]),
            loads=faculty_load([f"Abby,{cls.LOAD_ABBY},", "Bob,1,"]),
        )

    def test_sole_course_keeps_its_professor(self):
        r = self.run_
        self.assertEqual(r.faculty_of("COMP6100"), ["Abby"], msg=r.stdout[-3000:])
        self.assertEqual(r.faculty_of("COMP3100"), ["Bob"], msg=r.stdout[-3000:])

    def test_hard_constraints_pass(self):
        self.assertAllConstraintsPass(self.run_, allow=("C16",))


class SoleTeacherLoadIsNotSpentElsewhere(_SoleTeacher, SchedulerTestCase):
    """Abby has load 1: taking COMP3100 first would leave COMP6100 TBA."""
    LOAD_ABBY = "1"


class SoleTeacherIsTriedLastWhileDutyPending(_SoleTeacher, SchedulerTestCase):
    """Abby has load 2, so load alone would not strand COMP6100; she is still
    tried after Bob for COMP3100, keeping her time free for the course only she
    can teach."""
    LOAD_ABBY = "2"
