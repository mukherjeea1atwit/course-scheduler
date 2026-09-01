"""
WIT Class Scheduler — CP-SAT engine.

A second engine alongside the greedy one in main.py. Everything outside the
assignment step is deliberately IDENTICAL to main.py: the same nine CSV loaders,
the same advisory (non-fatal) check_inputs, the same ConstraintChecker C1-C19,
the same non-overlap-group report, the same RoomAssigner, the same exporters.
Only the choice of (faculty, day pattern, time slot) changes: instead of a
greedy walk it is one CP-SAT model, solved to a staged objective.

Rooms are NOT decision variables. They are assigned afterwards by the existing
RoomAssigner, because with 76 rooms against a hard ceiling of
max_concurrent_sections simultaneous classes, rooms are never the binding
constraint and modelling them would multiply the model for nothing.

The model ALWAYS returns a schedule. Over-subscribed input -- the customer's
normal state -- degrades into TBA and UNPLACED sections rather than failing.

Outputs are named *_cpsat so a CP-SAT run can never overwrite the greedy
engine's schedule.json / schedule.csv / result.txt.
"""
import contextlib
import csv
import io
import itertools
import json
import math
import os
import random
import re
import sys
import time as time_mod
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

# Two-day courses keep the three canonical patterns the institution has always
# used. Three- and four-day patterns are *generated* (any 3 or 4 of the 5
# weekdays) rather than whitelisted, because the requirement is only "N days",
# not a specific combination — MWF, MTTh, TThF and TWF are all acceptable.
LECTURE_PATTERNS = [["M", "W"], ["T", "Th"], ["W", "F"]]
# Grad courses meet one night/week; F excluded — no evening slots exist on Friday
GRAD_SINGLE_DAY_PATTERNS = [["M"], ["T"], ["W"], ["Th"]]


def teaching_day_allowance(*day_lists) -> int:
    """How many distinct days a professor may teach, given the courses involved.

    MAX_TEACHING_DAYS normally, but a single course that itself meets more days
    than that raises the floor — otherwise a 5-day course would be unschedulable
    for anyone. The scheduler and the C4 checker both call this so they cannot
    drift apart."""
    needed = max((len(d) for d in day_lists if d), default=0)
    return max(MAX_TEACHING_DAYS, needed)


def _spread_score(pattern: List[str]) -> Tuple[int, int]:
    """Lower is better. Ranks a day pattern by how evenly it spreads across the
    week: first by the number of back-to-back day pairs it contains (MWF has
    none, MTTh has one), then by how early it sits, so ordering is stable."""
    idx = sorted(ALL_DAYS.index(d) for d in pattern)
    adjacent = sum(1 for a, b in zip(idx, idx[1:]) if b - a == 1)
    return adjacent, sum(idx)


def build_day_patterns(num_days: int) -> List[List[str]]:
    """All ways to pick `num_days` of the five weekdays, best-spread first.
    2-day courses keep the canonical MW / TTh / WF list instead, so existing
    COMP/DATA schedules are unchanged."""
    if num_days == 2:
        return [list(p) for p in LECTURE_PATTERNS]
    if num_days == 1:
        # Every weekday. GRAD_SINGLE_DAY_PATTERNS excludes Friday because no
        # evening slot exists then — that is a graduate-evening rule and must not
        # leak onto daytime once-a-week courses.
        return [[d] for d in ALL_DAYS]
    combos = itertools.combinations(ALL_DAYS, max(1, min(num_days, len(ALL_DAYS))))
    return [list(c) for c in sorted(combos, key=lambda c: _spread_score(list(c)))]


# days-per-week → ordered pool of day patterns. Built once at import.
DAY_PATTERNS: Dict[int, List[List[str]]] = {n: build_day_patterns(n) for n in range(1, 6)}

DEFAULT_LECTURE_DAYS = 2   # used when the course list leaves days/week blank or 0
GRAD_START_HR   = 18        # 6 PM — grad courses start at 18:00
GRAD_END_HR     = 19        # grad start window: 18:00 ≤ hour < 19
FACULTY_GAP_MIN = 15        # min gap between back-to-back classes for same faculty
# Max sections that may run at the same time (room/resource ceiling). Overridden
# at startup from data/settings.csv so it can be changed without touching code.
MAX_CONCURRENT  = 10
MAX_DAY_SPAN_HR = 9         # max hours between a faculty's first and last class
MAX_TEACHING_DAYS = 4       # max distinct days a professor teaches in a week (C4)
DEFAULT_FACULTY_LOAD = 3    # load assumed for a name absent from faculty_load.csv
RESERVED_START  = 12 * 60   # Tue/Thu 12:00 reserved (minutes from midnight)
RESERVED_END    = 13 * 60   # Tue/Thu 13:00 — ends at 1 PM so 1:00 PM slots are free
AM_CUTOFF_HR    = 12        # hours before this = AM
AM_TARGET_RATIO = 0.60      # 60 % of undergrad meetings should be AM
# Within the AM window and within the PM window, the earlier half of the available
# start hours should take ~60 % of that window's meetings — stops everything piling
# onto 8:00 (or 13:00) once a section has already been steered into a window.
WINDOW_EARLY_RATIO = 0.60

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
    days_per_week: int = 0  # days/week this lecture was supposed to meet (0 = unknown)
    forced: bool = False    # placed by the last-resort fallback; not a valid assignment


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


# Scheduling tiers, most-constrained first. Upper-level undergraduate courses
# (3000/4000) are the scarcest: they typically have one or two qualified
# professors and no substitutes, so they must claim faculty load and slots
# before anything else. Graduate courses come next — they all compete for the
# single 18:00 evening slot. Lower-level undergraduate courses (1000/2000) go
# last: they have the largest faculty pools and the most sections, so they are
# the easiest to place around whatever is already fixed.
LEVEL_UPPER_UG, LEVEL_GRAD, LEVEL_LOWER_UG = 0, 1, 2


def course_level_tier(course_number: str) -> int:
    """Scheduling tier for a course number — see LEVEL_* above."""
    m = re.search(r"(\d+)", course_number or "")
    num = int(m.group(1)) if m else 0
    if num >= 5000:
        return LEVEL_GRAD
    if num >= 3000:
        return LEVEL_UPPER_UG
    return LEVEL_LOWER_UG


def per_meeting_min(total_min: int, num_days: int) -> int:
    return (total_min + num_days - 1) // num_days


# "COMP1050-3" → ("COMP1050", "3");  "MATH-1500-1-LAB" → ("MATH-1500", "1")
# The course number itself may contain hyphens and slashes, so only the trailing
# "-<digits>" (optionally followed by "-LAB") is treated as the section number.
_SECTION_ID_RE = re.compile(r"^(?P<course>.+)-(?P<sec>\d+)(?P<lab>-LAB)?$", re.IGNORECASE)


def split_section_id(section_id: str) -> Tuple[str, str, bool]:
    """Split a section id into (course_number, section_number, is_lab)."""
    m = _SECTION_ID_RE.match(section_id or "")
    if not m:
        return (section_id or ""), "", (section_id or "").upper().endswith("-LAB")
    return m.group("course"), m.group("sec"), bool(m.group("lab"))


def subject_of(course_number: str) -> str:
    """Leading letters of a course number — 'MATH1500' → 'MATH'."""
    m = re.match(r"([A-Za-z]+)", course_number or "")
    return m.group(1).upper() if m else ""


@dataclass
class MeetingRule:
    """One row of the meeting-length table (data/meeting_patterns.csv).

    `subject` and `lecture_hours` may be blank/None meaning "any". The most
    specific matching rule wins, so a MATH-specific row beats a wildcard row and
    an hours-specific row beats an hours-blank one."""
    subject: Optional[str]
    days_per_week: int
    lecture_hours: Optional[int]
    meeting_minutes: int
    lab_minutes: int

    def matches(self, subject: str, days: int, hours: int) -> bool:
        if self.days_per_week != days:
            return False
        if self.subject and self.subject != subject:
            return False
        if self.lecture_hours is not None and self.lecture_hours != hours:
            return False
        return True

    @property
    def specificity(self) -> int:
        # A subject-specific rule outranks an hours-specific one, so the MATH
        # 2-day row (105 min) wins over the generic "2 days, 4 hours" row (80 min)
        # rather than tying with it.
        return (2 if self.subject else 0) + (1 if self.lecture_hours is not None else 0)


# Defaults, used when data/meeting_patterns.csv is absent. The 2-day rows
# reproduce the historical COMP/DATA behaviour exactly (3-2-4 → 90 min lecture +
# 105 min lab; 4-0-4 → 80 min, no lab).
DEFAULT_MEETING_RULES: List[MeetingRule] = [
    MeetingRule(None,   2, 3, 90,  105),
    MeetingRule(None,   2, 4, 80,  0),
    MeetingRule("MATH", 2, None, 105, 0),   # math meets 105 min T/Th
    MeetingRule(None,   3, None, 70,  0),   # 70 min M/W/F (or any 3 days)
    MeetingRule(None,   4, None, 70,  0),   # 70 min across 4 days
]

MEETING_RULES: List[MeetingRule] = list(DEFAULT_MEETING_RULES)

GRAD_MEETING_MIN = 155      # single 155-min evening session (18:00–20:35)


def meeting_rule_for(course_number: str, lecture_hours: int, num_days: int) -> Optional[MeetingRule]:
    """Most specific matching rule, or None if the table cannot express this
    (course, hours, days) combination."""
    subject = subject_of(course_number)
    matches = [r for r in MEETING_RULES if r.matches(subject, num_days, lecture_hours)]
    if not matches:
        return None
    best = max(r.specificity for r in matches)
    return [r for r in matches if r.specificity == best][-1]


def meeting_minutes(course_number: str, lecture_hours: int, num_days: int) -> int:
    """Minutes for ONE meeting of this course when it meets `num_days` a week."""
    if is_grad(course_number):
        return GRAD_MEETING_MIN
    rule = meeting_rule_for(course_number, lecture_hours, num_days)
    if rule:
        return rule.meeting_minutes
    # No table entry: fall back to spreading the nominal contact hours evenly.
    return per_meeting_min(max(lecture_hours, 1) * 60, max(num_days, 1))


def lab_minutes(course_number: str, lecture_hours: int, lab_hours: int, num_days: int) -> int:
    """Minutes for the single weekly lab meeting, or 0 if the course has no lab."""
    if lab_hours <= 0 or is_grad(course_number):
        return 0
    rule = meeting_rule_for(course_number, lecture_hours, num_days)
    if rule and rule.lab_minutes:
        return rule.lab_minutes
    return 105 if lab_hours >= 2 else lab_hours * 60


_DAYS_WARNED: set = set()


def lecture_days_for(course_number: str, lecture_days_per_week: int) -> int:
    """Days per week this course should meet, honoring the course list.

    Graduate courses are a single 155-minute evening session by institutional
    rule, so a declared value above 1 is overridden — silently honoring it would
    schedule three 155-minute meetings (7 h 45 min) for a 4-credit course and
    every constraint check would still pass.
    """
    raw = str(lecture_days_per_week if lecture_days_per_week is not None else "").strip()
    n = to_int(raw, default=-1)

    if is_grad(course_number):
        if n > 1 and course_number not in _DAYS_WARNED:
            _DAYS_WARNED.add(course_number)
            print(f"[WARN] {course_number}: course list says {n} days/week, but graduate "
                  f"courses meet one {GRAD_MEETING_MIN}-min evening session. Using 1 day.")
        return 1

    if n < 0 and raw and course_number not in _DAYS_WARNED:
        _DAYS_WARNED.add(course_number)
        print(f"[WARN] {course_number}: 'lecture days per week' is {raw!r}, which is not a "
              f"number. Using the default of {DEFAULT_LECTURE_DAYS}.")
    if n <= 0:
        return DEFAULT_LECTURE_DAYS
    if n > len(ALL_DAYS) and course_number not in _DAYS_WARNED:
        _DAYS_WARNED.add(course_number)
        print(f"[WARN] {course_number}: course list says {n} days/week but the week has "
              f"only {len(ALL_DAYS)} days. Using {len(ALL_DAYS)}.")
    return min(n, len(ALL_DAYS))


def overlaps_reserved(days: List[str], start: time, end: time) -> bool:
    """True if this block falls on Tue/Thu and overlaps the 12:00–13:30 reserved window."""
    if not any(d in ("T", "Th") for d in days):
        return False
    s, e = t2m(start), t2m(end)
    return not (e <= RESERVED_START or s >= RESERVED_END)


def times_conflict(s1: int, e1: int, s2: int, e2: int, gap: Optional[int] = None) -> bool:
    """True if two time ranges are closer than `gap` minutes. `gap` defaults to
    FACULTY_GAP_MIN read at call time, so data/settings.csv can change it."""
    g = FACULTY_GAP_MIN if gap is None else gap
    return not (e1 + g <= s2 or e2 + g <= s1)


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


def load_meeting_patterns(path: str) -> List[MeetingRule]:
    """Load the meeting-length table. Missing file → built-in defaults."""
    if not os.path.exists(path):
        return list(DEFAULT_MEETING_RULES)
    rules: List[MeetingRule] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            days = to_int(r.get("lecture_days_per_week", ""))
            if days <= 0:
                continue
            subj = (r.get("subject") or "").strip().upper()
            hours_raw = (r.get("lecture_hours") or "").strip()
            rules.append(MeetingRule(
                subject=subj if subj and subj != "*" else None,
                days_per_week=days,
                lecture_hours=int(hours_raw) if hours_raw.isdigit() else None,
                meeting_minutes=to_int(r.get("meeting_minutes", "")),
                lab_minutes=to_int(r.get("lab_minutes", "")),
            ))
    seen: Dict[Tuple, int] = {}
    for i, r in enumerate(rules, start=2):   # +2: header row, 1-indexed
        key = (r.subject, r.days_per_week, r.lecture_hours)
        if key in seen:
            print(f"[WARN] meeting_patterns.csv row {i} repeats "
                  f"subject={r.subject or '*'} days={r.days_per_week} "
                  f"hours={r.lecture_hours if r.lecture_hours is not None else '*'} "
                  f"(first seen row {seen[key]}); the later row wins.")
        seen[key] = i
    return rules or list(DEFAULT_MEETING_RULES)


# Built-in defaults, kept separate from the live globals so every load starts
# from a known state. Without this, a bad edit in the web UI leaves the previous
# run's value in force for the lifetime of the server process.
SETTING_DEFAULTS: Dict[str, object] = {
    "max_concurrent_sections": 10,
    "max_daily_span_hours": 9,
    "faculty_gap_minutes": 15,
    "max_teaching_days": 4,
    "default_faculty_load": 3,
    "am_target_ratio": 0.60,
}


def load_settings(path: str) -> Dict[str, str]:
    """Load key/value tunables from data/settings.csv into the module globals.

    Every load resets to SETTING_DEFAULTS first, so a value removed from the file
    reverts rather than persisting. Anything unusable is reported by name — a
    silently ignored setting is worse than no setting, because the number on
    screen no longer matches the number in force.
    """
    global MAX_CONCURRENT, MAX_DAY_SPAN_HR, FACULTY_GAP_MIN, AM_TARGET_RATIO
    global MAX_TEACHING_DAYS, DEFAULT_FACULTY_LOAD

    MAX_CONCURRENT       = int(SETTING_DEFAULTS["max_concurrent_sections"])
    MAX_DAY_SPAN_HR      = int(SETTING_DEFAULTS["max_daily_span_hours"])
    FACULTY_GAP_MIN      = int(SETTING_DEFAULTS["faculty_gap_minutes"])
    MAX_TEACHING_DAYS    = int(SETTING_DEFAULTS["max_teaching_days"])
    DEFAULT_FACULTY_LOAD = int(SETTING_DEFAULTS["default_faculty_load"])
    AM_TARGET_RATIO      = float(SETTING_DEFAULTS["am_target_ratio"])

    out: Dict[str, str] = {}
    if not os.path.exists(path):
        print(f"[INFO] {os.path.basename(path)} not found; using built-in defaults.")
        return out

    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            k = (r.get("setting") or "").strip()
            v = (r.get("value") or "").strip()
            if k:
                if k not in SETTING_DEFAULTS:
                    print(f"[WARN] settings.csv: unknown setting {k!r} — ignored. "
                          f"Valid names: {', '.join(sorted(SETTING_DEFAULTS))}")
                out[k] = v

    def _num(key: str, cast, lo, hi, current):
        if key not in out or out[key] == "":
            return current
        try:
            x = cast(float(out[key]))
        except (ValueError, TypeError):
            print(f"[WARN] settings.csv: {key}={out[key]!r} is not a number — "
                  f"using {current}.")
            return current
        if not (lo <= x <= hi):
            print(f"[WARN] settings.csv: {key}={x} is outside {lo}–{hi} — "
                  f"using {current}.")
            return current
        return x

    MAX_CONCURRENT       = _num("max_concurrent_sections", int, 1, 1000, MAX_CONCURRENT)
    MAX_DAY_SPAN_HR      = _num("max_daily_span_hours", int, 1, 24, MAX_DAY_SPAN_HR)
    FACULTY_GAP_MIN      = _num("faculty_gap_minutes", int, 0, 240, FACULTY_GAP_MIN)
    MAX_TEACHING_DAYS    = _num("max_teaching_days", int, 1, len(ALL_DAYS), MAX_TEACHING_DAYS)
    DEFAULT_FACULTY_LOAD = _num("default_faculty_load", int, 0, 20, DEFAULT_FACULTY_LOAD)
    AM_TARGET_RATIO      = _num("am_target_ratio", float, 0.0, 1.0, AM_TARGET_RATIO)
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


def load_faculty_time_prefs(path: str) -> Dict[str, str]:
    """Returns {faculty_name: "AM"|"PM"} for the optional `Time Preference` column.

    Blank or unrecognized values are omitted, i.e. no preference.
    """
    out: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            name = (r.get("Faculty") or "").strip()
            pref = (r.get("Time Preference") or "").strip().upper()
            if name and pref in ("AM", "PM"):
                out[name] = pref
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

        # The Type column of rooms.csv ("Lab", "Lecture" or "Both") was loaded and
        # then never consulted by any placement decision — a room marked
        # lecture-only could still be handed a lab. It is invisible while every
        # room is "Both", but it silently breaks the moment a room is restricted.
        def type_ok(room: Room) -> bool:
            t = (room.type or "").strip().lower()
            if not t or t == "both":
                return True
            return t == needed_type

        # The course list's own "Preferred Room" column was parsed into
        # Section.preferred_room and never used either, so filling it in had no
        # effect. It is the most specific statement of intent available, so it is
        # tried ahead of the ranked room_preferences.csv entries.
        #
        # Lectures only. The course list has a single "Preferred Room" cell per
        # course, which reads as the room the class meets in; room_preferences.csv
        # is the file that distinguishes a course's lecture room from its lab room
        # (it has a Type column), so honouring one unlabelled value for the lab too
        # would silently override a properly chosen lab room.
        if sec.preferred_room and not is_lab:
            want = (sec.preferred_room or "").strip()
            for room in self.rooms:
                if (room.name == want and type_ok(room) and room.capacity >= needed_capacity
                        and self.is_free(room.name, days, start, end)):
                    return room.name

        for pref in self.room_prefs.get(key, []):
            cap = pref.max_cap or needed_capacity
            for room in self.rooms:
                if (room.name == pref.location and type_ok(room) and room.capacity >= cap
                        and self.is_free(room.name, days, start, end)):
                    return room.name

        free_candidates = sorted(
            (r for r in self.rooms
             if type_ok(r) and r.capacity >= needed_capacity and self.is_free(r.name, days, start, end)),
            key=lambda r: r.capacity,
        )
        if free_candidates:
            return free_candidates[0].name

        # Last resort: overbook, but never on the wrong room type — a lecture in
        # an undersized room is a seating problem, a lab in a room with no lab
        # equipment is not a class at all.
        typed = [r for r in self.rooms if type_ok(r)]
        if typed:
            worst = min(typed, key=lambda r: r.capacity)
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
        # Forced/UNPLACED sections, kept apart from the real ledger below — see
        # book_placeholder() for why they must spread but must not consume the cap.
        self._placeholder_intervals: Dict[str, Dict[Tuple[int, int], int]] = {d: {} for d in ALL_DAYS}
        # Booked (start, end) pairs per day with a count each. Used for C11 so the
        # guard counts true time overlaps (e.g. a 17:15-18:45 lecture overlapping
        # an 18:00 grad session), exactly as the constraint validator does.
        #
        # Counting distinct pairs rather than one entry per section keeps this
        # O(number of slots in timings.csv) instead of O(sections scheduled so
        # far). The scan runs on every candidate slot of every placement attempt,
        # so the difference is the difference between linear and quadratic once a
        # term is large or heavily over-subscribed.
        self._day_intervals: Dict[str, Dict[Tuple[int, int], int]] = {d: {} for d in ALL_DAYS}

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
        prefer=None,
    ) -> Optional[TimeSlot]:
        candidates = self._eligible_slots(sec, min_duration, force_pm=force_pm, max_duration=max_duration)
        ordered = sorted(candidates, key=lambda t: (self._busyness(t, days), t.start.hour, t.start.minute))
        if prefer is not None:
            ordered.sort(key=prefer)

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

    def book_placeholder(self, days: List[str], slot: TimeSlot) -> None:
        """Record a forced (UNPLACED) section on a separate ledger.

        Placeholders need to spread across day patterns like real sections — 60
        unplaceable sections all landing on Monday is useless to the person who
        has to fix them by hand. But they must NOT consume the real
        max_concurrent_sections budget: a placeholder is a class the scheduler
        already declared it could not schedule, so letting it occupy one of the
        10 tracks would push a class that CAN be scheduled out of the timetable,
        and would make the C11 check report a number that counts classes that do
        not exist. Two ledgers keep both properties.
        """
        s, e = t2m(slot.start), t2m(slot.stop)
        for d in days:
            self._placeholder_intervals[d][(s, e)] = \
                self._placeholder_intervals[d].get((s, e), 0) + 1

    def placeholder_capacity_ok(self, days: List[str], slot: TimeSlot) -> bool:
        """Capacity test for the forced path: real sections plus placeholders
        already parked here. Used only to spread placeholders, never to admit
        or reject a real section."""
        s, e = t2m(slot.start), t2m(slot.stop)
        worst = 0
        for d in days:
            worst = max(worst, sum(n for (bs, be), n in self._placeholder_intervals[d].items()
                                   if not (e <= bs or be <= s)))
        return self._concurrency(days, slot) + worst < MAX_CONCURRENT

    def placeholder_pressure(self, days: List[str], slot: TimeSlot) -> int:
        """How crowded this slot is counting both ledgers — the tie-break used to
        pick the least-bad pattern when every pattern is already full."""
        s, e = t2m(slot.start), t2m(slot.stop)
        worst = 0
        for d in days:
            worst = max(worst, sum(n for (bs, be), n in self._placeholder_intervals[d].items()
                                   if not (e <= bs or be <= s)))
        return self._concurrency(days, slot) + worst

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

        # The `evening` column of timings.csv decides which slots belong to the
        # graduate evening timetable. It used to be parsed and then ignored, with
        # the split hard-coded to "starts at or after 18:00" instead — so marking
        # a slot evening=TRUE did nothing, and a shop that wanted a 17:15 evening
        # slot or a non-evening 18:00 slot had no way to say so. Start hour is
        # still honoured for grad courses (C12 requires an 18:00 start), but the
        # undergraduate exclusion now follows the column.
        if is_grad(sec.course_number):
            return [t for t in self.slots if GRAD_START_HR <= t.start.hour < GRAD_END_HR and dur_ok(t)]
        slots = [t for t in self.slots
                 if not t.evening and t.start.hour < GRAD_START_HR and dur_ok(t)]
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

    def _concurrency(self, days: List[str], slot: TimeSlot) -> int:
        """Sections already booked that overlap this slot, on the busiest of `days`.
        Counts true time overlaps, not just matching start times, so a
        17:15-18:45 lecture and an 18:00 grad session count against each other."""
        s, e = t2m(slot.start), t2m(slot.stop)
        worst = 0
        for d in days:
            worst = max(worst, sum(n for (bs, be), n in self._day_intervals[d].items()
                                   if not (e <= bs or be <= s)))
        return worst

    def _slot_capacity_ok(self, days: List[str], slot: TimeSlot) -> bool:
        return self._concurrency(days, slot) < MAX_CONCURRENT

    def _increment_load(self, days: List[str], slot: TimeSlot) -> None:
        key = self._slot_key(slot)
        s, e = t2m(slot.start), t2m(slot.stop)
        for d in days:
            self._slot_load[d][key] = self._slot_load[d].get(key, 0) + 1
            self._day_intervals[d][(s, e)] = self._day_intervals[d].get((s, e), 0) + 1

    def _busyness(self, slot: TimeSlot, days: List[str]) -> int:
        key = self._slot_key(slot)
        return max(self._slot_load[d].get(key, 0) for d in days)

    def _would_exceed_span(self, faculty: str, days: List[str], start: time, end: time) -> bool:
        """True if adding this block would push the faculty's teaching span past
        MAX_DAY_SPAN_HR on any day."""
        if faculty == "TBA":
            return False
        s, e = t2m(start), t2m(end)
        busy = self._faculty_busy.get(faculty, {})
        for d in days:
            existing = busy.get(d, [])
            all_times = existing + [(s, e)]
            span_hr = (max(e2 for _, e2 in all_times) - min(s2 for s2, _ in all_times)) / 60
            if span_hr > MAX_DAY_SPAN_HR:
                return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# SCHEDULER ORCHESTRATOR
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
# CP-SAT ENGINE — TUNABLES AND SHARED HELPERS
# ──────────────────────────────────────────────────────────────────────────────

# Wall-clock ceiling for the solve. Hardcoded on purpose and deliberately NOT a
# settings.csv key: it is an engineering guard rail, not a scheduling policy, and
# the person editing settings.csv has no way to judge what a safe value is. Eight
# minutes is far more than any dataset seen so far needs (all three finish in
# well under a second) while still bounding the worst case for a term nobody has
# tried yet. If the limit is reached with a feasible-but-unproven solution, that
# solution is USED and the console says so.
CPSAT_TIME_LIMIT_S = 480.0

# Eight parallel workers, and no random_seed anywhere in this file. The customer
# wants a different valid schedule from one run to the next, so run-to-run
# variation is a FEATURE here, not a bug to be pinned down.
CPSAT_WORKERS = 8

# Shuffle the order in which sections and their candidate placements are declared
# to the model. CP-SAT's search follows declaration order, so this is what
# actually produces a visibly different (and equally optimal) timetable on each
# run. Set to False to get a byte-stable model for debugging.
CPSAT_SHUFFLE_MODEL = True

# Lab sessions are 1 h 45 min in the 3-2-4 offering. Module level (the greedy
# engine had it nested inside build_schedule) because the model builder, the
# placeholder fallback and the diagnostics all need it.
LAB_MAX_MIN = 105


def _bool_of_sum(model, lits, name: str):
    """A BoolVar equal to the sum of `lits`, for literal sets that are already
    mutually exclusive (they all come from one AddExactlyOne). Used to collapse
    many combo literals onto one meaning — "this section meets on Tuesday at
    10:00" — so downstream constraints are written once per meaning instead of
    once per combination."""
    v = model.NewBoolVar(name)
    model.Add(v == sum(lits) if lits else v == 0)
    return v


def _and_lit(model, a, b, name: str):
    """A BoolVar equal to (a AND b), in three clauses.

    This is the reification the vendor prototype avoided by materialising the
    conjunction as a placement variable instead — which is how one rule turned
    into 23k–38k literals and millions of pairwise clauses. Reifying keeps the
    two variable families separate and channels them, which is the whole point of
    a decomposed encoding."""
    v = model.NewBoolVar(name)
    model.AddImplication(v, a)
    model.AddImplication(v, b)
    model.AddBoolOr([v, a.Not(), b.Not()])
    return v



def _patterns_for_section(sec: Section) -> List[List[str]]:
    """Ordered pool of legal day patterns for a section — the same pool the greedy
    engine uses, lifted to module level because the CP-SAT model needs it while
    *building* the model rather than while walking a search."""
    n = lecture_days_for(sec.course_number, sec.lecture_days_per_week)
    if is_grad(sec.course_number):
        # One evening, Mon–Thu. No 2-day fallback: a grad session is always
        # GRAD_MEETING_MIN, which always fits one evening slot, and a 2-day
        # placement would contradict the days/week on the section and fail C15.
        return [list(p) for p in GRAD_SINGLE_DAY_PATTERNS]
    return DAY_PATTERNS.get(n, DAY_PATTERNS[DEFAULT_LECTURE_DAYS])


def _legal_lecture_combos(
    sec: Section,
    timeslots: List[TimeSlot],
    per_day: int,
    patterns: List[List[str]],
) -> List[Tuple[Tuple[str, ...], TimeSlot]]:
    """Every (day-pattern, slot) pair that is legal for this lecture *in isolation*.

    Everything here is a property of the section alone — nothing depends on what
    any other section does — so it is resolved in Python once and becomes the
    domain of a single ExactlyOne. This is the decomposition the vendor prototype
    lacked: it materialised (faculty × pattern × slot × lab-day × lab-slot) tuples,
    multiplying these local facts by every faculty and lab choice and producing
    tens of thousands of placement literals that then had to be de-conflicted
    pairwise.

    Encoded here, by construction:
      • EXACT meeting duration (slot.duration_min == per_day, not ">="; the
        prototype passed only a minimum and put 18 sections in 105-minute blocks
        where meeting_patterns.csv requires 90)
      • graduate sessions start at 18:00 (C12)
      • undergraduates never occupy an `evening=TRUE` slot
      • each slot's `Days Allowed` column
      • the reserved Tue/Thu 12:00–13:00 hour
    """
    out: List[Tuple[Tuple[str, ...], TimeSlot]] = []
    grad = is_grad(sec.course_number)
    for slot in timeslots:
        if slot.duration_min != per_day:
            continue
        if grad:
            if not (GRAD_START_HR <= slot.start.hour < GRAD_END_HR):
                continue
        else:
            # The `evening` column decides the graduate timetable; the start-hour
            # test is the institutional 18:00 rule. Both are applied, exactly as
            # TimeSlotScheduler._eligible_slots does.
            if slot.evening or slot.start.hour >= GRAD_START_HR:
                continue
        for pat in patterns:
            if slot.days_allowed and not all(d in slot.days_allowed for d in pat):
                continue
            if overlaps_reserved(list(pat), slot.start, slot.stop):
                continue
            out.append((tuple(pat), slot))
    return out


def _legal_lab_combos(
    timeslots: List[TimeSlot],
    lab_min: int,
) -> List[Tuple[str, TimeSlot]]:
    """Every (day, slot) pair a lab meeting may take, ignoring its lecture.

    The lab's two couplings to its lecture — same start time, different day — are
    channelled in the model rather than enumerated here, which is what keeps this
    O(days × slots) instead of O(lecture placements × days × slots)."""
    out: List[Tuple[str, TimeSlot]] = []
    if lab_min <= 0:
        return out
    for slot in timeslots:
        # Labs are 105 min in the 3-2-4 offering; the lower bound comes from
        # meeting_patterns.csv, the upper bound is the institutional lab length.
        if not (lab_min <= slot.duration_min <= LAB_MAX_MIN):
            continue
        if slot.evening or slot.start.hour >= GRAD_START_HR:
            continue
        for d in (slot.days_allowed or ALL_DAYS):
            if d not in ALL_DAYS:
                continue
            if overlaps_reserved([d], slot.start, slot.stop):
                continue
            out.append((d, slot))
    return out


class _SecModel:
    """Per-section slice of the CP-SAT model: its domains and its variables.

    Keeping one object per section (rather than a pile of parallel dicts) is what
    makes the channelling readable — every constraint below reads `sm.day[d]`,
    `sm.start`, `sm.y[f]` and never has to re-derive which literals mean what."""

    __slots__ = ("sec", "n_days", "per_day", "lab_min", "grad", "combos",
                 "lab_combos", "cands", "sel", "lab_sel", "unpl", "start",
                 "labdur", "lab_end", "lab_end_gap", "lab_size_gap",
                 "day", "labday", "y", "sig", "topup_cands")

    def __init__(self, sec: Section, timeslots: List[TimeSlot]):
        self.sec = sec
        self.grad = is_grad(sec.course_number)
        self.n_days = lecture_days_for(sec.course_number, sec.lecture_days_per_week)
        self.per_day = meeting_minutes(sec.course_number, sec.lecture_hours, self.n_days)
        self.lab_min = lab_minutes(sec.course_number, sec.lecture_hours,
                                   sec.lab_hours, self.n_days)
        self.combos = _legal_lecture_combos(sec, timeslots, self.per_day,
                                            _patterns_for_section(sec))
        self.lab_combos = _legal_lab_combos(timeslots, self.lab_min)
        # C17 (lab starts with its lecture) is enforced by an equality on start
        # minutes, so a lecture start with no lab slot at the same clock time can
        # never be part of a solution. Dropping those combos up front shrinks the
        # domain instead of leaving the solver to discover it by propagation.
        if self.lab_min > 0:
            lab_starts = {t2m(s.start) for _d, s in self.lab_combos}
            self.combos = [c for c in self.combos if t2m(c[1].start) in lab_starts]
        self.cands: List[str] = []
        # Non-preferred professors this section may be handed as a FOUNDATION
        # top-up. Empty for everything except COMP1000 / COMP1050 / COMP2000.
        self.topup_cands: List[str] = []
        self.sel: List = []
        self.lab_sel: List = []
        self.unpl = None
        self.start = None
        self.labdur = None
        self.lab_end = None
        self.lab_end_gap = None
        self.lab_size_gap = None
        self.day: Dict[str, object] = {}
        self.labday: Dict[str, object] = {}
        self.y: Dict[str, object] = {}
        # (day, start_min, end_min) → literal. Used only by the non-overlap group
        # coverage constraints, which care about *when a section meets*, not about
        # which (pattern, slot) produced that meeting. Collapsing combos onto this
        # signature is what turns coverage from combo² clauses into signature²
        # clauses — roughly a 10× reduction here and the difference between a
        # readable model and the prototype's millions of pairwise clauses.
        self.sig: Dict[Tuple[str, int, int], object] = {}


def build_schedule_cpsat(
    sections: List[Section],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
    time_sched: TimeSlotScheduler,
    room_assigner: RoomAssigner,
    non_overlap_groups: Optional[Dict[str, List[str]]] = None,
    faculty_time_prefs: Optional[Dict[str, str]] = None,
) -> Dict[str, ScheduledSection]:
    """CP-SAT replacement for the greedy build_schedule().

    Same signature, same return type, same downstream contract — only the way a
    (faculty, day pattern, time slot) triple is *chosen* differs. Rooms are still
    assigned afterwards by RoomAssigner, exactly as the greedy engine does, on the
    deliberate grounds that with 76 rooms and a hard ceiling of
    max_concurrent_sections simultaneous classes, room supply is never the binding
    constraint and making rooms decision variables would multiply the model for
    nothing.

    The model ALWAYS returns a schedule. Every section carries an `unpl` escape
    literal with a dominating penalty, so an over-subscribed term (the customer's
    normal state: 71 sections against 52 declared faculty load) degrades into
    TBA/UNPLACED sections instead of an empty result.
    """
    from ortools.sat.python import cp_model     # imported lazily: the greedy
    # engine in main.py must keep working on a machine with no ortools installed.

    t_build0 = time_mod.perf_counter()

    faculty_time_prefs = faculty_time_prefs or {}
    non_overlap_groups = non_overlap_groups or {}
    timeslots = time_sched.slots

    def on_roster(fac: str) -> bool:
        """faculty_load.csv is the authoritative list of who teaches this term —
        a name that appears only in prof_preferences.csv may not be used. Same
        rule, same reason, as the greedy engine's on_roster()."""
        return faculty_limits.get(fac, 0) > 0

    def max_load(fac: str) -> int:
        return faculty_limits.get(fac, DEFAULT_FACULTY_LOAD)

    course_section_counts: Dict[str, int] = {}
    for s in sections:
        cn0 = normalize(s.course_number)
        course_section_counts[cn0] = course_section_counts.get(cn0, 0) + 1

    model = cp_model.CpModel()

    # ── per-section variables ────────────────────────────────────────────────
    sms: List[_SecModel] = [_SecModel(s, timeslots) for s in sections]

    # Declaration order is deliberately shuffled. CP-SAT explores in the order
    # variables were created, so a different order yields a different — equally
    # optimal — schedule on each run. The customer explicitly wants that variety;
    # the alternative lever (solver.parameters.random_seed) is left untouched on
    # purpose so the objective value stays reproducible even when the layout does
    # not. Set CPSAT_SHUFFLE_MODEL = False to get a byte-stable model.
    order = list(range(len(sms)))
    if CPSAT_SHUFFLE_MODEL:
        random.shuffle(order)

    all_faculty = sorted({f for f in faculty_limits if f != "TBA" and on_roster(f)})

    for i in order:
        sm = sms[i]
        sec = sm.sec
        sm.unpl = model.NewBoolVar(f"unpl[{sec.id}]")

        # ── lecture placement: exactly one legal (pattern, slot), or the escape.
        combos = list(sm.combos)
        if CPSAT_SHUFFLE_MODEL:
            random.shuffle(combos)
        sm.combos = combos
        sm.sel = [model.NewBoolVar(f"sel[{sec.id}][{k}]") for k in range(len(combos))]
        model.AddExactlyOne(sm.sel + [sm.unpl])

        # start minute of the lecture, 0 when the section is unplaced. The lab
        # channelling and every faculty interval read this one variable.
        sm.start = model.NewIntVar(0, 24 * 60, f"start[{sec.id}]")
        model.Add(sm.start == sum(v * t2m(c[1].start) for v, c in zip(sm.sel, combos)))

        # day occupancy, derived from the pattern. `sum` is safe as an equality
        # because AddExactlyOne above guarantees at most one sel is true.
        for d in ALL_DAYS:
            v = model.NewBoolVar(f"day[{sec.id}][{d}]")
            model.Add(v == sum(lit for lit, c in zip(sm.sel, combos) if d in c[0]))
            sm.day[d] = v

        # ── lab placement (C9 different day, C10 one day, C17 same start) ─────
        if sm.lab_min > 0:
            lab_combos = list(sm.lab_combos)
            if CPSAT_SHUFFLE_MODEL:
                random.shuffle(lab_combos)
            sm.lab_combos = lab_combos
            sm.lab_sel = [model.NewBoolVar(f"lab[{sec.id}][{k}]")
                          for k in range(len(lab_combos))]
            # The SAME escape literal, so a section that cannot seat its lab is
            # unplaced as a unit — a lab of a lecture that does not exist would
            # otherwise book a real room for a class nobody is teaching.
            model.AddExactlyOne(sm.lab_sel + [sm.unpl])
            # C17: lab starts at the same clock time as its lecture.
            model.Add(sum(v * t2m(c[1].start) for v, c in zip(sm.lab_sel, lab_combos))
                      == sm.start)
            sm.labdur = model.NewIntVar(0, LAB_MAX_MIN, f"labdur[{sec.id}]")
            model.Add(sm.labdur == sum(v * c[1].duration_min
                                       for v, c in zip(sm.lab_sel, lab_combos)))
            # OptionalIntervalVar wants AFFINE endpoints (one variable times a
            # coefficient plus a constant), and a lab's end is start + a variable
            # duration. Materialising the end and the gap-extended end/size once
            # per section keeps every interval below affine — and costs three
            # variables per lab section rather than three per (section, prof, day).
            sm.lab_end = model.NewIntVar(0, 24 * 60 + LAB_MAX_MIN, f"labend[{sec.id}]")
            model.Add(sm.lab_end == sm.start + sm.labdur)
            sm.lab_size_gap = model.NewIntVar(0, LAB_MAX_MIN + FACULTY_GAP_MIN,
                                              f"labsz[{sec.id}]")
            model.Add(sm.lab_size_gap == sm.labdur + FACULTY_GAP_MIN)
            sm.lab_end_gap = model.NewIntVar(0, 24 * 60 + LAB_MAX_MIN + FACULTY_GAP_MIN,
                                             f"labendg[{sec.id}]")
            model.Add(sm.lab_end_gap == sm.start + sm.lab_size_gap)
            for d in ALL_DAYS:
                v = model.NewBoolVar(f"labday[{sec.id}][{d}]")
                model.Add(v == sum(lit for lit, c in zip(sm.lab_sel, lab_combos)
                                   if c[0] == d))
                sm.labday[d] = v
                # C9: the lab never shares a day with its own lecture.
                model.Add(v + sm.day[d] <= 1)

        # ── faculty: preference rows are HARD, enforced by domain construction.
        # A section is simply never offered to a professor outside its row; the
        # only other value is TBA. (The foundation top-up runs after the solve,
        # as it does in the greedy engine, and is the one sanctioned exception.)
        seen: set = set()
        for f in fac_prefs.get(sec.course_number, []):
            if f not in seen and on_roster(f):
                seen.add(f)
                sm.cands.append(f)
        # The ONE sanctioned exception: a foundational course (CS1 / CS2 / Data
        # Structures — courses anyone can teach) may go to a professor outside
        # its preference row, but only to fill load that would otherwise go
        # unused. The greedy engine does this as a post-pass over leftover TBA
        # sections; here it is a candidate with a price tag (W_TOPUP below), set
        # so that a top-up is always worse than a preferred assignment and always
        # better than a TBA. Modelling it rather than bolting it on afterwards is
        # what makes the final TBA count provably optimal: a post-hoc pass can
        # only see the timetable the solver already froze, so whether it finds
        # the extra assignment depends on which of several equally-optimal
        # timetables came back — measured at 18 TBAs on one run and 19 on the
        # next from identical input.
        if normalize(sec.course_number) in FOUNDATION_COURSES:
            sm.topup_cands = [f for f in all_faculty if f not in seen]
        for f in sm.cands + sm.topup_cands + ["TBA"]:
            sm.y[f] = model.NewBoolVar(f"y[{sec.id}][{f}]")
        model.AddExactlyOne(list(sm.y.values()))
        # An unplaced section holds no real time, so it must hold no real
        # professor either — otherwise a named prof's day would be blocked by a
        # class the scheduler already declared it could not schedule.
        model.AddImplication(sm.unpl, sm.y["TBA"])

        # ── meeting signatures, for the non-overlap group coverage below ──────
        for lit, (pat, slot) in zip(sm.sel, combos):
            for d in pat:
                key = (d, t2m(slot.start), t2m(slot.stop))
                sm.sig.setdefault(key, []).append(lit)
        sm.sig = {k: _bool_of_sum(model, v, f"sig[{sec.id}]{k}")
                  for k, v in sm.sig.items()}

    # ── faculty load, C3, C18 ────────────────────────────────────────────────
    # One linear constraint per bucket. The prototype's failure mode was
    # enumerating pairs; every cap here is a single AddLinearConstraint over the
    # literals that share a bucket, which is O(assignments) to build in total.
    for f in all_faculty:
        lits = [sm.y[f] for sm in sms if f in sm.y]
        if lits:
            model.Add(sum(lits) <= max_load(f))                       # C1
            grad_lits = [sm.y[f] for sm in sms if f in sm.y and sm.grad]
            if grad_lits:
                model.Add(sum(grad_lits) <= 2)                        # C18

    by_course_fac: Dict[Tuple[str, str], List] = {}
    for sm in sms:
        for f in sm.cands + sm.topup_cands:
            by_course_fac.setdefault((f, sm.sec.course_number), []).append(sm.y[f])
    for lits in by_course_fac.values():
        if len(lits) > 2:
            model.Add(sum(lits) <= 2)                                 # C3

    # ── C4: distinct teaching days per professor ─────────────────────────────
    # teaching_day_allowance(), not the raw MAX_TEACHING_DAYS constant: a course
    # that itself meets more days than the cap raises the floor, otherwise a
    # 5-day course is infeasible for every named professor. (The prototype used
    # the raw constant and made exactly that mistake.) The allowance is a
    # variable here because it depends on which courses the professor ends up
    # with; it only ever moves above MAX_TEACHING_DAYS for a section whose own
    # day count exceeds it, which is rare and usually absent entirely.
    fday: Dict[Tuple[str, str], object] = {}
    for f in all_faculty:
        teaches = [sm for sm in sms if f in sm.y]
        if not teaches:
            continue
        for d in ALL_DAYS:
            fday[(f, d)] = model.NewBoolVar(f"fday[{f}][{d}]")
        for sm in teaches:
            for d in ALL_DAYS:
                # fday >= y AND day  (one clause each; no pairwise enumeration)
                model.AddBoolOr([fday[(f, d)], sm.y[f].Not(), sm.day[d].Not()])
                if sm.lab_min > 0:
                    model.AddBoolOr([fday[(f, d)], sm.y[f].Not(), sm.labday[d].Not()])
        long_courses = [sm for sm in teaches if sm.n_days > MAX_TEACHING_DAYS]
        if long_courses:
            allow = model.NewIntVar(MAX_TEACHING_DAYS, len(ALL_DAYS), f"allow[{f}]")
            for sm in long_courses:
                model.Add(allow >= sm.n_days).OnlyEnforceIf(sm.y[f])
            # The lower bound alone is not enough. `allow` is not in the
            # objective, so with only "allow >= n_days if assigned" the solver is
            # free to park it at 5 and hand this professor a fifth teaching day
            # even when it gives them none of the long courses — the raised
            # allowance leaked to everyone who was merely a CANDIDATE for one.
            # For a foundation course that is the entire roster, since every
            # professor is a top-up candidate. Bounding it above by what was
            # actually assigned pins it back to MAX_TEACHING_DAYS unless a long
            # course really landed here.
            model.Add(allow <= MAX_TEACHING_DAYS + sum(
                (sm.n_days - MAX_TEACHING_DAYS) * sm.y[f] for sm in long_courses))
            model.Add(sum(fday[(f, d)] for d in ALL_DAYS) <= allow)
        else:
            model.Add(sum(fday[(f, d)] for d in ALL_DAYS) <= MAX_TEACHING_DAYS)

    # ── professor double-booking incl. the 15-minute gap, and the 9 h span ────
    # AddNoOverlap over OPTIONAL intervals, one per (section, professor, day) —
    # not one 2-literal clause per illegal pair of placements. The gap is folded
    # into the interval length: times_conflict() says two blocks clash unless
    # end1 + gap <= start2, which is exactly "the intervals [s, e+gap) overlap",
    # so an interval of size (duration + gap) makes NoOverlap enforce the gap for
    # free. That is ~1 400 intervals and ~100 NoOverlap constraints here, against
    # the prototype's 3.5–10.6 MILLION pairwise clauses for the same rule.
    gap = FACULTY_GAP_MIN
    span_max = MAX_DAY_SPAN_HR * 60
    fac_intervals: Dict[Tuple[str, str], List] = {}
    for f in all_faculty:
        teaches = [sm for sm in sms if f in sm.y]
        if not teaches:
            continue
        for d in ALL_DAYS:
            # first/last teaching minute on this day, used for the C2 span check
            f_lo = model.NewIntVar(0, 24 * 60, f"lo[{f}][{d}]")
            f_hi = model.NewIntVar(0, 24 * 60, f"hi[{f}][{d}]")
            model.Add(f_hi - f_lo <= span_max)
            model.Add(f_hi >= f_lo)
            ivs = []
            for sm in teaches:
                pres = _and_lit(model, sm.y[f], sm.day[d], f"p[{sm.sec.id}][{f}][{d}]")
                ivs.append(model.NewOptionalIntervalVar(
                    sm.start, sm.per_day + gap, sm.start + sm.per_day + gap,
                    pres, f"iv[{sm.sec.id}][{f}][{d}]"))
                model.Add(f_lo <= sm.start).OnlyEnforceIf(pres)
                model.Add(f_hi >= sm.start + sm.per_day).OnlyEnforceIf(pres)
                if sm.lab_min > 0:
                    lpres = _and_lit(model, sm.y[f], sm.labday[d],
                                     f"lp[{sm.sec.id}][{f}][{d}]")
                    ivs.append(model.NewOptionalIntervalVar(
                        sm.start, sm.lab_size_gap, sm.lab_end_gap,
                        lpres, f"liv[{sm.sec.id}][{f}][{d}]"))
                    model.Add(f_lo <= sm.start).OnlyEnforceIf(lpres)
                    model.Add(f_hi >= sm.start + sm.labdur).OnlyEnforceIf(lpres)
            if len(ivs) > 1:
                model.AddNoOverlap(ivs)
            fac_intervals[(f, d)] = ivs

    # ── C11: at most max_concurrent_sections true time overlaps per day ──────
    # AddCumulative, one per weekday, over the same optional-interval idea with a
    # capacity instead of a no-overlap. Unplaced sections have every day literal
    # false, so a placeholder never consumes one of the 10 tracks — the property
    # validator check 14 tests, and the reason book_placeholder() exists in the
    # greedy engine.
    for d in ALL_DAYS:
        ivs, demands = [], []
        for sm in sms:
            ivs.append(model.NewOptionalIntervalVar(
                sm.start, sm.per_day, sm.start + sm.per_day, sm.day[d],
                f"cc[{sm.sec.id}][{d}]"))
            demands.append(1)
            if sm.lab_min > 0:
                ivs.append(model.NewOptionalIntervalVar(
                    sm.start, sm.labdur, sm.lab_end, sm.labday[d],
                    f"ccl[{sm.sec.id}][{d}]"))
                demands.append(1)
        model.AddCumulative(ivs, demands, MAX_CONCURRENT)

    # ── C14: at most 2 sections of one course at the same day-set and time ───
    # Keyed exactly the way ConstraintChecker._c14_time_dupes keys it (plain
    # course number, sorted day tuple, start, end) so the model and the checker
    # cannot disagree. The prototype never modelled C14 at all and its own
    # checker duly reported "✗ FAIL C14" on an "optimal" solution — a solver is
    # only as right as its constraint list.
    c14: Dict[Tuple, List] = {}
    for sm in sms:
        cn = sm.sec.course_number
        for lit, (pat, slot) in zip(sm.sel, sm.combos):
            c14.setdefault((cn, tuple(sorted(pat)), t2m(slot.start), t2m(slot.stop)),
                           []).append(lit)
        for lit, (day, slot) in zip(sm.lab_sel, sm.lab_combos):
            c14.setdefault((cn, (day,), t2m(slot.start), t2m(slot.stop)),
                           []).append(lit)
    for lits in c14.values():
        if len(lits) > 2:
            model.Add(sum(lits) <= 2)

    # ── non-overlap cohort groups (data/non_overlap_groups.csv) ──────────────
    # DIRECTIONAL by design, and deliberately not symmetric: within a group the
    # cohort-specific course (the one belonging to FEWER groups) must have every
    # one of its sections pairable with SOME section of the shared/gateway
    # course. The reverse is not required — a student in a clashing section of
    # the shared course simply takes another section of it. Both check_non_
    # overlap_groups() and the independent validator apply that same direction.
    course_to_secs: Dict[str, List[_SecModel]] = {}
    for sm in sms:
        course_to_secs.setdefault(normalize(sm.sec.course_number), []).append(sm)
    group_membership: Dict[str, int] = {}
    for courses_in_grp in non_overlap_groups.values():
        for c in courses_in_grp:
            group_membership[c] = group_membership.get(c, 0) + 1

    n_cov_constraints = 0
    for grp, courses_in_grp in sorted(non_overlap_groups.items()):
        present = [c for c in courses_in_grp if c in course_to_secs]
        for c1, c2 in itertools.combinations(sorted(present), 2):
            g1, g2 = group_membership.get(c1, 1), group_membership.get(c2, 1)
            if g1 != g2:
                c1_small = g1 < g2
            else:
                # Tie on group membership → fall back to section count, the same
                # tie-break the checker and the validator use.
                c1_small = (course_section_counts.get(c1, 1)
                            <= course_section_counts.get(c2, 1))
            small, big = (c1, c2) if c1_small else (c2, c1)
            for a in course_to_secs[small]:
                covers = []
                for b in course_to_secs[big]:
                    cov = model.NewBoolVar(f"cov[{grp}][{a.sec.id}][{b.sec.id}]")
                    # a covers b only if both are really on the timetable …
                    model.AddImplication(cov, a.unpl.Not())
                    model.AddImplication(cov, b.unpl.Not())
                    # … and none of their meetings collide. Expressed over
                    # meeting SIGNATURES, so this is a couple of dozen clauses
                    # per pair rather than combo × combo.
                    for (d1, s1, e1), la in a.sig.items():
                        for (d2, s2, e2), lb in b.sig.items():
                            if d1 == d2 and s1 < e2 and s2 < e1:
                                model.AddBoolOr([cov.Not(), la.Not(), lb.Not()])
                    covers.append(cov)
                # HARD, with `unpl` as the one relief valve. That is what keeps
                # this from becoming the prototype's AddExactlyOne-with-no-escape
                # trap: a cohort pairing that genuinely cannot be honoured makes
                # a section UNPLACED (visible, reported, top-weighted so the
                # solver exhausts every alternative first) instead of making the
                # whole model infeasible. It also avoids a separate objective
                # tier — which matters, because each extra tier multiplies the
                # dominating weights and a weight range spanning 10+ orders of
                # magnitude is what makes CP-SAT's LP bound useless and the
                # search grind for minutes on cosmetics.
                n_cov_constraints += 1
                model.AddBoolOr(covers + [a.unpl])

    # ── objective ────────────────────────────────────────────────────────────
    # Staged, using the dominating-weight trick: each tier's weight is set to
    # (maximum possible value of everything below it) + 1, so a tier is never
    # traded away for any amount of gain in a lower tier. Weights are derived
    # from real upper bounds rather than guessed round numbers, which keeps the
    # coefficients as small as the ordering allows.
    n_sec = len(sms)
    ug = [sm for sm in sms if not sm.grad]
    ug_meetings = sum(sm.n_days + (1 if sm.lab_min > 0 else 0) for sm in ug)
    all_meetings = sum(sm.n_days + (1 if sm.lab_min > 0 else 0) for sm in sms)

    soft_terms = []          # (weight, expression) pairs, tier 3
    soft_ub = 0

    # (3a) AM target ratio — AM_TARGET_RATIO of undergraduate MEETINGS before noon.
    am_target = int(math.ceil(AM_TARGET_RATIO * ug_meetings))
    am_expr = []
    for sm in ug:
        for lit, (pat, slot) in zip(sm.sel, sm.combos):
            if slot.start.hour < AM_CUTOFF_HR:
                am_expr.append(len(pat) * lit)
        for lit, (_d, slot) in zip(sm.lab_sel, sm.lab_combos):
            if slot.start.hour < AM_CUTOFF_HR:
                am_expr.append(lit)
    if am_expr or am_target:
        am_dev = model.NewIntVar(0, max(ug_meetings, am_target), "am_dev")
        model.Add(am_dev >= sum(am_expr) - am_target)
        model.Add(am_dev >= am_target - sum(am_expr))
        soft_terms.append((2, am_dev))
        soft_ub += 2 * max(ug_meetings, am_target)

    # (3b) per-faculty AM/PM preference from faculty_load.csv's Time Preference.
    ampm_viol = []
    for sm in sms:
        if sm.grad:
            continue        # grad courses are evening-only; no window to prefer
        pm = _bool_of_sum(model, [lit for lit, (_p, s) in zip(sm.sel, sm.combos)
                                  if s.start.hour >= AM_CUTOFF_HR],
                          f"pm[{sm.sec.id}]")
        for f in sm.cands + sm.topup_cands:
            want = faculty_time_prefs.get(f)
            if want not in ("AM", "PM"):
                continue
            v = model.NewBoolVar(f"tp[{sm.sec.id}][{f}]")
            if want == "AM":
                model.AddBoolOr([v, sm.y[f].Not(), pm.Not()])
            else:
                model.AddBoolOr([v, sm.y[f].Not(), pm])
            ampm_viol.append(v)
    if ampm_viol:
        soft_terms.append((4, sum(ampm_viol)))
        soft_ub += 4 * len(ampm_viol)

    # (3c) weekday balance (mirrors the C16 checker: ≤ 40 % from the mean).
    bal_dev = []
    for d in ALL_DAYS:
        cnt = []
        for sm in sms:
            cnt.append(sm.day[d])
            if sm.lab_min > 0:
                cnt.append(sm.labday[d])
        dev = model.NewIntVar(0, all_meetings, f"bal[{d}]")
        # 5·dev >= |5·count − total| avoids fractional arithmetic on the mean.
        model.Add(5 * dev >= 5 * sum(cnt) - all_meetings)
        model.Add(5 * dev >= all_meetings - 5 * sum(cnt))
        bal_dev.append(dev)
    if bal_dev:
        soft_terms.append((1, sum(bal_dev)))
        soft_ub += 1 * len(ALL_DAYS) * all_meetings

    # (3d) preference-row rank, weakest of all: earlier names in
    # prof_preferences.csv are nudged ahead, never at the cost of anything above.
    rank_terms, rank_ub = [], 0
    for sm in sms:
        for pos, f in enumerate(sm.cands):
            if pos:
                rank_terms.append(pos * sm.y[f])
        rank_ub += max([len(sm.cands) - 1, 0])
    if rank_terms:
        soft_terms.append((1, sum(rank_terms)))
        soft_ub += rank_ub

    # (2) faculty load shortfall — declared load that goes unused. Note this is
    # the same quantity as the TBA count up to a constant (every section is
    # either staffed or TBA, so Σ shortfall = supply − (N − TBA)); it is kept as
    # its own tier because the per-professor breakdown is what lets the fairness
    # term below spread the shortfall instead of stranding one person at zero.
    short_vars = []
    for f in all_faculty:
        lits = [sm.y[f] for sm in sms if f in sm.y]
        cap = max_load(f)
        sv = model.NewIntVar(0, cap, f"short[{f}]")
        model.Add(sv >= cap - sum(lits)) if lits else model.Add(sv == cap)
        short_vars.append(sv)
    load_ub = sum(max_load(f) for f in all_faculty)
    max_short = model.NewIntVar(0, max(load_ub, 1), "max_short")
    for sv in short_vars:
        model.Add(max_short >= sv)

    # Fairness (spread the unused load rather than stranding one professor at
    # zero) rides in the soft tier rather than getting a tier of its own. Every
    # extra tier multiplies the weights above it, and the objective's dynamic
    # range is the single biggest lever on how fast CP-SAT can prove anything:
    # an earlier revision with fairness and cohort coverage as separate tiers put
    # W_unplaced at 6.2e9, which left the LP bound so weak the solver spent five
    # minutes closing a gap worth a handful of soft-preference points.
    soft_terms.append((8, max_short))
    soft_ub += 8 * max(load_ub, 1)

    topup_lits = [sm.y[f] for sm in sms for f in sm.topup_cands]

    W_LOAD = soft_ub + 1                                     # (2) beats prefs
    lower_ub = W_LOAD * max(load_ub, 1) + soft_ub            # everything below
    # A foundation top-up must cost more than any amount of load-shortfall or
    # soft-preference gain (so it is never taken for cosmetics) and less than one
    # TBA (so it is always taken when it removes one). Its multiplier is the
    # number of top-up-eligible sections, not the section count, which keeps the
    # weight range — and therefore the quality of CP-SAT's LP bound — an order of
    # magnitude tighter than a blanket n_sec would.
    W_TOPUP = lower_ub + 1
    n_topup = max(len(topup_lits), 1)
    W_TBA = W_TOPUP * (n_topup + 1) + lower_ub + 1           # (1) beats top-up
    W_UNPL = W_TBA * (n_sec + 1) + W_TOPUP * (n_topup + 1) + lower_ub + 1

    obj = []
    obj.append(W_UNPL * sum(sm.unpl for sm in sms))
    obj.append(W_TBA * sum(sm.y["TBA"] for sm in sms))
    if topup_lits:
        obj.append(W_TOPUP * sum(topup_lits))
    if short_vars:
        obj.append(W_LOAD * sum(short_vars))
    for w, expr in soft_terms:
        obj.append(w * expr)
    model.Minimize(sum(obj))

    build_s = time_mod.perf_counter() - t_build0
    proto = model.Proto()
    n_vars, n_cons = len(proto.variables), len(proto.constraints)
    print("\n────────────────── CP-SAT MODEL ──────────────────")
    print(f"  sections               : {n_sec} "
          f"({sum(1 for sm in sms if sm.lab_min > 0)} with a lab, "
          f"{sum(1 for sm in sms if sm.grad)} graduate)")
    print(f"  variables              : {n_vars:,}")
    print(f"  constraints            : {n_cons:,}")
    print(f"  build time             : {build_s:.2f} s")
    print(f"  cohort coverage rules  : {n_cov_constraints}")
    print(f"  foundation top-up opts : {len(topup_lits)}")
    print(f"  objective weights      : unplaced={W_UNPL:,} TBA={W_TBA:,} "
          f"top-up={W_TOPUP:,} load-shortfall={W_LOAD:,} soft≤{soft_ub:,}")
    print(f"  time limit             : {CPSAT_TIME_LIMIT_S:.0f} s, "
          f"{CPSAT_WORKERS} workers, no random_seed (varied output is intended)")
    sys.stdout.flush()

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = CPSAT_TIME_LIMIT_S
    solver.parameters.num_workers = CPSAT_WORKERS
    # Stop once the remaining PROVABLE improvement is confined to the soft tier.
    # The tolerance is a tenth of the soft tier's own range, which is an order of
    # magnitude below W_LOAD — so reaching it is a proof that the
    # unplaced count, the TBA count and the faculty-load shortfall are ALL at
    # their true optimum, and that everything still on the table is a cosmetic
    # nudge to the AM/PM split or the weekday spread.
    #
    # This is worth a lot of wall clock. Measured on the customer's data: without
    # it the solver reached that point in ~20 s and then spent the remaining
    # 460 s closing a gap of 28 objective points out of 36 million — a schedule
    # nobody could tell apart. On a tool people re-run all day, eight minutes of
    # that is worse than the nudge is worth. The 480 s ceiling still applies on
    # top, for a dataset where even the primary tiers do not converge.
    #
    # Note the honesty cost: with a tolerance set, CP-SAT reports OPTIMAL when it
    # is optimal WITHIN the tolerance, so the console says so explicitly rather
    # than claiming an exact optimum.
    solver.parameters.absolute_gap_limit = max(2.0, soft_ub / 10.0)
    # random_seed is deliberately NOT set and the solver is deliberately NOT
    # forced single-threaded: the customer wants a different valid schedule from
    # one run to the next.
    t_solve0 = time_mod.perf_counter()
    status = solver.Solve(model)
    solve_s = time_mod.perf_counter() - t_solve0

    name = solver.StatusName(status)
    have_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    print(f"  solver status          : {name}")
    print(f"  solve time             : {solve_s:.2f} s")
    if status == cp_model.OPTIMAL:
        # The tolerance has to be compared against the NET cost of one extra
        # unit of each tier, not against a raw weight. Comparing it to W_LOAD
        # alone would be invalid: W_LOAD is only soft_ub + 1, so one unit of
        # load shortfall traded against the soft tier can net as little as 1,
        # which sits inside the tolerance. The argument that actually holds
        # routes through the TBA tier — one extra TBA costs at least
        # W_TOPUP + 1 net, and the load shortfall is a linear function of the
        # TBA count (see the shortfall construction above), so pinning TBA
        # exactly pins the shortfall too.
        net_tba = W_TOPUP + 1
        print(f"  → optimal for the staged objective above, to within "
              f"{solver.parameters.absolute_gap_limit:.0f} objective points. "
              f"One more unstaffed section would cost at least {net_tba:,} "
              f"objective points net, far outside that tolerance, so the "
              f"unplaced count, the TBA count and the load shortfall are exact. "
              f"Only soft preferences (AM/PM split, weekday spread, preference "
              f"rank) may have a little room left.")
    elif status == cp_model.FEASIBLE:
        # The prototype conflated this with INFEASIBLE and told the user their
        # data was impossible when the solver had merely run out of clock.
        print(f"  → TIME LIMIT ({CPSAT_TIME_LIMIT_S:.0f} s) reached with a feasible "
              f"but UNPROVEN solution. That solution is being used; it satisfies "
              f"every hard constraint, it is simply not certified best-possible.")
        print(f"    best objective {solver.ObjectiveValue():.0f}, "
              f"best bound {solver.BestObjectiveBound():.0f}")
    elif status == cp_model.UNKNOWN:
        print(f"  → TIME LIMIT ({CPSAT_TIME_LIMIT_S:.0f} s) reached with NO solution "
              f"found. This is NOT a proof that the inputs are impossible — the "
              f"search simply did not finish. Every section will be emitted as "
              f"UNPLACED so the run still produces a file you can work from.")
    elif status == cp_model.INFEASIBLE:
        print("  → PROVEN INFEASIBLE: no assignment satisfies the hard constraints. "
              "Every section will be emitted as UNPLACED.")
    else:
        print(f"  → solver returned {name}; every section will be emitted as UNPLACED.")
    print("──────────────────────────────────────────────────\n")
    sys.stdout.flush()

    # ── read the solution back ───────────────────────────────────────────────
    # placement[section_id] = (faculty, days, lec_slot, lab_day|None, lab_slot|None)
    placement: Dict[str, Tuple] = {}
    unplaced_ids: List[str] = []
    for sm in sms:
        sid = sm.sec.id
        if not have_solution or solver.Value(sm.unpl):
            unplaced_ids.append(sid)
            continue
        pick = next((c for lit, c in zip(sm.sel, sm.combos) if solver.Value(lit)), None)
        if pick is None:                     # defensive: should be impossible
            unplaced_ids.append(sid)
            continue
        pat, slot = pick
        lab_day = lab_slot = None
        if sm.lab_min > 0:
            lab_pick = next((c for lit, c in zip(sm.lab_sel, sm.lab_combos)
                             if solver.Value(lit)), None)
            if lab_pick is None:
                unplaced_ids.append(sid)
                continue
            lab_day, lab_slot = lab_pick
        fac = next((f for f, v in sm.y.items() if solver.Value(v)), "TBA")
        placement[sid] = (fac, list(pat), slot, lab_day, lab_slot)

    return _materialise_cpsat(
        sms, placement, unplaced_ids, fac_prefs, faculty_limits,
        time_sched, room_assigner, have_solution)


def _materialise_cpsat(
    sms: List["_SecModel"],
    placement: Dict[str, Tuple],
    unplaced_ids: List[str],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
    time_sched: TimeSlotScheduler,
    room_assigner: RoomAssigner,
    have_solution: bool,
) -> Dict[str, ScheduledSection]:
    """Turn the solver's answer into ScheduledSections: book faculty time, assign
    rooms post-hoc with the existing RoomAssigner, place the placeholders that
    could not be solved, and run the foundation top-up.

    Rooms are handled here rather than in the model on purpose (see
    build_schedule_cpsat). If no room can be found the section is marked
    UNPLACED, exactly as the greedy engine does."""
    by_id = {sm.sec.id: sm for sm in sms}
    lectures: Dict[str, ScheduledSection] = {}
    labs: List[ScheduledSection] = []
    unplaced_report: List[Tuple[str, str, str]] = []

    faculty_load: Dict[str, int] = {f: 0 for f in faculty_limits}
    fac_course_count: Dict[Tuple[str, str], int] = {}
    fac_grad_count: Dict[str, int] = {}
    faculty_days_map: Dict[str, set] = {}

    def _tally(fac: str, course_number: str, delta: int) -> None:
        key = (fac, course_number)
        fac_course_count[key] = fac_course_count.get(key, 0) + delta
        if is_grad(course_number):
            fac_grad_count[fac] = fac_grad_count.get(fac, 0) + delta

    # ── 1. commit everything the solver placed ───────────────────────────────
    # Deterministic order so a room's preferred occupant does not depend on dict
    # iteration order; the *schedule* still varies run to run, because the times
    # and faculty it is derived from do.
    for sid in sorted(placement, key=lambda s: (by_id[s].sec.course_number, s)):
        sm = by_id[sid]
        sec = sm.sec
        fac, days, slot, lab_day, lab_slot = placement[sid]

        room = room_assigner.find_and_book(sec, days, slot.start, slot.stop, is_lab=False)
        lab_room = None
        if lab_slot is not None:
            lab_room = room_assigner.find_and_book(sec, [lab_day], lab_slot.start,
                                                   lab_slot.stop, is_lab=True)
        # RoomAssigner returns None only when rooms.csv has no room of the
        # required Type at all — a data problem, not a scheduling one.
        if room is None or (lab_slot is not None and lab_room is None):
            reason = ("no room of the required Type exists in rooms.csv for this "
                      "section (check the Type column)")
            print(f"[CRITICAL] {sid} ({sec.course_number}): cannot be placed — {reason}.")
            unplaced_report.append((sid, sec.course_number, reason))
            unplaced_ids.append(sid)
            continue

        time_sched.book(fac, days, slot)
        if lab_slot is not None:
            time_sched.book(fac, [lab_day], lab_slot)
        faculty_load[fac] = faculty_load.get(fac, 0) + 1
        _tally(fac, sec.course_number, +1)
        faculty_days_map.setdefault(fac, set()).update(days)

        # `topup` marks a PREFERENCE EXCEPTION for C19 and for the independent
        # validator. Only a foundation course can reach this branch — topup_cands
        # is empty for everything else — so the exception stays exactly as narrow
        # as the rule that sanctions it.
        is_topup = fac in sm.topup_cands
        if is_topup:
            print(f"[TOPUP] {sid} ({sec.course_number}) → {fac} "
                  f"(PREFERENCE EXCEPTION — not on this course's preference row; "
                  f"taking a foundation section to reach their load; load now "
                  f"{faculty_load[fac]}/{faculty_limits.get(fac, DEFAULT_FACULTY_LOAD)})")
        lectures[sid] = ScheduledSection(
            section_id=sid, course_number=sec.course_number, course_name=sec.course_name,
            faculty=fac, room=room, days=list(days),
            start_time=slot.start, end_time=slot.stop,
            has_lab=sec.lab_hours > 0, is_lab=False,
            days_per_week=sm.n_days, forced=False, topup=is_topup,
        )
        if lab_slot is not None:
            faculty_days_map[fac].add(lab_day)
            labs.append(ScheduledSection(
                section_id=f"{sid}-LAB", course_number=sec.course_number,
                course_name=sec.course_name, faculty=fac, room=lab_room,
                days=[lab_day], start_time=lab_slot.start, end_time=lab_slot.stop,
                has_lab=False, is_lab=True, forced=False, topup=is_topup,
            ))

    # ── 2. placeholders for whatever could not be placed ─────────────────────
    # A placeholder holds no room and no concurrency track (book_placeholder,
    # not book) — the property validator checks 12 and 14 test, and the reason
    # the greedy engine keeps two ledgers.
    for sid in sorted(set(unplaced_ids)):
        sm = by_id[sid]
        sec = sm.sec
        if sid in lectures:
            continue
        if not have_solution:
            reason = ("the CP-SAT search returned no solution for this run; see the "
                      "solver status above")
        else:
            reason = _diagnose_cpsat_placement(sm, time_sched)
        print(f"[CRITICAL] {sid} ({sec.course_number}): cannot be placed — {reason}. "
              f"Forcing a placeholder; this section needs manual attention.")
        unplaced_report.append((sid, sec.course_number, reason))

        days_f, slot_f = _placeholder_slot(sm, time_sched)
        time_sched.book_placeholder(days_f, slot_f)
        lectures[sid] = ScheduledSection(
            section_id=sid, course_number=sec.course_number, course_name=sec.course_name,
            faculty="TBA", room="UNPLACED", days=list(days_f),
            start_time=slot_f.start, end_time=slot_f.stop,
            has_lab=sec.lab_hours > 0, is_lab=False,
            days_per_week=sm.n_days, forced=True,
        )
        if sm.lab_min > 0:
            lab_day, lab_slot = _placeholder_lab(sm, days_f, slot_f, time_sched)
            time_sched.book_placeholder([lab_day], lab_slot)
            unplaced_report.append((f"{sid}-LAB", sec.course_number,
                                    "its lecture could not be placed"))
            labs.append(ScheduledSection(
                section_id=f"{sid}-LAB", course_number=sec.course_number,
                course_name=sec.course_name, faculty="TBA", room="UNPLACED",
                days=[lab_day], start_time=lab_slot.start, end_time=lab_slot.stop,
                has_lab=False, is_lab=True, forced=True,
            ))

    # ── 3. explain every TBA in plain English, as the greedy engine does ─────
    for sid, lec in sorted(lectures.items()):
        if lec.faculty == "TBA" and not lec.forced:
            sm = by_id[sid]
            print(f"[WARN] {sid} ({lec.course_number}): "
                  f"{_diagnose_cpsat_staffing(sm, fac_prefs, faculty_limits, faculty_load, fac_course_count, fac_grad_count)}. "
                  f"Left as TBA.")

    lab_by_parent = {lab.section_id.replace("-LAB", ""): lab for lab in labs}

    # ── 4. foundation top-up ─────────────────────────────────────────────────
    _topup_underloaded_cpsat(lectures, lab_by_parent, fac_prefs, faculty_limits,
                             faculty_load, fac_course_count, fac_grad_count,
                             faculty_days_map, time_sched)

    if unplaced_report:
        print("\n── ⚠ SECTIONS THAT COULD NOT BE PLACED ──")
        for sid, cn, reason in unplaced_report:
            print(f"  {sid} ({cn}): {reason}")
        print(f"  {len(unplaced_report)} section(s) hold a placeholder slot and room "
              f"'UNPLACED'. Fix the inputs above and re-run.")
        print("────────────────────────────────────────\n")

    result: Dict[str, ScheduledSection] = {}
    for sid, s in lectures.items():
        result[sid] = s
        if sid in lab_by_parent:
            lab = lab_by_parent[sid]
            result[lab.section_id] = lab
    return result


def _placeholder_slot(sm: "_SecModel", time_sched: TimeSlotScheduler) -> Tuple[List[str], TimeSlot]:
    """Pick a day pattern and slot for a section the solver could not place.

    A placeholder is not a schedule — it exists so the section is visible in the
    UI and the exports instead of silently vanishing. It must still land on a
    legal day pattern of the right LENGTH (C15 checks that) and must spread
    across the week: sixty unplaceable sections all parked on Monday are no use
    to the person who has to fix them by hand."""
    patterns = _patterns_for_section(sm.sec)
    cands = [c[1] for c in sm.combos] or [s for s in time_sched.slots
                                          if s.duration_min == sm.per_day]
    if not cands:
        # Nothing of the right length exists at all — take the longest slot there
        # is, so the section still appears somewhere.
        cands = sorted(time_sched.slots, key=lambda s: -s.duration_min)[:1]
    best = None
    for pat in patterns:
        legal = [c for c in cands
                 if (not c.days_allowed or all(d in c.days_allowed for d in pat))
                 and not overlaps_reserved(pat, c.start, c.stop)]
        for slot in (legal or cands):
            pressure = time_sched.placeholder_pressure(pat, slot)
            if time_sched.placeholder_capacity_ok(pat, slot):
                return list(pat), slot
            if best is None or pressure < best[0]:
                best = (pressure, list(pat), slot)
    if best:
        return best[1], best[2]
    return list(patterns[0]), cands[0]


def _placeholder_lab(sm: "_SecModel", lec_days: List[str], lec_slot: TimeSlot,
                     time_sched: TimeSlotScheduler) -> Tuple[str, TimeSlot]:
    """Placeholder day/slot for the lab of a placeholder lecture. Keeps C9 (a
    different day) and C17 (the same start) where it can, so the placeholder at
    least looks like the class it stands in for."""
    same_start = [c for c in sm.lab_combos
                  if t2m(c[1].start) == t2m(lec_slot.start) and c[0] not in lec_days]
    pool = same_start or [c for c in sm.lab_combos if c[0] not in lec_days] or sm.lab_combos
    if not pool:
        day = next((d for d in ALL_DAYS if d not in lec_days), ALL_DAYS[0])
        return day, lec_slot
    return min(pool, key=lambda c: time_sched.placeholder_pressure([c[0]], c[1]))


def _diagnose_cpsat_placement(sm: "_SecModel", time_sched: TimeSlotScheduler) -> str:
    """Explain why a section has no legal placement, distinguishing a slot menu
    that has nothing of the right length from one that is simply full.

    Adapted from the greedy engine's _diagnose_placement: the questions a user
    asks ("is this a timings.csv problem or a capacity problem?") are the same,
    only the evidence differs — the solver's answer is global, so "the model
    could not fit this section anywhere" is the strongest statement available."""
    per_day = sm.per_day
    same_len = [s for s in time_sched.slots if s.duration_min == per_day]
    if not same_len:
        have = sorted({s.duration_min for s in time_sched.slots})
        return (f"TIMING — this course needs a {per_day}-minute meeting "
                f"({sm.n_days} day(s)/week), but timings.csv only offers "
                f"{', '.join(str(d) for d in have)} minute slots. Either add a "
                f"{per_day}-minute row to timings.csv, or change this course's "
                f"meeting length in meeting_patterns.csv")
    if sm.lab_min > 0 and not sm.lab_combos:
        return (f"TIMING — this course needs a {sm.lab_min}–{LAB_MAX_MIN} minute lab "
                f"slot and timings.csv offers none that a lab may use")
    if not sm.combos:
        return (f"TIMING — {per_day}-minute slots exist but none is legal for this "
                f"course once the reserved Tue/Thu 12:00-13:00 hour, each slot's "
                f"'Days Allowed' column"
                + (", and the requirement that its lab start at the same clock time"
                   if sm.lab_min > 0 else "")
                + " are applied")
    return (f"CAPACITY — {len(sm.combos)} legal (day pattern, slot) combination(s) "
            f"exist for this section, but the solver could not use any of them "
            f"without breaking a hard constraint: every one of them would push "
            f"some time past the {MAX_CONCURRENT}-section concurrency ceiling "
            f"(max_concurrent_sections in settings.csv) or collide with a rule "
            f"that cannot bend. Raise that ceiling if the rooms really exist, add "
            f"time slots, or reduce the section count")


def _diagnose_cpsat_staffing(sm: "_SecModel", fac_prefs, faculty_limits,
                             faculty_load, fac_course_count, fac_grad_count) -> str:
    """Explain in plain English why no professor could take this section.

    Same buckets as the greedy engine's _diagnose_staffing — a hiring problem, a
    course-load problem and a clock problem must not all read as "no faculty
    satisfied all constraints", because the fix is different in each case (add a
    name to prof_preferences.csv / raise a load in faculty_load.csv / add a time
    slot). The counts quoted are from the SOLVED schedule, so "at their full
    course load" means it in the answer the user is looking at."""
    sec = sm.sec
    listed = fac_prefs.get(sec.course_number, [])
    if not listed:
        return ("STAFFING — no faculty are listed for this course in "
                "prof_preferences.csv; add at least one name there")
    pool = [f for f in listed if faculty_limits.get(f, 0) > 0]
    if not pool:
        return (f"STAFFING — the name(s) listed for this course "
                f"({', '.join(listed)}) cannot be used: each is either missing "
                f"from faculty_load.csv or has a course load of 0")

    at_cap, at_course_cap, at_grad_cap, clash = [], [], [], []
    for f in pool:
        used = faculty_load.get(f, 0)
        cap = faculty_limits.get(f, DEFAULT_FACULTY_LOAD)
        if used >= cap:
            at_cap.append(f"{f} ({used}/{cap} courses)")
        elif fac_course_count.get((f, sec.course_number), 0) >= 2:
            at_course_cap.append(f)
        elif sm.grad and fac_grad_count.get(f, 0) >= 2:
            at_grad_cap.append(f)
        else:
            clash.append(f)

    parts = []
    if at_cap:
        parts.append(f"at their full course load — {', '.join(at_cap)}")
    if at_course_cap:
        parts.append(f"already teaching 2 sections of this course — {', '.join(at_course_cap)}")
    if at_grad_cap:
        parts.append(f"already teaching 2 graduate sections — {', '.join(at_grad_cap)}")
    if clash:
        parts.append(f"have load left but no day/time that fits alongside what they "
                     f"already teach (9-hour daily span, {MAX_TEACHING_DAYS} teaching "
                     f"days, {FACULTY_GAP_MIN}-min gap) — {', '.join(clash)}")

    who = (f"{pool[0]} is the only qualified professor and is"
           if len(pool) == 1 else
           f"all {len(pool)} qualified professors ({', '.join(pool)}) are")
    head = "STAFFING" if (at_cap or at_course_cap or at_grad_cap) else "FACULTY TIMING"
    msg = f"{head} — {who} unavailable: " + "; ".join(parts)
    if sm.topup_cands:
        # Without this line the message names only the preference row, which
        # reads as "list another name here" — the wrong fix for a foundation
        # course, where the solver was already free to hand the section to
        # anybody on the roster and still could not.
        msg += (f". This is a foundation course, so the top-up exception also "
                f"offered it to the {len(sm.topup_cands)} other professor(s) on "
                f"the roster and none of them had room either — the department is "
                f"simply out of teaching load")
    return msg


def _topup_underloaded_cpsat(
    lectures: Dict[str, ScheduledSection],
    lab_by_parent: Dict[str, ScheduledSection],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
    faculty_load: Dict[str, int],
    fac_course_count: Dict[Tuple[str, str], int],
    fac_grad_count: Dict[str, int],
    faculty_days_map: Dict[str, set],
    time_sched: TimeSlotScheduler,
) -> None:
    """Safety net: fill any prof still below their target load from leftover TBA
    sections, exactly as the greedy engine's _topup_underloaded does.

    Both of the greedy engine's passes are reproduced — pass 1 hands a leftover
    TBA section to a professor its preference row already names, pass 2 is the
    narrow FOUNDATION exception (CS1 / CS2 / Data Structures) flagged
    `topup=True` so C19 and the independent validator read it as a sanctioned
    exception rather than a preference violation.

    In the normal case this finds NOTHING, because both passes are already in the
    model: the solver minimises TBA count with the foundation exception as a
    priced candidate, so any assignment this pass could make, the solver already
    made — and made optimally rather than in whatever order the leftovers happen
    to be iterated. That is exactly why it moved into the model. Measured on the
    shipped data, the post-hoc-only version returned 18 TBAs on one run and 19 on
    the next from identical input, because it could only work with the timetable
    the solver had already frozen.

    It is kept for the case the model cannot cover: when the 480 s ceiling is hit
    the returned solution is feasible but not proven optimal, and this pass can
    still pick up an assignment the search had not reached. It only ever touches
    sections that are already placed — time, days and room do not change, so
    every room booking and the concurrency ceiling stay valid; only the
    professor's own calendar has to be re-checked."""
    def max_load(fac: str) -> int:
        return faculty_limits.get(fac, DEFAULT_FACULTY_LOAD)

    _pool_cache: List[List[str]] = []

    def underloaded() -> List[str]:
        if not _pool_cache:
            profs = [f for f in faculty_limits
                     if f != "TBA" and max_load(f) > 0
                     and faculty_load.get(f, 0) < max_load(f)]
            _pool_cache.append(sorted(profs, key=lambda f: faculty_load.get(f, 0) / max_load(f)))
        return _pool_cache[0]

    def feasible(fac: str, lec: ScheduledSection, lab: Optional[ScheduledSection]) -> bool:
        if faculty_load.get(fac, 0) >= max_load(fac):
            return False
        if fac_course_count.get((fac, lec.course_number), 0) >= 2:              # C3
            return False
        if is_grad(lec.course_number) and fac_grad_count.get(fac, 0) >= 2:      # C18
            return False
        new_days = set(faculty_days_map.get(fac, set())) | set(lec.days)
        if lab:
            new_days |= set(lab.days)
        if len(new_days) > teaching_day_allowance(lec.days, lab.days if lab else None):
            return False                                                       # C4
        if not time_sched._faculty_free(fac, lec.days, lec.start_time, lec.end_time):
            return False                                                       # gap
        if time_sched._would_exceed_span(fac, lec.days, lec.start_time, lec.end_time):
            return False                                                       # C2
        if lab:
            if not time_sched._faculty_free(fac, lab.days, lab.start_time, lab.end_time):
                return False
            if time_sched._would_exceed_span(fac, lab.days, lab.start_time, lab.end_time):
                return False
        return True

    def _assign(lec: ScheduledSection, fac: str, lab: Optional[ScheduledSection],
                note: str, exception: bool = True) -> None:
        previous = lec.faculty
        time_sched._block_faculty(fac, lec.days, lec.start_time, lec.end_time)
        faculty_days_map.setdefault(fac, set()).update(lec.days)
        lec.faculty, lec.topup = fac, exception
        if lab:
            time_sched._block_faculty(fac, lab.days, lab.start_time, lab.end_time)
            faculty_days_map[fac].update(lab.days)
            lab.faculty, lab.topup = fac, exception
        faculty_load[fac] = faculty_load.get(fac, 0) + 1

        def _tally(f: str, cn: str, delta: int) -> None:
            fac_course_count[(f, cn)] = fac_course_count.get((f, cn), 0) + delta
            if is_grad(cn):
                fac_grad_count[f] = fac_grad_count.get(f, 0) + delta
        _tally(previous, lec.course_number, -1)
        _tally(fac, lec.course_number, +1)
        _pool_cache.clear()
        print(f"[TOPUP] {lec.section_id} ({lec.course_number}) → {fac} "
              f"({note}; load now {faculty_load[fac]}/{max_load(fac)})")

    def _prefers(fac: str, lec: ScheduledSection) -> bool:
        return (faculty_limits.get(fac, 0) > 0
                and fac in fac_prefs.get(lec.course_number, []))

    # Pass 1 — PREFERRED top-up. The solver already maximises staffing, so this
    # normally finds nothing; it is kept because "nothing to do" is a property of
    # the data, not something to assume.
    for lec in [s for s in lectures.values()
                if s.faculty == "TBA" and not s.is_lab and not s.forced]:
        lab = lab_by_parent.get(lec.section_id)
        for fac in underloaded():
            if not _prefers(fac, lec) or not feasible(fac, lec, lab):
                continue
            _assign(lec, fac, lab, "was TBA; preferred", exception=False)
            break

    # Pass 2 — FOUNDATION exception, only after every preferred pairing is made.
    tba_foundation = [s for s in lectures.values()
                      if s.faculty == "TBA" and not s.is_lab and not s.forced
                      and normalize(s.course_number) in FOUNDATION_COURSES]
    for lec in tba_foundation:
        lab = lab_by_parent.get(lec.section_id)
        for fac in underloaded():
            if not feasible(fac, lec, lab):
                continue
            _assign(lec, fac, lab,
                    "PREFERENCE EXCEPTION — not on this course's preference row; "
                    "taking a leftover TBA foundation section to reach their load")
            break


def check_inputs(
    courses: List[Course],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
) -> bool:
    """Cross-check the input files against each other BEFORE scheduling.

    The scheduler joins three files on plain strings: the course list and
    prof_preferences.csv on course number, prof_preferences.csv and
    faculty_load.csv on faculty name. A join that misses produces no error — the
    course simply finds no eligible faculty and falls through to TBA, which looks
    identical to "we ran out of capacity". On the first real maths data set that
    hid 39 of 70 sections behind four typos (MATH1876/7 vs MATH1876/77 alone
    stranded 18). This reports those misses by name, before a schedule that
    cannot possibly be right gets built.
    """
    offered = {normalize(c.number): c for c in courses if c.sections > 0}
    pref_keys = {normalize(k) for k in fac_prefs}
    problems = 0

    print("\n────────────────── INPUT CHECK ──────────────────")

    # Offered courses nobody is listed to teach.
    missing = sorted((cn, c) for cn, c in offered.items()
                     if cn not in pref_keys or not fac_prefs.get(c.number))
    if missing:
        stranded = sum(c.sections for _, c in missing)
        print(f"  ⚠  {len(missing)} offered course(s) have no faculty listed in "
              f"prof_preferences.csv — {stranded} section(s) can only be TBA:")
        for cn, c in missing:
            near = [k for k in pref_keys
                    if k.startswith(cn[:6]) or cn.startswith(k[:6])]
            hint = f"  (did you mean {', '.join(sorted(near)[:3])}?)" if near else ""
            print(f"       {c.number:<14} {c.sections} section(s){hint}")
        problems += len(missing)

    # Preference rows for courses that are not offered — harmless but usually a typo.
    orphan_prefs = sorted(k for k in pref_keys if k not in {normalize(c.number) for c in courses})
    if orphan_prefs:
        print(f"  ·  {len(orphan_prefs)} preference row(s) name a course that is not in the "
              f"course list at all (ignored): {', '.join(orphan_prefs[:8])}"
              f"{' …' if len(orphan_prefs) > 8 else ''}")

    # Faculty named in preferences but absent from faculty_load.csv.
    named = {f for names in fac_prefs.values() for f in names}
    unknown = sorted(named - set(faculty_limits))
    if unknown:
        print(f"  ⚠  {len(unknown)} name(s) appear in prof_preferences.csv but not in "
              f"faculty_load.csv, so they CANNOT be assigned anything:")
        for n in unknown:
            near = [k for k in faculty_limits
                    if k and n and (k.lower()[:4] == n.lower()[:4])]
            hint = f"  (did you mean {', '.join(sorted(near)[:3])}?)" if near else ""
            print(f"       {n!r}{hint}")
        problems += len(unknown)

    # Faculty with a load who are never listed to teach anything.
    zero = sorted(f for f, load in faculty_limits.items() if load <= 0 and f != "TBA")
    if zero:
        print(f"  ·  {len(zero)} professor(s) have a load of 0 and will not be scheduled: "
              f"{', '.join(zero)}")

    idle = sorted(f for f, load in faculty_limits.items()
                  if load > 0 and f != "TBA" and f not in named)
    if idle:
        print(f"  ⚠  {len(idle)} professor(s) have a teaching load but are not named in any "
              f"preference row, so they can never be assigned: {', '.join(idle)}")
        problems += len(idle)

    # Capacity sanity check.
    demand = sum(c.sections for c in offered.values())
    supply = sum(l for f, l in faculty_limits.items() if f != "TBA")
    if demand > supply:
        print(f"  ⚠  {demand} section(s) offered but total faculty load is only {supply} — "
              f"at least {demand - supply} section(s) must be TBA.")
        problems += 1

    # Structural staffing check. A course can be impossible to staff even when
    # every name lines up: a professor may teach at most 2 sections of one course
    # (C3) and at most their declared load overall, so a course needs enough
    # qualified professors, not just one. COMP3100 (3 sections, Leon the only
    # qualified name, load 3) can never be fully staffed — but before this check
    # it just produced three silent TBAs that looked like a scheduler bug.
    for cn, course in sorted(offered.items()):
        listed = next((v for k, v in fac_prefs.items() if normalize(k) == cn), [])
        pool = [f for f in listed if faculty_limits.get(f, 0) > 0]
        if not pool:
            continue          # already reported above as "no faculty listed"
        capacity = sum(min(faculty_limits.get(f, 0), 2) for f in pool)
        if course.sections > capacity:
            problems += 1
            who = (f"{pool[0]}, its only qualified professor,"
                   if len(pool) == 1 else
                   f"its {len(pool)} qualified professors ({', '.join(pool)})")
            print(f"  ⚠  {course.number} needs {course.sections} section(s) but {who} "
                  f"can cover at most {capacity} "
                  f"(a professor may teach ≤ 2 sections of one course, and no more "
                  f"than their load in faculty_load.csv) — at least "
                  f"{course.sections - capacity} section(s) will be TBA. "
                  f"Add another qualified name in prof_preferences.csv, or reduce "
                  f"the section count.")

    if not problems:
        print("  ✓  Course numbers and faculty names line up across all input files.")
    print("─────────────────────────────────────────────────\n")
    return problems == 0


# ──────────────────────────────────────────────────────────────────────────────
# CONSTRAINT CHECKER
# ──────────────────────────────────────────────────────────────────────────────

class ConstraintChecker:
    """Validates a completed schedule against all scheduling constraints."""

    def __init__(self, fac_prefs: Optional[Dict[str, List[str]]] = None,
                 timeslots: Optional[List[TimeSlot]] = None):
        self.fac_prefs = fac_prefs or {}
        # Durations the timetable actually offers — C7 validates against these
        # instead of a hardcoded 75-95 min window, so adding a 70-minute math slot
        # to timings.csv does not make every math lecture look invalid.
        self.valid_durations = sorted({t.duration_min for t in (timeslots or [])})

    def run_all(
        self,
        sections: Dict[str, ScheduledSection],
        faculty_limits: Dict[str, int],
    ) -> bool:
        checks = [
            ("C1  Faculty course load matches limits",          self._c1_load),
            (f"C2  Faculty daily span ≤ {MAX_DAY_SPAN_HR} h",        self._c2_daily),
            ("C3  Faculty ≤ 2 sections of same course",         self._c3_duplicates),
            (f"C4  Faculty teaches ≤ {MAX_TEACHING_DAYS} days/week",  self._c4_days),
            ("C5  No blank faculty field",                      self._c5_assigned),
            ("C7  Lecture/lab duration matches an available slot", self._c7_durations),
            ("C9  Lab on different day than lecture",           self._c9_lab_day),
            ("C10 Lab is exactly one day",                      self._c10_lab_one_day),
            (f"C11 ≤ {MAX_CONCURRENT} concurrent sections per time slot", self._c11_concurrency),
            ("C12 Graduate courses start at 6 PM (18:00)",       self._c12_grad_time),
            ("C13 Same faculty for lecture and its lab",        self._c13_lab_faculty),
            ("C14 ≤ 2 sections of same course at same time",    self._c14_time_dupes),
            ("C15 Lecture meets its declared days/week",         self._c15_patterns),
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
                if span_hr > MAX_DAY_SPAN_HR:
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
        """A professor teaches at most MAX_TEACHING_DAYS distinct days — unless one
        of their own courses meets more days than that, in which case that course
        sets the floor. Uses the same helper the scheduler does."""
        fac_days: Dict[str, set] = {}
        fac_courses: Dict[str, List[List[str]]] = {}
        for s in sections.values():
            fac_days.setdefault(s.faculty, set()).update(s.days)
            fac_courses.setdefault(s.faculty, []).append(list(s.days))
        ok = True
        for fac, days in fac_days.items():
            if fac == "TBA":
                continue
            allowed = teaching_day_allowance(*fac_courses.get(fac, []))
            if len(days) > allowed:
                print(f"    {fac} teaches {len(days)} days ({','.join(sorted(days))}), "
                      f"allowed {allowed}")
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
            if self.valid_durations and dur not in self.valid_durations:
                print(f"    {sid}: {dur} min matches no slot in timings.csv "
                      f"(available: {', '.join(str(d) for d in self.valid_durations)})")
                ok = False
            elif s.is_lab and not (100 <= dur <= 110):
                print(f"    LAB {sid}: {dur} min (expect 105)")
                ok = False
            elif is_grad(s.course_number) and not (145 <= dur <= 165):
                print(f"    {sid}: {dur} min (grad, expect {GRAD_MEETING_MIN})")
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
            # Placeholders hold no room and no track (see book_placeholder), so
            # counting them here would report a concurrency figure that includes
            # classes the scheduler already declared unplaceable.
            if s.forced or s.room == "UNPLACED":
                continue
            for d in s.days:
                day_intervals[d].append((t2m(s.start_time), t2m(s.end_time)))

        ok = True
        for d, intervals in day_intervals.items():
            for s1, e1 in intervals:
                concurrent = sum(1 for s2, e2 in intervals if not (e1 <= s2 or e2 <= s1))
                if concurrent > MAX_CONCURRENT:
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
            # Skip placeholders, exactly as C11 does. An UNPLACED section holds an
            # invented time the scheduler already rejected, so counting it here
            # made the tool report that its own output broke a hard rule when the
            # real schedule was fine — three "sections at the same time" where two
            # of them are not actually scheduled at all.
            if s.forced or s.room == "UNPLACED":
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
        """Every lecture must meet exactly as many days a week as the course list
        says. Which days is deliberately not checked for 3+ day courses — MWF,
        MTTh, TThF and TWF are all acceptable. Two-day courses still have to use
        one of the three canonical patterns."""
        canonical_2day = [{"M", "W"}, {"T", "Th"}, {"W", "F"}]
        ok = True
        for sid, s in sections.items():
            if s.is_lab:
                continue
            if s.days_per_week and len(s.days) != s.days_per_week:
                print(f"    {sid}: meets {len(s.days)} day(s) {s.days}, "
                      f"course list says {s.days_per_week}")
                ok = False
            elif len(s.days) == 2 and set(s.days) not in canonical_2day:
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
    all_courses: Optional[List[Course]] = None,
) -> bool:
    """Best-effort verification for data/non_overlap_groups.csv.

    For every pair of courses within a group, we do NOT require every section
    of both courses to avoid each other — only the cohort-specific course
    (e.g. AAIN1000) needs every one of ITS sections to have a non-conflicting
    option in the other (shared/gateway) course, since that's the actual
    number of students affected. Sections of the shared course beyond what's
    needed are free to overlap. Reports failures as warnings; does not raise.

    Which course is "the cohort-specific one" is decided by how many *groups*
    each course belongs to, not by section count. A truly shared course (e.g.
    COMP1000) sits in several groups — one per cohort that needs it — while a
    cohort-specific course sits in just one. That's a structural fact about
    the curriculum, unlike section count, which can vary run to run for
    reasons that have nothing to do with which course is "the shared one"
    (e.g. the shared course happening to get only 1 section this term). Ties
    in group count fall back to section count, same idea as before.

    Also flags a related risk the coverage check alone can't see: if the SAME
    specific section of a shared course is the *only* non-conflicting option
    for two or more different pairings, every affected student across both
    pairings would be funneled into that one section — a capacity/overflow
    risk this tool can't rule out since it has no enrollment data, only timing.

    A group member that never made it onto the schedule is otherwise silently
    ignored (the group just shrinks). `all_courses` (the loaded course list)
    lets us tell the two ways that happens apart in the printed report: the
    course_number isn't in the course list at all (near-certainly a typo or a
    renamed course code, e.g. CYBR2500 vs COMP2500) vs. it's a real course
    with 0 sections this term (nothing to schedule, not a data error).
    """
    if not groups:
        return True

    # Placeholder sections are excluded. An UNPLACED section holds an invented
    # time the scheduler already rejected, so pairing against it produced lines
    # like "✓ PASS Y3_FALL_CY: COMP3100 / COMP3400 — all 3 COMP3100 sections have
    # a non-conflicting option" for three sections that have no real time at all.
    # A PASS asserted over fabricated data is worse than no check.
    by_course: Dict[str, List[ScheduledSection]] = {}
    placeholder_only: Dict[str, List[str]] = {}
    for s in sections.values():
        if s.is_lab:
            continue
        cn = normalize(s.course_number)
        if s.forced or s.room == "UNPLACED":
            placeholder_only.setdefault(cn, []).append(s.section_id)
        else:
            by_course.setdefault(cn, []).append(s)

    known_sections = {normalize(c.number): c.sections for c in (all_courses or [])}

    # How many distinct groups each course shows up in — the primary signal
    # for which side of a pairing is "the shared course" (many groups) vs.
    # "the cohort-specific course" (typically just one).
    group_membership: Dict[str, int] = {}
    for courses_in_grp in groups.values():
        for c in courses_in_grp:
            group_membership[c] = group_membership.get(c, 0) + 1

    # section_id → [(group, small_course, big_course), ...] it was the *sole*
    # non-conflicting option for, across all pairings — used for the overflow-
    # risk note below.
    sole_option_usage: Dict[str, List[Tuple[str, str, str]]] = {}

    print("\n── NON-OVERLAP GROUP CHECK (data/non_overlap_groups.csv) ──")
    all_ok = True
    for grp, courses in groups.items():
        present = [c for c in courses if c in by_course]
        missing = [c for c in courses if c not in by_course]
        for c in missing:
            if c in placeholder_only:
                print(f"  ⚠ SKIP  {grp}: {c} could not be placed "
                      f"({', '.join(placeholder_only[c])} are UNPLACED placeholders) — "
                      f"this group cannot be verified until those sections are fixed")
            elif c not in known_sections:
                print(f"  ⚠ SKIP  {grp}: {c} is not in the course list "
                      f"(check for a typo or renamed course number) — not constrained")
            else:
                print(f"  ⚠ SKIP  {grp}: {c} has 0 sections in the course list "
                      f"(not offered this term) — not constrained")
        if len(present) < 2:
            print(f"  ⚠ SKIP  {grp}: only {len(present)} of {len(courses)} course(s) "
                  f"are actually scheduled — this group has no effect")
            continue
        for i, c1 in enumerate(present):
            for c2 in present[i + 1:]:
                secs1, secs2 = by_course[c1], by_course[c2]
                g1, g2 = group_membership.get(c1, 1), group_membership.get(c2, 1)
                if g1 != g2:
                    # Fewer groups → more cohort-specific → this side needs full coverage.
                    c1_is_small = g1 < g2
                else:
                    # Tied on group membership: fall back to whichever has fewer
                    # scheduled sections, same heuristic as before.
                    c1_is_small = len(secs1) <= len(secs2)
                small, big = (secs1, secs2) if c1_is_small else (secs2, secs1)
                small_name, big_name = (c1, c2) if c1_is_small else (c2, c1)

                uncovered = []
                for a in small:
                    options = [
                        b for b in big
                        if not blocks_overlap(a.days, t2m(a.start_time), t2m(a.end_time),
                                               b.days, t2m(b.start_time), t2m(b.end_time))
                    ]
                    if not options:
                        uncovered.append(a.section_id)
                    elif len(options) == 1:
                        sole_option_usage.setdefault(options[0].section_id, []).append((grp, small_name, big_name))

                if not uncovered:
                    print(f"  ✓ PASS  {grp}: {small_name} / {big_name} — all {len(small)} "
                          f"{small_name} section(s) have a non-conflicting {big_name} option")
                else:
                    print(f"  ✗ FAIL  {grp}: {small_name} / {big_name} — {len(uncovered)} of "
                          f"{len(small)} {small_name} section(s) have no non-conflicting {big_name} "
                          f"option ({', '.join(uncovered)}); a student in {'those sections' if len(uncovered) > 1 else 'that section'} cannot also take {big_name}")
                    all_ok = False
    print("────────────────────────────────────────────────────────────\n")

    risk_lines = [
        f"  ⚠ {sec_id} is the only non-conflicting option for {len(usage)} different pairings "
        f"({'; '.join(f'{grp} ({small}→{big})' for grp, small, big in usage)}) — if each pairing has "
        f"real enrollment, they'd all need seats in this one section"
        for sec_id, usage in sole_option_usage.items() if len(usage) > 1
    ]
    if risk_lines:
        print("── OVERFLOW RISK (same section is the sole option for multiple pairings) ──")
        for line in risk_lines:
            print(line)
        print("This tool only checks timing, not enrollment — verify capacity by hand.")
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
                # True when the scheduler could not place this section legally and
                # fell back to a placeholder. The UI flags these the way it flags
                # TBA faculty, so they are visible rather than blending in.
                "unplaced": bool(s.forced or s.room == "UNPLACED"),
                # True when this section was filled by the foundation top-up — a
                # professor outside the course's preference row taking a leftover
                # TBA section of CS1/CS2/Data Structures to reach their load.
                # It is the one sanctioned exception to C19, and without it in the
                # export nothing downstream could tell a legitimate top-up apart
                # from a genuine preference violation.
                "topup": bool(s.topup),
            })

    with open(path, "w") as f:
        json.dump(events, f, indent=2)
    print(f"✓ Exported {len(events)} events → {path}")


def export_simple_csv(
    sections: Dict[str, ScheduledSection],
    path: str = "schedule_simple.csv",
) -> None:
    """The compact, human-readable export Mike asked for:

        Course Designation/Number, Type, Days, Times, Faculty

    Section and Room are appended after those five so that multiple sections of
    the same course stay distinguishable — without them, four sections of
    COMP1000 produce four identical rows.
    """
    def short_time(t: time) -> str:
        h = t.hour % 12 or 12
        return f"{h}" if t.minute == 0 else f"{h}:{t.minute:02d}"

    def section_number(sid: str) -> str:
        return split_section_id(sid)[1]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Course Designation/Number", "Type", "Days", "Times",
                    "Faculty", "Section", "Room", "Status"])
        for sid, s in sections.items():
            if not s.start_time:
                continue
            w.writerow([
                s.course_number,
                "Lab" if s.is_lab else "LEC",
                "".join(s.days),
                f"{short_time(s.start_time)}-{short_time(s.end_time)}",
                s.faculty or "",
                section_number(sid),
                s.room or "",
                "UNPLACED" if (s.forced or s.room == "UNPLACED") else "",
            ])
    print(f"✓ Exported simple CSV → {path}")


def export_csv(
    sections: Dict[str, ScheduledSection],
    course_titles: Dict[str, str],
    path: str = "schedule.csv",
) -> None:
    def split_subj_crse(num: str) -> Tuple[str, str]:
        """('MATH1876/77') → ('MATH', '1876/77'). Everything after the leading
        letters is the course code — taking only the first run of digits dropped
        the '/77' from every cross-listed section."""
        s = (num or "").strip()
        m = re.match(r"([A-Za-z]+)[\s-]*(.*)$", s)
        if not m:
            return "", s
        return m.group(1), m.group(2)

    def section_label(sid: str) -> str:
        _course, sec, is_lab = split_section_id(sid)
        if not sec:
            return ""
        return f"{sec}L" if is_lab else sec

    def fmt_time(t: time) -> str:
        h = t.hour % 12 or 12
        return f"{h:02d}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["CRN", "Subj", "Crse", "Section", "Location", "Credit",
                    "Title", "Days", "Time", "Cap.", "Act.", "Rem", "Instructor",
                    "Date (MM/DD)", "Status"])
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
                # Trailing column so existing importers that read by position are
                # unaffected. A forced placement is not a real assignment and must
                # not look like one in the file people import.
                "UNPLACED" if (s.forced or s.room == "UNPLACED") else "",
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

    global MEETING_RULES
    load_settings(dp("settings.csv"))
    MEETING_RULES  = load_meeting_patterns(dp("meeting_patterns.csv"))

    courses        = load_courses(dp("course-list-Spring 27(Sheet1) (1).csv"))
    fac_prefs      = load_faculty_preferences(dp("prof_preferences.csv"))
    timeslots      = load_timeslots(dp("timings.csv"))
    faculty_limits = load_faculty_loads(dp("faculty_load.csv"))
    faculty_tprefs = load_faculty_time_prefs(dp("faculty_load.csv"))
    rooms          = load_rooms(dp("rooms.csv"))
    room_prefs     = load_room_preferences(dp("room_preferences.csv"))
    overlap_groups = load_non_overlap_groups(dp("non_overlap_groups.csv"))

    # Nothing downstream can work without at least one time slot: every placement
    # path, including the last-resort placeholder, ends by picking a slot out of
    # this list, so an empty timings.csv surfaced as a bare IndexError deep in the
    # scheduler with no output files and no clue what was wrong. Say it plainly
    # instead — this is an input problem with an obvious fix.
    if not timeslots:
        raise SystemExit(
            "[ERROR] timings.csv contains no usable time slots, so no class can be "
            "scheduled at all. Add at least one row (start_time, stop_time, "
            "duration_min, slot_label, evening, Days Allowed), or restore the file "
            "from data-defaults/timings.csv."
        )
    if not rooms:
        raise SystemExit(
            "[ERROR] rooms.csv contains no rooms, so no class can be given a room. "
            "Add at least one row, or restore the file from data-defaults/rooms.csv."
        )

    check_inputs(courses, fac_prefs, faculty_limits)

    course_titles = {normalize(c.number): c.name for c in courses}
    sections      = build_sections(courses, fac_prefs)

    # Faculty and time are chosen by the CP-SAT model; rooms are assigned inside
    # it, post-hoc, by the same RoomAssigner the greedy engine uses.
    time_sched    = TimeSlotScheduler(timeslots)
    room_assigner = RoomAssigner(rooms, room_prefs)
    scheduled     = build_schedule_cpsat(sections, fac_prefs, faculty_limits,
                                         time_sched, room_assigner,
                                         non_overlap_groups=overlap_groups,
                                         faculty_time_prefs=faculty_tprefs)

    print_summary(scheduled, courses, faculty_limits, time_sched.slot_load)
    ConstraintChecker(fac_prefs, timeslots).run_all(scheduled, faculty_limits)
    check_non_overlap_groups(scheduled, overlap_groups, courses)
    # Distinct filenames, so a CP-SAT run can never clobber the greedy engine's
    # output. The JSON keeps the exact shape schedule.json has (flat list, one
    # event per section per day) because the web UI and the independent validator
    # both read that shape.
    export_json(scheduled, courses, os.path.join(base, "schedule_cpsat.json"))
    export_csv(scheduled, course_titles, os.path.join(base, "schedule_cpsat.csv"))
    export_simple_csv(scheduled, os.path.join(base, "schedule_cpsat_simple.csv"))


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
    result_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result_cpsat.txt")
    with open(result_path, "w", encoding="utf-8") as log:
        tee = _Tee(sys.stdout, log)
        with contextlib.redirect_stdout(tee):
            _run()


if __name__ == "__main__":
    main()
