"""
WIT Class Scheduler
Assigns faculty, rooms, and time slots to course sections
subject to scheduling constraints.
"""
import contextlib
import csv
import io
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import time
from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

# Scheduling rules enforced below:
#  • Faculty preference is a HARD constraint — a section is only assigned to a
#    prof listed in its preference row; otherwise it goes to TBA. (faculty_candidates)
#  • ≤ 2 graduate (5000+) sections per professor.                        (can_assign)
#  • Load balancing: within the preferred pool the most-underloaded prof (relative
#    to their target load) is tried first, so sections spread toward every prof's
#    target instead of piling onto whoever is listed first.       (faculty_candidates)

# ── Course-list selector ──────────────────────────────────────────────────────
# True  → Spring 27 Excel file  ("list of courses and hours COMP - Spring 27.xlsx")
# False → original CSV          ("data/course_list.csv")
USE_SPRING27 = True
# ─────────────────────────────────────────────────────────────────────────────

ALL_DAYS        = ["M", "T", "W", "Th", "F"]
LECTURE_PATTERNS = [["M", "W"], ["T", "Th"], ["W", "F"]]
# Grad courses meet one night/week; F excluded — no evening slots exist on Friday
GRAD_SINGLE_DAY_PATTERNS = [["M"], ["T"], ["W"], ["Th"]]
GRAD_START_HR   = 18        # 6 PM — grad courses start at 18:00
GRAD_END_HR     = 19        # grad start window: 18:00 ≤ hour < 19
FACULTY_GAP_MIN = 15        # min gap between back-to-back classes for same faculty
RESERVED_START  = 12 * 60   # Tue/Thu 12:00 reserved (minutes from midnight)
RESERVED_END    = 13 * 60   # Tue/Thu 13:00 — ends at 1 PM so 1:00 PM slots are free
AM_CUTOFF_HR    = 12        # hours before this = AM
AM_TARGET_RATIO = 0.60      # 60 % of undergrad meetings should be AM

# Foundational courses anyone can teach — used to top up underloaded profs from
# leftover (TBA) sections (CS1 / CS2 / Data Structures). Intentional, narrow
# exception to the hard preference rule.
FOUNDATION_COURSES = {"COMP1000", "COMP1050", "COMP2000"}


# ──────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Course:
    number: str
    name: str
    lecture_days_per_week: int
    lecture_hours: int
    lab_hours: int
    sections: int
    preferred_room: Optional[str] = None


@dataclass
class Section:
    id: str
    course_number: str
    course_name: str
    lecture_days_per_week: int
    lecture_hours: int
    lab_hours: int
    preferred_room: Optional[str]
    faculty_options: List[str] = field(default_factory=list)


@dataclass
class Room:
    name: str
    type: str
    capacity: int


@dataclass
class TimeSlot:
    start: time
    stop: time
    duration_min: int
    label: str
    evening: bool
    days_allowed: List[str]


@dataclass
class RoomPreference:
    course: str
    type: str
    rank: int
    location: str
    max_cap: int


@dataclass
class ScheduledSection:
    section_id: str
    course_number: str
    course_name: str
    faculty: str
    room: Optional[str]
    days: List[str]
    start_time: Optional[time]
    end_time: Optional[time]
    has_lab: bool
    is_lab: bool = False
    topup: bool = False   # assigned via the underload top-up exception (non-preferred prof)


# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────

def to_int(s: str, default: int = 0) -> int:
    try:
        return int((s or "").strip())
    except (ValueError, AttributeError):
        return default


def parse_time(s: str) -> time:
    parts = s.strip().split(":")
    h, m = int(parts[0]), int(parts[1])
    sec = int(parts[2]) if len(parts) > 2 else 0
    return time(h, m, sec)


def split_csv(s: str) -> List[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def t2m(t: time) -> int:
    """Convert a time object to minutes since midnight."""
    return t.hour * 60 + t.minute


def normalize(course_number: str) -> str:
    return (course_number or "").replace(" ", "").strip()


def is_grad(course_number: str) -> bool:
    m = re.search(r"(\d+)", course_number or "")
    return bool(m and int(m.group(1)) >= 5000)


def course_level(course_number: str) -> int:
    m = re.search(r"(\d+)", course_number or "")
    return int(m.group(1)) if m else 0


def schedule_priority(sec: Section) -> int:
    """Lower number = scheduled first.  Upper-UG → grad → lower-UG."""
    level = course_level(sec.course_number)
    if 3000 <= level < 5000:
        return 0
    if level >= 5000:
        return 1
    return 2


def per_meeting_min(total_min: int, num_days: int) -> int:
    return (total_min + num_days - 1) // num_days


def lecture_lab_minutes(lecture_hours: int, lab_hours: int, graduate: bool = False) -> Tuple[int, int]:
    if graduate:
        # All grad courses: single 155-min evening session (18:00-20:35), no labs
        return 155, 0
    if lecture_hours == 3:
        # 3-2-4 offering: 90 min/meeting × 2 days; lab is 1 h 45 min (105 min)
        return 180, 105
    if lecture_hours == 4:
        # 4-0-4 offering: 80 min/meeting × 2 days; no lab
        return 160, 0
    # Fallback for other hour counts
    return lecture_hours * 60, (90 if lab_hours >= 2 else lab_hours * 60)


def overlaps_reserved(days: List[str], start: time, end: time) -> bool:
    """True if this block falls on Tue/Thu and overlaps the 12:00–13:30 reserved window."""
    if not any(d in ("T", "Th") for d in days):
        return False
    s, e = t2m(start), t2m(end)
    return not (e <= RESERVED_START or s >= RESERVED_END)


def times_conflict(s1: int, e1: int, s2: int, e2: int, gap: int = FACULTY_GAP_MIN) -> bool:
    """True if two time ranges are closer than `gap` minutes."""
    return not (e1 + gap <= s2 or e2 + gap <= s1)


def blocks_overlap(days1: List[str], s1: int, e1: int, days2: List[str], s2: int, e2: int) -> bool:
    """True if two (days, start_min, end_min) blocks share a day and their times
    actually overlap (no faculty gap applied — this is a student-scheduling check)."""
    if not (set(days1) & set(days2)):
        return False
    return s1 < e2 and s2 < e1



# ──────────────────────────────────────────────────────────────────────────────
# CSV LOADERS
# ──────────────────────────────────────────────────────────────────────────────

def load_courses_excel(path: str) -> List[Course]:
    """Load courses from the Spring 27 Excel workbook."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    out = []
    for row in rows[1:]:           # skip header row
        if row[1] is None:         # skip empty rows
            continue
        out.append(Course(
            number=str(row[1]).strip(),
            name=str(row[2]).strip() if row[2] else "",
            lecture_days_per_week=int(row[3]) if row[3] is not None else 0,
            lecture_hours=int(row[4]) if row[4] is not None else 0,
            lab_hours=int(row[5]) if row[5] is not None else 0,
            sections=int(row[6]) if row[6] is not None else 0,
            preferred_room=str(row[7]).strip() if row[7] else None,
        ))
    return out


def load_courses(path: str) -> List[Course]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.append(Course(
                number=r["Course number"].strip(),
                name=r["Course Name"].strip(),
                lecture_days_per_week=to_int(r["lecture days per week"]),
                lecture_hours=to_int(r["lecture hours"]),
                lab_hours=to_int(r["lab hours"]),
                sections=to_int(r["number of sections"]),
                preferred_room=(r.get("Preferred Room") or "").strip() or None,
            ))
    return out


def load_faculty_preferences(path: str) -> Dict[str, List[str]]:
    """Returns {course_number: [ranked faculty list]}."""
    out: Dict[str, List[str]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            course = r["Course Number"].strip()
            fac_str = r.get("Faculty") or r.get("faculty") or ""
            out[course] = split_csv(fac_str)
    return out


def load_rooms(path: str) -> List[Room]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.append(Room(
                name=r["Room"].strip(),
                type=r["Type"].strip(),
                capacity=to_int(r["Capacity"]),
            ))
    return out


def load_timeslots(path: str) -> List[TimeSlot]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out.append(TimeSlot(
                start=parse_time(r["start_time"]),
                stop=parse_time(r["stop_time"]),
                duration_min=to_int(r["duration_min"]),
                label=r["slot_label"].strip(),
                evening=(r["evening"] or "").strip().lower() in ("true", "1", "yes"),
                days_allowed=split_csv(r["Days Allowed"].strip().strip('"')),
            ))
    return out


def load_faculty_loads(path: str) -> Dict[str, int]:
    """Returns {faculty_name: max_course_load}."""
    out: Dict[str, int] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = (r.get("Faculty") or "").strip()
            if name:
                out[name] = to_int(r.get("CS Course Load", "0"))
    return out


def load_room_preferences(path: str) -> Dict[Tuple[str, str], List[RoomPreference]]:
    """Returns {(normalized_course, type_lower): [RoomPreference sorted by rank]}."""
    out: Dict[Tuple[str, str], List[RoomPreference]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            course = normalize(r["Course"])
            rtype = (r["Type"] or "").strip()
            key = (course, rtype.lower())
            pref = RoomPreference(
                course=course,
                type=rtype,
                rank=to_int(r["PreferenceRank"]),
                location=(r["Location"] or "").strip(),
                max_cap=to_int(r.get("max_cap", "0")),
            )
            out.setdefault(key, []).append(pref)
    for lst in out.values():
        lst.sort(key=lambda p: p.rank)
    return out


def load_non_overlap_groups(path: str) -> Dict[str, List[str]]:
    """Returns {group_name: [normalized_course_number, ...]}.

    Each group is a set of courses students are expected to take in the same
    semester per the curriculum (e.g. COMP2000/COMP2100/COMP2650 in Fall
    Year 2). The scheduler tries to keep at least one non-overlapping section
    per course within a group; groups/courses can be added by editing this CSV.
    """
    out: Dict[str, List[str]] = {}
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            group = (r.get("group") or "").strip()
            course = normalize(r.get("course_number") or "")
            if not group or not course:
                continue
            lst = out.setdefault(group, [])
            if course not in lst:
                lst.append(course)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# SECTION BUILDER
# ──────────────────────────────────────────────────────────────────────────────

def build_sections(courses: List[Course], faculty_prefs: Dict[str, List[str]]) -> List[Section]:
    sections: List[Section] = []
    for course in courses:
        if course.sections == 0:
            continue
        fac = faculty_prefs.get(course.number, [])
        for i in range(1, course.sections + 1):
            sections.append(Section(
                id=f"{course.number}-{i}",
                course_number=course.number,
                course_name=course.name,
                lecture_days_per_week=course.lecture_days_per_week,
                lecture_hours=course.lecture_hours,
                lab_hours=course.lab_hours,
                preferred_room=course.preferred_room,
                faculty_options=fac,
            ))
    return sections


# FacultyAssigner removed — faculty selection is now integrated into build_schedule
# so that time, room, and faculty constraints are satisfied jointly.


# ──────────────────────────────────────────────────────────────────────────────
# ROOM ASSIGNER
# ──────────────────────────────────────────────────────────────────────────────

class RoomAssigner:
    """Tracks room availability and assigns rooms to sections."""

    def __init__(self, rooms: List[Room], room_prefs: Dict[Tuple[str, str], List[RoomPreference]]):
        self.rooms = rooms
        self.room_prefs = room_prefs
        self._booked: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}

    def is_free(self, room: str, days: List[str], start: time, end: time) -> bool:
        s, e = t2m(start), t2m(end)
        for d in days:
            for (bs, be) in self._booked.get(room, {}).get(d, []):
                if not (e <= bs or s >= be):
                    return False
        return True

    def _book(self, room: str, days: List[str], start: time, end: time) -> None:
        s, e = t2m(start), t2m(end)
        entry = self._booked.setdefault(room, {})
        for d in days:
            entry.setdefault(d, []).append((s, e))

    def find_room(
        self,
        sec: Section,
        days: List[str],
        start: time,
        end: time,
        *,
        is_lab: bool,
        needed_capacity: int = 25,
    ) -> Optional[str]:
        """Return the best available room name without booking it. None only if no rooms exist."""
        needed_type = "lab" if is_lab else "lecture"
        key = (normalize(sec.course_number), needed_type)

        for pref in self.room_prefs.get(key, []):
            cap = pref.max_cap or needed_capacity
            for room in self.rooms:
                if room.name == pref.location and room.capacity >= cap and self.is_free(room.name, days, start, end):
                    return room.name

        free_candidates = sorted(
            (r for r in self.rooms if r.capacity >= needed_capacity and self.is_free(r.name, days, start, end)),
            key=lambda r: r.capacity,
        )
        if free_candidates:
            return free_candidates[0].name

        if self.rooms:
            worst = min(self.rooms, key=lambda r: r.capacity)
            print(
                f"[ROOM-OVERBOOK] {sec.id} on {days} "
                f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')} → {worst.name}"
            )
            return worst.name

        return None

    def book_room(self, room: str, days: List[str], start: time, end: time) -> None:
        """Commit a room booking found via find_room."""
        self._book(room, days, start, end)

    def find_and_book(
        self,
        sec: Section,
        days: List[str],
        start: time,
        end: time,
        *,
        is_lab: bool,
        needed_capacity: int = 25,
    ) -> Optional[str]:
        room = self.find_room(sec, days, start, end, is_lab=is_lab, needed_capacity=needed_capacity)
        if room:
            self.book_room(room, days, start, end)
        return room


# ──────────────────────────────────────────────────────────────────────────────
# TIME SLOT SCHEDULER
# ──────────────────────────────────────────────────────────────────────────────

class TimeSlotScheduler:
    """Tracks faculty and slot-load availability; finds and books time slots."""

    def __init__(self, timeslots: List[TimeSlot]):
        self.slots = sorted(timeslots, key=lambda t: (t.start.hour, t.start.minute))
        self._faculty_busy: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
        self._slot_load: Dict[str, Dict[str, int]] = {d: {} for d in ALL_DAYS}
        # Every booked (start, end) interval per day. Used for C11 so the guard
        # counts true time overlaps (e.g. a 17:15-18:45 lecture overlapping an
        # 18:00 grad session), exactly as the constraint validator does.
        self._day_intervals: Dict[str, List[Tuple[int, int]]] = {d: [] for d in ALL_DAYS}

    # ── public interface ────────────────────────────────────────────

    def find_slot(
        self,
        sec: Section,
        faculty: str,
        days: List[str],
        min_duration: int,
        *,
        force_pm: bool = False,
        max_duration: Optional[int] = None,
    ) -> Optional[TimeSlot]:
        candidates = self._eligible_slots(sec, min_duration, force_pm=force_pm, max_duration=max_duration)
        ordered = sorted(candidates, key=lambda t: (self._busyness(t, days), t.start.hour, t.start.minute))

        for slot in ordered:
            if slot.days_allowed and not all(d in slot.days_allowed for d in days):
                continue
            if overlaps_reserved(days, slot.start, slot.stop):
                continue
            if not self._slot_capacity_ok(days, slot):        # C11
                continue
            if not self._faculty_free(faculty, days, slot.start, slot.stop):
                continue
            if self._would_exceed_span(faculty, days, slot.start, slot.stop):  # C2
                continue
            return slot
        return None

    def book(self, faculty: str, days: List[str], slot: TimeSlot) -> None:
        self._block_faculty(faculty, days, slot.start, slot.stop)
        self._increment_load(days, slot)

    @property
    def slot_load(self) -> Dict[str, Dict[str, int]]:
        return self._slot_load

    # ── private helpers ─────────────────────────────────────────────

    def _slot_key(self, slot: TimeSlot) -> str:
        return f"{slot.start.strftime('%H:%M')}-{slot.stop.strftime('%H:%M')}"

    def _eligible_slots(
        self,
        sec: Section,
        min_duration: int,
        *,
        force_pm: bool,
        max_duration: Optional[int] = None,
    ) -> List[TimeSlot]:
        def dur_ok(t: TimeSlot) -> bool:
            return t.duration_min >= min_duration and (max_duration is None or t.duration_min <= max_duration)

        if is_grad(sec.course_number):
            return [t for t in self.slots if GRAD_START_HR <= t.start.hour < GRAD_END_HR and dur_ok(t)]
        slots = [t for t in self.slots if t.start.hour < GRAD_START_HR and dur_ok(t)]
        if force_pm:
            slots = [t for t in slots if t.start.hour >= AM_CUTOFF_HR]
        return slots

    def _faculty_free(self, faculty: str, days: List[str], start: time, end: time) -> bool:
        if faculty == "TBA":
            return True
        s, e = t2m(start), t2m(end)
        busy = self._faculty_busy.get(faculty, {})
        for d in days:
            for (bs, be) in busy.get(d, []):
                if times_conflict(s, e, bs, be):
                    return False
        return True

    def _block_faculty(self, faculty: str, days: List[str], start: time, end: time) -> None:
        if faculty == "TBA":
            return
        s, e = t2m(start), t2m(end)
        entry = self._faculty_busy.setdefault(faculty, {})
        for d in days:
            entry.setdefault(d, []).append((s, e))

    def _slot_capacity_ok(self, days: List[str], slot: TimeSlot) -> bool:
        # Count sections whose time range actually overlaps this slot (not just
        # those with the same start minute) so the ≤ 10 concurrent limit (C11)
        # holds even when blocks of different lengths/start times overlap.
        s, e = t2m(slot.start), t2m(slot.stop)
        for d in days:
            concurrent = sum(1 for (bs, be) in self._day_intervals[d] if not (e <= bs or be <= s))
            if concurrent >= 10:
                return False
        return True

    def _increment_load(self, days: List[str], slot: TimeSlot) -> None:
        key = self._slot_key(slot)
        s, e = t2m(slot.start), t2m(slot.stop)
        for d in days:
            self._slot_load[d][key] = self._slot_load[d].get(key, 0) + 1
            self._day_intervals[d].append((s, e))

    def _busyness(self, slot: TimeSlot, days: List[str]) -> int:
        key = self._slot_key(slot)
        return max(self._slot_load[d].get(key, 0) for d in days)

    def _would_exceed_span(self, faculty: str, days: List[str], start: time, end: time) -> bool:
        """True if adding this block would push the faculty's teaching span > 9 h on any day."""
        if faculty == "TBA":
            return False
        s, e = t2m(start), t2m(end)
        busy = self._faculty_busy.get(faculty, {})
        for d in days:
            existing = busy.get(d, [])
            all_times = existing + [(s, e)]
            span_hr = (max(e2 for _, e2 in all_times) - min(s2 for s2, _ in all_times)) / 60
            if span_hr > 9:
                return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULER ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────────────────

def build_schedule(
    sections: List[Section],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
    time_sched: TimeSlotScheduler,
    room_assigner: RoomAssigner,
    non_overlap_groups: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, ScheduledSection]:
    """
    Jointly assigns faculty + day pattern + time slot + room for each section so that
    C2 (daily span), C4 (days/week), C11 (concurrency), and C16 (day balance) are
    all satisfied during assignment rather than flagged after the fact.
    """
    lectures: Dict[str, ScheduledSection] = {}
    labs: List[ScheduledSection] = []

    # ── non-overlap groups (data/non_overlap_groups.csv) ───────────────────────
    # course_to_groups: normalized course number → list of group names it belongs to.
    # group_reps: group → {course → (days_frozenset, start_min, end_min)} — the first
    # non-overlapping time found for each course in the group; used to bias later
    # sections of other group courses away from it (best-effort, checked at the end).
    non_overlap_groups = non_overlap_groups or {}
    course_to_groups: Dict[str, List[str]] = {}
    for grp, courses_in_grp in non_overlap_groups.items():
        for c in courses_in_grp:
            course_to_groups.setdefault(c, []).append(grp)
    group_reps: Dict[str, Dict[str, Tuple[frozenset, int, int]]] = {}

    def _group_bias(course_number: str, days: List[str], start_min: int, end_min: int) -> int:
        """0 if this slot keeps a non-overlapping representative achievable for
        every group this course belongs to, 1 if it would clash with another
        course's already-established representative (deprioritized, not banned)."""
        cn = normalize(course_number)
        for grp in course_to_groups.get(cn, []):
            reps = group_reps.get(grp, {})
            if cn in reps:
                continue  # this course already has a safe representative
            for other_cn, (o_days, o_s, o_e) in reps.items():
                if other_cn != cn and blocks_overlap(days, start_min, end_min, list(o_days), o_s, o_e):
                    return 1
        return 0

    def _record_group_rep(course_number: str, days: List[str], start_min: int, end_min: int) -> None:
        cn = normalize(course_number)
        for grp in course_to_groups.get(cn, []):
            reps = group_reps.setdefault(grp, {})
            if cn in reps:
                continue
            conflict = any(
                other_cn != cn and blocks_overlap(days, start_min, end_min, list(o_days), o_s, o_e)
                for other_cn, (o_days, o_s, o_e) in reps.items()
            )
            if not conflict:
                reps[cn] = (frozenset(days), start_min, end_min)

    # ── integrated state ───────────────────────────────────────────────────────
    faculty_load: Dict[str, int] = {f: 0 for f in faculty_limits}
    faculty_days_map: Dict[str, set] = {}   # {faculty → set of days they teach}
    day_count: Dict[str, int] = {d: 0 for d in ALL_DAYS}  # sections per day (C16)

    # AM/PM balance (undergrad only)
    total_ug = sum(
        1 + (1 if s.lab_hours > 0 else 0)
        for s in sections if not is_grad(s.course_number)
    )
    max_am = math.ceil(AM_TARGET_RATIO * total_ug)
    am_used = 0

    ordered = sorted(sections, key=lambda s: (schedule_priority(s), s.course_number, s.id))

    # ── helpers scoped to this function ───────────────────────────────────────

    def max_load(fac: str) -> int:
        return faculty_limits.get(fac, 3)

    def can_assign(fac: str, sec: Section) -> bool:
        """Faculty has remaining load capacity, hasn't taught 2 sections of this
        course yet, and — for grad courses — hasn't already been given 2 grad sections."""
        faculty_load.setdefault(fac, 0)
        if faculty_load[fac] >= max_load(fac):
            return False
        same = sum(
            1 for s in lectures.values()
            if s.faculty == fac and s.course_number == sec.course_number
        )
        if same >= 2:
            return False
        # ≤ 2 graduate (5000+) sections per professor, across all grad courses.
        if is_grad(sec.course_number):
            grad_count = sum(
                1 for s in lectures.values()
                if s.faculty == fac and is_grad(s.course_number)
            )
            if grad_count >= 2:
                return False
        return True

    def faculty_candidates(sec: Section) -> List[str]:
        """Preferred faculty only — preference is a HARD constraint, so a section
        is never offered to a prof outside its preference row (it falls through to
        the TBA fallback instead). Within the preferred pool the most-underloaded
        prof (smallest load / target ratio) is tried first so sections spread toward
        every prof's target; CSV rank order breaks ties (stable sort)."""
        seen: set = set()
        pref: List[str] = []
        for f in fac_prefs.get(sec.course_number, []):
            if f not in seen:
                pref.append(f)
                seen.add(f)
                faculty_load.setdefault(f, 0)

        def fill_ratio(f: str) -> float:
            cap = max_load(f)
            return faculty_load.get(f, 0) / cap if cap > 0 else float("inf")

        return sorted(pref, key=fill_ratio)

    def _patterns_for(sec: Section) -> List[List[str]]:
        """Ordered pool of day patterns to try for a section.
        Grad courses with lecture_days_per_week==1 prefer single-day evenings (M/T/W/Th),
        falling back to 2-day patterns for courses whose total minutes exceed the longest
        single evening slot (e.g. 4-credit courses needing 240 min).
        """
        if is_grad(sec.course_number) and sec.lecture_days_per_week == 1:
            return GRAD_SINGLE_DAY_PATTERNS + LECTURE_PATTERNS
        return LECTURE_PATTERNS

    def viable_patterns(fac: str, sec: Section) -> List[List[str]]:
        """
        Return patterns that keep faculty ≤ 4 days (C4), sorted by load added to
        already-busy days (lightest first, for C16 balance).
        For grad single-day courses, single-day patterns are ranked first; 2-day patterns
        follow as a fallback (e.g. 4-credit courses that exceed the longest evening slot).
        Falls back to all patterns if C4 cannot be satisfied.
        """
        current = faculty_days_map.get(fac, set())

        def _rank(pool: List[List[str]]) -> List[List[str]]:
            ok, over = [], []
            for p in pool:
                score = sum(day_count.get(d, 0) for d in p)
                (ok if len(current | set(p)) <= 4 else over).append((score, p))
            ok.sort(key=lambda x: x[0])
            over.sort(key=lambda x: x[0])
            return [p for _, p in ok] or [p for _, p in over]

        if is_grad(sec.course_number) and sec.lecture_days_per_week == 1:
            return _rank(GRAD_SINGLE_DAY_PATTERNS) + _rank(LECTURE_PATTERNS)
        return _rank(LECTURE_PATTERNS)

    LAB_MAX_MIN = 105  # lab sessions are 1 h 45 min (105 min) in the 3-2-4 offering

    def _lab_day_candidates(fac: str, lecture_days: List[str]) -> List[str]:
        """All non-lecture days ordered by preference: C4-safe first, then least-loaded."""
        current = faculty_days_map.get(fac, set()) | set(lecture_days)
        non_lecture = [d for d in ALL_DAYS if d not in lecture_days]
        c4_ok = sorted([d for d in non_lecture if len(current | {d}) <= 4],
                       key=lambda d: day_count.get(d, 0))
        overflow = sorted([d for d in non_lecture if d not in c4_ok],
                          key=lambda d: day_count.get(d, 0))
        return c4_ok + overflow

    def _find_lab_at(
        fac: str,
        lecture_days: List[str],
        lec_slot: "TimeSlot",
    ) -> Optional[Tuple[str, "TimeSlot", str]]:
        """
        Find (lab_day, lab_slot, lab_room) where lab_slot.start == lec_slot.start.
        Only considers C4-safe days (adding the day keeps faculty ≤ 4 days/week).
        Returns None if no valid same-start lab can be placed.
        """
        current = faculty_days_map.get(fac, set()) | set(lecture_days)
        non_lecture = [d for d in ALL_DAYS if d not in lecture_days]
        c4_safe = sorted(
            [d for d in non_lecture if len(current | {d}) <= 4],
            key=lambda d: day_count.get(d, 0),
        )
        for lab_day in c4_safe:
            for lab_s in time_sched.slots:
                if lab_s.start != lec_slot.start:
                    continue
                if lab_s.duration_min < lab_min or lab_s.duration_min > LAB_MAX_MIN:
                    continue
                if lab_s.days_allowed and lab_day not in lab_s.days_allowed:
                    continue
                if overlaps_reserved([lab_day], lab_s.start, lab_s.stop):
                    continue
                if not time_sched._slot_capacity_ok([lab_day], lab_s):
                    continue
                if not time_sched._faculty_free(fac, [lab_day], lab_s.start, lab_s.stop):
                    continue
                if time_sched._would_exceed_span(fac, [lab_day], lab_s.start, lab_s.stop):
                    continue
                lab_room = room_assigner.find_room(sec, [lab_day], lab_s.start, lab_s.stop, is_lab=True)
                if lab_room is not None:
                    return lab_day, lab_s, lab_room
        return None

    def _try_assign(
        sec: Section,
        fac: str,
        days: List[str],
        lec_min: int,
        force_pm: bool,
    ) -> Optional[Tuple]:
        """
        Find (days, lec_slot, lec_room, lab_day, lab_slot, lab_room).
        When the section has a lab, the lab slot must start at the SAME CLOCK TIME
        as the lecture slot (hard constraint). Returns None if impossible.
        lab_day/slot/room are None when lab_hours == 0.
        """
        per_day = per_meeting_min(lec_min, len(days))

        # Collect and order candidate lecture slots.
        # max_duration=per_day ensures lectures only get slots of the correct duration
        # and never spill into the wider lab_105 slots.
        # When force_pm is set, sort PM slots before AM so they're tried first.
        raw = time_sched._eligible_slots(sec, per_day, force_pm=False, max_duration=per_day)
        if force_pm:
            raw.sort(key=lambda t: (
                0 if t.start.hour >= AM_CUTOFF_HR else 1,
                time_sched._busyness(t, days),
                t.start.hour, t.start.minute,
            ))
        else:
            raw.sort(key=lambda t: (time_sched._busyness(t, days), t.start.hour, t.start.minute))
        raw.sort(key=lambda t: _group_bias(sec.course_number, days, t2m(t.start), t2m(t.stop)))

        for lec_slot in raw:
            if lec_slot.days_allowed and not all(d in lec_slot.days_allowed for d in days):
                continue
            if overlaps_reserved(days, lec_slot.start, lec_slot.stop):
                continue
            if not time_sched._slot_capacity_ok(days, lec_slot):
                continue
            if not time_sched._faculty_free(fac, days, lec_slot.start, lec_slot.stop):
                continue
            if time_sched._would_exceed_span(fac, days, lec_slot.start, lec_slot.stop):
                continue

            lec_room = room_assigner.find_room(sec, days, lec_slot.start, lec_slot.stop, is_lab=False)
            if lec_room is None:
                continue

            if sec.lab_hours == 0:
                return days, lec_slot, lec_room, None, None, None

            lab_info = _find_lab_at(fac, days, lec_slot)
            if lab_info is not None:
                return days, lec_slot, lec_room, *lab_info
            # This lecture slot can't pair with a same-start lab; try next slot.

        return None

    def _topup_underloaded(lab_by_parent: Dict[str, ScheduledSection]) -> None:
        """Fill any prof still below their target load using leftover (TBA)
        foundational sections — CS1 / CS2 / Data Structures — that anyone can
        teach. This is a deliberate, narrow exception to the hard preference
        rule: it assigns a prof to a course they're not listed as preferred for,
        but only an already-placed TBA section and only to reach the prof's
        target. Sections are marked `topup` so C19 records them as exceptions.
        """
        def underloaded() -> List[str]:
            profs = [f for f in faculty_limits
                     if f != "TBA" and max_load(f) > 0 and faculty_load.get(f, 0) < max_load(f)]
            return sorted(profs, key=lambda f: faculty_load.get(f, 0) / max_load(f))

        def feasible(fac: str, lec: ScheduledSection, lab: Optional[ScheduledSection]) -> bool:
            if faculty_load.get(fac, 0) >= max_load(fac):
                return False
            same = sum(1 for s in lectures.values()
                       if s.faculty == fac and s.course_number == lec.course_number)
            if same >= 2:                                                      # C3
                return False
            new_days = set(faculty_days_map.get(fac, set())) | set(lec.days)
            if lab:
                new_days |= set(lab.days)
            if len(new_days) > 4:                                              # C4
                return False
            # Faculty must be free (incl. 15-min gap) and within the 9 h span (C2)
            # for both the lecture and, if present, its lab. Lecture and lab fall
            # on different days (C9), so they can be checked independently.
            if not time_sched._faculty_free(fac, lec.days, lec.start_time, lec.end_time):
                return False
            if time_sched._would_exceed_span(fac, lec.days, lec.start_time, lec.end_time):
                return False
            if lab:
                if not time_sched._faculty_free(fac, lab.days, lab.start_time, lab.end_time):
                    return False
                if time_sched._would_exceed_span(fac, lab.days, lab.start_time, lab.end_time):
                    return False
            return True

        tba_foundation = [s for s in lectures.values()
                          if s.faculty == "TBA" and not s.is_lab
                          and normalize(s.course_number) in FOUNDATION_COURSES]
        for lec in tba_foundation:
            lab = lab_by_parent.get(lec.section_id)
            for fac in underloaded():
                if not feasible(fac, lec, lab):
                    continue
                # Reassign faculty only — time/room/days are unchanged, so room
                # and concurrency (C11) bookings stay valid; just block the prof.
                time_sched._block_faculty(fac, lec.days, lec.start_time, lec.end_time)
                faculty_days_map.setdefault(fac, set()).update(lec.days)
                lec.faculty, lec.topup = fac, True
                if lab:
                    time_sched._block_faculty(fac, lab.days, lab.start_time, lab.end_time)
                    faculty_days_map[fac].update(lab.days)
                    lab.faculty, lab.topup = fac, True
                faculty_load[fac] = faculty_load.get(fac, 0) + 1
                print(f"[TOPUP] {lec.section_id} ({lec.course_number}) → {fac} "
                      f"(was TBA; load now {faculty_load[fac]}/{max_load(fac)})")
                break

    # ── main scheduling loop ──────────────────────────────────────────────────

    for sec in ordered:
        lec_min, lab_min = lecture_lab_minutes(sec.lecture_hours, sec.lab_hours, is_grad(sec.course_number))
        force_pm = not is_grad(sec.course_number) and am_used >= max_am

        # chosen = (fac, days, lec_slot, lec_room, lab_day|None, lab_slot|None, lab_room|None)
        chosen = None

        # Search jointly over (faculty × day_pattern) until all constraints satisfied
        for fac in faculty_candidates(sec):
            if not can_assign(fac, sec):
                continue
            for days in viable_patterns(fac, sec):
                result = _try_assign(sec, fac, days, lec_min, force_pm)
                if result:
                    chosen = (fac, *result)
                    break
            if chosen:
                break

        # Fallback: TBA faculty. Try day patterns least-loaded-first so TBA
        # sections (e.g. grad courses whose preferred profs are all full) spread
        # across the week instead of piling onto Monday.
        if not chosen:
            print(f"[WARN] {sec.id}: No faculty satisfied all constraints; trying TBA.")
            tba_patterns = sorted(_patterns_for(sec), key=lambda p: sum(day_count.get(d, 0) for d in p))
            for days in tba_patterns:
                result = _try_assign(sec, "TBA", days, lec_min, False)
                if result:
                    chosen = ("TBA", *result)
                    break

        # Hard fallback: force something rather than crash
        if not chosen:
            print(f"[CRITICAL] {sec.id}: No assignment found; forcing.")
            days_f = _patterns_for(sec)[0]
            per_day_f = per_meeting_min(lec_min, len(days_f))
            cands = time_sched._eligible_slots(sec, per_day_f, force_pm=False, max_duration=None)
            slot_f = cands[0] if cands else time_sched.slots[0]
            lab_info_f = _find_lab_at("TBA", days_f, slot_f) if sec.lab_hours > 0 else None
            chosen = ("TBA", days_f, slot_f, "FORCE_ASSIGN_ROOM",
                      *(lab_info_f if lab_info_f else (None, None, None)))

        fac, days, slot, room, pre_lab_day, pre_lab_slot, pre_lab_room = chosen

        # Commit lecture
        room_assigner.book_room(room, days, slot.start, slot.stop)
        time_sched.book(fac, days, slot)
        faculty_load[fac] = faculty_load.get(fac, 0) + 1
        faculty_days_map.setdefault(fac, set()).update(days)
        for d in days:
            day_count[d] += 1
        if not is_grad(sec.course_number):
            am_used += 1 if slot.start.hour < AM_CUTOFF_HR else 0

        lectures[sec.id] = ScheduledSection(
            section_id=sec.id,
            course_number=sec.course_number,
            course_name=sec.course_name,
            faculty=fac,
            room=room,
            days=list(days),
            start_time=slot.start,
            end_time=slot.stop,
            has_lab=sec.lab_hours > 0,
            is_lab=False,
        )
        _record_group_rep(sec.course_number, days, t2m(slot.start), t2m(slot.stop))

        # ── lab ───────────────────────────────────────────────────────────────
        if sec.lab_hours > 0:
            if pre_lab_day is not None:
                # Same-start lab found during assignment — commit it directly.
                lab_day, lab_slot, lab_room = pre_lab_day, pre_lab_slot, pre_lab_room
                room_assigner.book_room(lab_room, [lab_day], lab_slot.start, lab_slot.stop)
            else:
                # Hard-fallback path: no same-start lab found; place lab anywhere available.
                print(f"[WARN] {sec.id}-LAB: could not match lecture start time; scheduling independently.")
                lab_day_candidates = _lab_day_candidates(fac, days)
                lab_slot = None
                lab_day = lab_day_candidates[0] if lab_day_candidates else ALL_DAYS[0]
                for ld in lab_day_candidates:
                    lab_slot = time_sched.find_slot(sec, fac, [ld], lab_min, max_duration=LAB_MAX_MIN)
                    if lab_slot:
                        lab_day = ld
                        break
                if lab_slot is None:
                    print(f"[CRITICAL] {sec.id}-LAB: No time slot found; forcing.")
                    cands = time_sched._eligible_slots(sec, lab_min, force_pm=False, max_duration=LAB_MAX_MIN)
                    lab_slot = cands[0] if cands else time_sched.slots[0]
                lab_room = room_assigner.find_room(sec, [lab_day], lab_slot.start, lab_slot.stop, is_lab=True)
                if lab_room is None:
                    lab_room = "FORCE_ASSIGN_ROOM"
                else:
                    room_assigner.book_room(lab_room, [lab_day], lab_slot.start, lab_slot.stop)

            time_sched.book(fac, [lab_day], lab_slot)
            faculty_days_map.setdefault(fac, set()).add(lab_day)
            day_count[lab_day] += 1
            if not is_grad(sec.course_number):
                am_used += 1 if lab_slot.start.hour < AM_CUTOFF_HR else 0

            labs.append(ScheduledSection(
                section_id=f"{sec.id}-LAB",
                course_number=sec.course_number,
                course_name=sec.course_name,
                faculty=fac,
                room=lab_room,
                days=[lab_day],
                start_time=lab_slot.start,
                end_time=lab_slot.stop,
                has_lab=False,
                is_lab=True,
            ))

    lab_by_parent = {lab.section_id.replace("-LAB", ""): lab for lab in labs}

    # Top up under-target profs with leftover TBA foundational sections.
    _topup_underloaded(lab_by_parent)

    # Interleave labs right after their parent lecture
    result: Dict[str, ScheduledSection] = {}
    for sid, s in lectures.items():
        result[sid] = s
        if sid in lab_by_parent:
            lab = lab_by_parent[sid]
            result[lab.section_id] = lab

    return result


# ──────────────────────────────────────────────────────────────────────────────
# CONSTRAINT CHECKER
# ──────────────────────────────────────────────────────────────────────────────

class ConstraintChecker:
    """Validates a completed schedule against all scheduling constraints."""

    def __init__(self, fac_prefs: Optional[Dict[str, List[str]]] = None):
        self.fac_prefs = fac_prefs or {}

    def run_all(
        self,
        sections: Dict[str, ScheduledSection],
        faculty_limits: Dict[str, int],
    ) -> bool:
        checks = [
            ("C1  Faculty course load matches limits",          self._c1_load),
            ("C2  Faculty daily span ≤ 9 h",                   self._c2_daily),
            ("C3  Faculty ≤ 2 sections of same course",         self._c3_duplicates),
            ("C4  Faculty teaches ≤ 4 days/week",               self._c4_days),
            ("C5  No blank faculty field",                      self._c5_assigned),
            ("C7  Lecture/lab duration in valid range",         self._c7_durations),
            ("C9  Lab on different day than lecture",           self._c9_lab_day),
            ("C10 Lab is exactly one day",                      self._c10_lab_one_day),
            ("C11 ≤ 10 concurrent sections per time slot",      self._c11_concurrency),
            ("C12 Graduate courses start at 6 PM (18:00)",       self._c12_grad_time),
            ("C13 Same faculty for lecture and its lab",        self._c13_lab_faculty),
            ("C14 ≤ 2 sections of same course at same time",    self._c14_time_dupes),
            ("C15 Lecture day patterns: MW / TTh / WF only",    self._c15_patterns),
            ("C16 Sections balanced across weekdays (≤ 40 %)",  self._c16_balance),
            ("C17 Lab starts at the same time as its lecture",   self._c17_lab_same_start),
            ("C18 ≤ 2 graduate sections per faculty",            self._c18_grad_per_faculty),
            ("C19 Faculty preference honored (hard constraint)", self._c19_pref_honored),
        ]

        print("\n══════════════════ CONSTRAINT VALIDATION ══════════════════")
        all_ok = True
        for label, fn in checks:
            try:
                ok = fn(sections, faculty_limits)
            except Exception as exc:
                print(f"  ⚠  {label}: exception — {exc}")
                ok = False
            print(f"  {'✓ PASS' if ok else '✗ FAIL'}  {label}")
            all_ok = all_ok and ok
        print("════════════════════════════════════════════════════════════\n")
        return all_ok

    # ── individual checks ───────────────────────────────────────────

    def _c1_load(self, sections, limits):
        counts: Dict[str, int] = {}
        for s in sections.values():
            if not s.is_lab:
                counts[s.faculty] = counts.get(s.faculty, 0) + 1
        ok = True
        for fac, count in counts.items():
            if fac == "TBA":
                continue
            expected = limits.get(fac, 3)
            if count > expected:
                print(f"    {fac}: {count} courses (expected {expected}) — OVERLOADED")
                ok = False
            elif count < expected:
                print(f"    ⚠ {fac}: {count} courses (target {expected}) — under target")
                # Under-target is a warning only, not a failure
        return ok

    def _c2_daily(self, sections, _):
        fac_days: Dict[str, Dict[str, List[Tuple[int, int]]]] = {}
        for s in sections.values():
            if not s.start_time:
                continue
            for d in s.days:
                fac_days.setdefault(s.faculty, {}).setdefault(d, []).append(
                    (t2m(s.start_time), t2m(s.end_time))
                )
        ok = True
        for fac, days in fac_days.items():
            if fac == "TBA":
                continue  # TBA is a placeholder, not a real faculty member
            for d, slots in days.items():
                span_hr = (max(e for _, e in slots) - min(s for s, _ in slots)) / 60
                if span_hr > 9:
                    print(f"    {fac} on {d}: {span_hr:.1f} h span (> 9 h)")
                    ok = False
        return ok

    def _c3_duplicates(self, sections, _):
        counts: Dict[Tuple[str, str], int] = {}
        for s in sections.values():
            if not s.is_lab:
                key = (s.faculty, s.course_number)
                counts[key] = counts.get(key, 0) + 1
        ok = True
        for (fac, course), n in counts.items():
            if fac != "TBA" and n > 2:
                print(f"    {fac} teaches {n} sections of {course}")
                ok = False
        return ok

    def _c4_days(self, sections, _):
        fac_days: Dict[str, set] = {}
        for s in sections.values():
            fac_days.setdefault(s.faculty, set()).update(s.days)
        ok = True
        for fac, days in fac_days.items():
            if fac != "TBA" and len(days) >= 5:
                print(f"    {fac} teaches {len(days)} days ({','.join(sorted(days))})")
                ok = False
        return ok

    def _c5_assigned(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if not s.faculty:
                print(f"    {sid} has empty faculty field")
                ok = False
        return ok

    def _c7_durations(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if not s.start_time:
                continue
            dur = t2m(s.end_time) - t2m(s.start_time)
            if s.is_lab:
                if not (100 <= dur <= 110):
                    print(f"    LAB {sid}: {dur} min (expect 105)")
                    ok = False
            elif is_grad(s.course_number):
                if not (145 <= dur <= 165):
                    print(f"    {sid}: {dur} min (grad, expect 155)")
                    ok = False
            else:
                if not (75 <= dur <= 95):
                    print(f"    LEC {sid}: {dur} min (expect 80 or 90)")
                    ok = False
        return ok

    def _c9_lab_day(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if not s.is_lab:
                continue
            base = sid.replace("-LAB", "")
            if base in sections:
                shared = set(sections[base].days) & set(s.days)
                if shared:
                    print(f"    {sid}: shares day(s) {shared} with lecture")
                    ok = False
        return ok

    def _c10_lab_one_day(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if s.is_lab and len(s.days) > 1:
                print(f"    {sid}: lab on multiple days {s.days}")
                ok = False
        return ok

    def _c11_concurrency(self, sections, _):
        day_intervals: Dict[str, List[Tuple[int, int]]] = {d: [] for d in ALL_DAYS}
        for s in sections.values():
            if not s.start_time:
                continue
            for d in s.days:
                day_intervals[d].append((t2m(s.start_time), t2m(s.end_time)))

        ok = True
        for d, intervals in day_intervals.items():
            for s1, e1 in intervals:
                concurrent = sum(1 for s2, e2 in intervals if not (e1 <= s2 or e2 <= s1))
                if concurrent > 10:
                    print(f"    {d} {s1 // 60:02d}:{s1 % 60:02d}: {concurrent} concurrent sections")
                    ok = False
                    break
        return ok

    def _c12_grad_time(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if s.start_time and is_grad(s.course_number):
                if not (GRAD_START_HR <= s.start_time.hour < GRAD_END_HR):
                    print(f"    {sid}: starts at {s.start_time} (not 6 PM)")
                    ok = False
        return ok

    def _c13_lab_faculty(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if s.is_lab:
                base = sid.replace("-LAB", "")
                if base in sections and sections[base].faculty != s.faculty:
                    print(f"    {sid}: lab={s.faculty} ≠ lecture={sections[base].faculty}")
                    ok = False
        return ok

    def _c14_time_dupes(self, sections, _):
        seen: Dict[tuple, int] = {}
        for s in sections.values():
            if not s.start_time:
                continue
            key = (s.course_number, tuple(sorted(s.days)), t2m(s.start_time), t2m(s.end_time))
            seen[key] = seen.get(key, 0) + 1
        ok = True
        for (course, days, start, _), n in seen.items():
            if n > 2:
                print(f"    {course}: {n} sections at same time on {days} ({start // 60:02d}:{start % 60:02d})")
                ok = False
        return ok

    def _c15_patterns(self, sections, _):
        valid = [{"M", "W"}, {"T", "Th"}, {"W", "F"}]
        ok = True
        for sid, s in sections.items():
            if not s.is_lab and len(s.days) == 2 and set(s.days) not in valid:
                print(f"    {sid}: invalid 2-day pattern {s.days}")
                ok = False
        return ok

    def _c16_balance(self, sections, _):
        count = {d: 0 for d in ALL_DAYS}
        for s in sections.values():
            for d in s.days:
                count[d] += 1
        avg = sum(count.values()) / len(count)
        ok = True
        for d, c in count.items():
            if avg > 0 and abs(c - avg) > 0.4 * avg:
                print(f"    {d}: {c} sections vs avg {avg:.1f} (>40 % deviation)")
                ok = False
        return ok

    def _c17_lab_same_start(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if not s.is_lab or not s.start_time:
                continue
            base = sid.replace("-LAB", "")
            if base not in sections:
                continue
            lec = sections[base]
            if lec.start_time and t2m(s.start_time) != t2m(lec.start_time):
                print(f"    {sid}: lab starts {s.start_time.strftime('%H:%M')} "
                      f"≠ lecture {lec.start_time.strftime('%H:%M')}")
                ok = False
        return ok

    def _c18_grad_per_faculty(self, sections, _):
        counts: Dict[str, int] = {}
        for s in sections.values():
            if not s.is_lab and is_grad(s.course_number):
                counts[s.faculty] = counts.get(s.faculty, 0) + 1
        ok = True
        for fac, n in counts.items():
            if fac != "TBA" and n > 2:
                print(f"    {fac}: {n} graduate sections (max 2)")
                ok = False
        return ok

    def _c19_pref_honored(self, sections, _):
        ok = True
        for sid, s in sections.items():
            if s.faculty == "TBA" or s.is_lab:
                continue
            prefs = self.fac_prefs.get(s.course_number)
            if prefs and s.faculty not in prefs:
                if s.topup:
                    print(f"    (exception) {sid}: {s.faculty} via underload top-up of {s.course_number}")
                    continue
                print(f"    {sid}: {s.faculty} not in preference list for {s.course_number}")
                ok = False
        return ok


def check_non_overlap_groups(
    sections: Dict[str, ScheduledSection],
    groups: Dict[str, List[str]],
) -> bool:
    """Best-effort verification for data/non_overlap_groups.csv: for every pair
    of courses within a group, at least one scheduled lecture section of each
    course must not share a day/time with any section of the other — so a
    student following the curriculum can register for one section of each
    without a clash. Reports failures as warnings; does not raise.
    """
    if not groups:
        return True

    by_course: Dict[str, List[ScheduledSection]] = {}
    for s in sections.values():
        if not s.is_lab:
            by_course.setdefault(normalize(s.course_number), []).append(s)

    print("\n── NON-OVERLAP GROUP CHECK (data/non_overlap_groups.csv) ──")
    all_ok = True
    for grp, courses in groups.items():
        present = [c for c in courses if c in by_course]
        for i, c1 in enumerate(present):
            for c2 in present[i + 1:]:
                secs1, secs2 = by_course[c1], by_course[c2]
                has_clear_pair = any(
                    not blocks_overlap(a.days, t2m(a.start_time), t2m(a.end_time),
                                       b.days, t2m(b.start_time), t2m(b.end_time))
                    for a in secs1 for b in secs2
                )
                if has_clear_pair:
                    print(f"  ✓ PASS  {grp}: {c1} / {c2} have a non-overlapping section pair")
                else:
                    print(f"  ✗ FAIL  {grp}: {c1} / {c2} — every section pair overlaps; "
                          f"a student cannot take both without a conflict")
                    all_ok = False
    print("────────────────────────────────────────────────────────────\n")
    return all_ok


# ──────────────────────────────────────────────────────────────────────────────
# EXPORTERS
# ──────────────────────────────────────────────────────────────────────────────

def export_json(
    sections: Dict[str, ScheduledSection],
    courses: List[Course],
    path: str = "schedule.json",
) -> None:
    sections_count = {normalize(c.number): c.sections for c in courses}
    events = []

    for sid, s in sections.items():
        if not s.start_time:
            continue
        total = sections_count.get(normalize(s.course_number), 1)
        parts = sid.split("-")
        sec_num = parts[1] if len(parts) >= 2 and parts[1].isdigit() else None
        display = s.course_number if total <= 1 or sec_num is None else f"{s.course_number}-{sec_num}"

        for day in s.days:
            events.append({
                "id": sid,
                "day": day,
                "course": display,
                "prof": s.faculty,
                "room": s.room,
                "start": s.start_time.strftime("%H:%M"),
                "end": s.end_time.strftime("%H:%M"),
                "isLab": s.is_lab,
            })

    with open(path, "w") as f:
        json.dump(events, f, indent=2)
    print(f"✓ Exported {len(events)} events → {path}")


def export_csv(
    sections: Dict[str, ScheduledSection],
    course_titles: Dict[str, str],
    path: str = "schedule.csv",
) -> None:
    def split_subj_crse(num: str) -> Tuple[str, str]:
        subj = re.match(r"([A-Za-z]+)", num or "")
        crse = re.search(r"(\d+)", num or "")
        return (subj.group(1) if subj else ""), (crse.group(1) if crse else "")

    def section_label(sid: str) -> str:
        parts = sid.split("-")
        if len(parts) < 2:
            return ""
        return f"{parts[1]}L" if len(parts) >= 3 and parts[2].upper() == "LAB" else parts[1]

    def fmt_time(t: time) -> str:
        h = t.hour % 12 or 12
        return f"{h:02d}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["CRN", "Subj", "Crse", "Section", "Location", "Credit",
                    "Title", "Days", "Time", "Cap.", "Act.", "Rem", "Instructor", "Date (MM/DD)"])
        for sid, s in sections.items():
            if not s.start_time:
                continue
            subj, crse = split_subj_crse(s.course_number)
            title = course_titles.get(normalize(s.course_number), "")
            if s.is_lab:
                title = f"{title} - LAB" if title else "LAB"
            w.writerow([
                "",                                         # CRN (empty)
                subj, crse,                                 # Subj / Crse
                section_label(sid),                         # Section
                s.room or "",                               # Location
                "",                                         # Credit (empty)
                title,                                      # Title
                "".join(s.days),                            # Days
                f"{fmt_time(s.start_time)}-{fmt_time(s.end_time)}",  # Time
                25, "", "",                                 # Cap / Act / Rem
                s.faculty or "",                            # Instructor
                "",                                         # Date (empty)
            ])
    print(f"✓ Exported Excel-style CSV → {path}")


# ──────────────────────────────────────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────────────────────────────────────

def print_summary(
    sections: Dict[str, ScheduledSection],
    courses: List[Course],
    faculty_limits: Dict[str, int],
    slot_load: Dict[str, Dict[str, int]],
) -> None:
    total_lec = sum(1 for s in sections.values() if not s.is_lab)
    total_lab = sum(1 for s in sections.values() if s.is_lab)
    total_fac = len({s.faculty for s in sections.values() if s.faculty and s.faculty != "TBA"})
    am = sum(1 for s in sections.values() if s.start_time and s.start_time.hour < AM_CUTOFF_HR)
    pm_count = sum(1 for s in sections.values() if s.start_time and s.start_time.hour >= AM_CUTOFF_HR)

    print("\n══════════════════ GLOBAL SUMMARY ══════════════════")
    print(f"  Courses in course_list.csv  : {len({c.number for c in courses})}")
    print(f"  Lecture sections            : {total_lec}")
    print(f"  Lab sections                : {total_lab}")
    print(f"  Unique faculty (≠ TBA)      : {total_fac}")
    print(f"  AM / PM sections            : {am} / {pm_count}")
    print("════════════════════════════════════════════════════\n")

    print("══════════════ COURSE SECTION COUNTS ══════════════")
    counts: Dict[str, Dict[str, int]] = {}
    for s in sections.values():
        e = counts.setdefault(s.course_number, {"lec": 0, "lab": 0})
        e["lab" if s.is_lab else "lec"] += 1
    for course in sorted(counts):
        e = counts[course]
        lab_str = f"{e['lab']} lab(s)" if e["lab"] else "no labs"
        print(f"  {course}: {e['lec']} lecture(s), {lab_str}")
    print("════════════════════════════════════════════════════\n")

    grad = {sid: s for sid, s in sections.items() if is_grad(s.course_number) and s.start_time}
    if grad:
        print("══════════════ GRADUATE (5000+) TIMINGS ════════════")
        by_course: Dict[str, list] = {}
        for sid, s in grad.items():
            by_course.setdefault(s.course_number, []).append((sid, s))
        for course in sorted(by_course):
            print(f"  {course}:")
            for sid, s in sorted(by_course[course], key=lambda x: x[1].start_time):
                label = "LAB" if s.is_lab else "LEC"
                print(f"    {sid} [{label}] {''.join(s.days)} {s.start_time.strftime('%H:%M')}-{s.end_time.strftime('%H:%M')} | {s.faculty} | {s.room}")
        print("════════════════════════════════════════════════════\n")

    tba = [(sid, s) for sid, s in sections.items() if s.faculty == "TBA"]
    if tba:
        print("══════════════ TBA FACULTY SECTIONS ════════════════")
        for sid, s in sorted(tba):
            label = "LAB" if s.is_lab else "LEC"
            print(f"  {sid}: {s.course_number} [{label}] {''.join(s.days) or '-'} | {s.room}")
        print("════════════════════════════════════════════════════\n")

    print("══════════════ FACULTY ASSIGNMENTS ═════════════════")
    fac_map: Dict[str, list] = {}
    for sid, s in sections.items():
        fac_map.setdefault(s.faculty, []).append((sid, s))
    for fac in sorted(fac_map):
        sec_list = fac_map[fac]
        lec_count = sum(1 for _, s in sec_list if not s.is_lab)
        target = faculty_limits.get(fac)
        tgt_str = f", target={target}" if target is not None and fac != "TBA" else ""
        print(f"\n  {fac}: {len(sec_list)} section(s) [lec={lec_count}{tgt_str}]")
        for sid, s in sorted(sec_list, key=lambda x: (x[1].course_number, x[0])):
            label = "LAB" if s.is_lab else "LEC"
            days = "".join(s.days) if s.days else "-"
            start = s.start_time.strftime("%H:%M") if s.start_time else "-"
            end = s.end_time.strftime("%H:%M") if s.end_time else "-"
            print(f"    {sid} [{label}] {days} {start}-{end} | {s.room}")
    print("\n════════════════════════════════════════════════════\n")

    print("══════════════ SLOT UTILIZATION BY DAY ═════════════")
    for day in ALL_DAYS:
        slots = slot_load.get(day, {})
        if slots:
            print(f"  {day}:")
            for k, v in sorted(slots.items()):
                print(f"    {k}: {v} section(s)")
    print("════════════════════════════════════════════════════\n")


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def _run() -> None:
    base = os.path.dirname(os.path.abspath(__file__))
    def dp(name: str) -> str:
        return os.path.join(base, "data", name)

    courses        = load_courses(dp("course-list-Spring 27(Sheet1) (1).csv"))
    fac_prefs      = load_faculty_preferences(dp("prof_preferences.csv"))
    timeslots      = load_timeslots(dp("timings.csv"))
    faculty_limits = load_faculty_loads(dp("faculty_load.csv"))
    rooms          = load_rooms(dp("rooms.csv"))
    room_prefs     = load_room_preferences(dp("room_preferences.csv"))
    overlap_groups = load_non_overlap_groups(dp("non_overlap_groups.csv"))

    course_titles = {normalize(c.number): c.name for c in courses}
    sections      = build_sections(courses, fac_prefs)

    # Faculty, time, and room are now assigned jointly inside build_schedule.
    time_sched    = TimeSlotScheduler(timeslots)
    room_assigner = RoomAssigner(rooms, room_prefs)
    scheduled     = build_schedule(sections, fac_prefs, faculty_limits, time_sched, room_assigner,
                                    non_overlap_groups=overlap_groups)

    print_summary(scheduled, courses, faculty_limits, time_sched.slot_load)
    ConstraintChecker(fac_prefs).run_all(scheduled, faculty_limits)
    check_non_overlap_groups(scheduled, overlap_groups)
    export_json(scheduled, courses, os.path.join(base, "schedule.json"))
    export_csv(scheduled, course_titles, os.path.join(base, "schedule.csv"))


class _Tee(io.TextIOBase):
    """Write to multiple streams simultaneously."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            if not getattr(st, "closed", False):
                try:
                    st.write(s)
                    st.flush()
                except ValueError:
                    pass
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            if not getattr(st, "closed", False):
                try:
                    st.flush()
                except ValueError:
                    pass


def main() -> None:
    result_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result.txt")
    with open(result_path, "w", encoding="utf-8") as log:
        tee = _Tee(sys.stdout, log)
        with contextlib.redirect_stdout(tee):
            _run()


if __name__ == "__main__":
    main()
