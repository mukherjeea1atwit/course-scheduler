"""
WIT Class Scheduler
Assigns faculty, rooms, and time slots to course sections
subject to scheduling constraints.
"""
import contextlib
import csv
import io
import itertools
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

def build_schedule(
    sections: List[Section],
    fac_prefs: Dict[str, List[str]],
    faculty_limits: Dict[str, int],
    time_sched: TimeSlotScheduler,
    room_assigner: RoomAssigner,
    non_overlap_groups: Optional[Dict[str, List[str]]] = None,
    faculty_time_prefs: Optional[Dict[str, str]] = None,
) -> Dict[str, ScheduledSection]:
    """
    Jointly assigns faculty + day pattern + time slot + room for each section so that
    C2 (daily span), C4 (days/week), C11 (concurrency), and C16 (day balance) are
    all satisfied during assignment rather than flagged after the fact.
    """
    lectures: Dict[str, ScheduledSection] = {}
    labs: List[ScheduledSection] = []
    # Sections the scheduler could not legally place; surfaced at the end of the run.
    unplaced: List[Tuple[str, str, str]] = []

    # ── non-overlap groups (data/non_overlap_groups.csv) ───────────────────────
    # A group is a cohort of courses the same students take simultaneously, e.g.
    # Y1_FALL_AI = {COMP1000, AAIN1000}. We never require *every* section of a
    # shared/gateway course (COMP1000) to dodge every section of a smaller,
    # cohort-specific course (AAIN1000) — only enough of them to give each
    # AAIN1000 section a non-conflicting COMP1000 option, since that's the
    # number of students actually affected. A course can sit in several groups
    # at once (COMP1000 also pairs with COMP1100 for Cyber, COMP1010 for IT,
    # etc.) — group_reps and course_claimed_sections are shared across all of
    # them so two different pairings don't both quietly reserve the *same*
    # physical section and stack all of their students into it.
    non_overlap_groups = non_overlap_groups or {}
    course_to_groups: Dict[str, List[str]] = {}
    for grp, courses_in_grp in non_overlap_groups.items():
        for c in courses_in_grp:
            course_to_groups.setdefault(c, []).append(grp)

    # How many sections of `cn` should try to stay clear of the rest of `grp`:
    # the most sections any single groupmate has, since a groupmate with K
    # sections may need up to K distinct non-conflicting times to cover all of
    # them. Uses the actual section list being scheduled, not the nominal
    # course-list count, so it tracks what's really being placed.
    course_section_counts: Dict[str, int] = {}
    for s in sections:
        cn0 = normalize(s.course_number)
        course_section_counts[cn0] = course_section_counts.get(cn0, 0) + 1

    def _group_target(grp: str, cn: str) -> int:
        partners = [normalize(c) for c in non_overlap_groups.get(grp, []) if normalize(c) != cn]
        return max((course_section_counts.get(p, 1) or 1 for p in partners), default=1)

    # group → {course → [(days_frozenset, start_min, end_min, section_id), ...]},
    # capped at _group_target(grp, course) entries.
    group_reps: Dict[str, Dict[str, List[Tuple[frozenset, int, int, str]]]] = {}
    # course → section_ids already used as *someone's* rep, in any group — lets
    # a still-unsatisfied pairing prefer a section nobody else has claimed yet.
    course_claimed_sections: Dict[str, set] = {}

    def _group_bias(course_number: str, section_id: str, days: List[str], start_min: int, end_min: int) -> int:
        """0 = ideal (this course still needs reps in some group and this slot is
        clear and unclaimed), 1 = clear but a section already claimed by a
        different pairing (mild — risks stacking two pairings on one section),
        2 = clashes with a groupmate's established rep (real conflict risk).
        Purely an ordering preference; groups whose course already hit its
        target are skipped, so it never fights other placed courses forever."""
        cn = normalize(course_number)
        penalty = 0
        for grp in course_to_groups.get(cn, []):
            reps = group_reps.get(grp, {}).get(cn, [])
            if len(reps) >= _group_target(grp, cn):
                continue  # this course already has enough reps for this group
            for other_cn, other_reps in group_reps.get(grp, {}).items():
                if other_cn == cn:
                    continue
                if any(blocks_overlap(days, start_min, end_min, list(o_days), o_s, o_e)
                       for (o_days, o_s, o_e, _oid) in other_reps):
                    penalty = 2
            if penalty < 2 and section_id in course_claimed_sections.get(cn, set()):
                penalty = max(penalty, 1)
        return penalty

    def _record_group_rep(course_number: str, section_id: str, days: List[str], start_min: int, end_min: int) -> None:
        cn = normalize(course_number)
        for grp in course_to_groups.get(cn, []):
            reps = group_reps.setdefault(grp, {}).setdefault(cn, [])
            if len(reps) >= _group_target(grp, cn):
                continue
            conflict = any(
                other_cn != cn and any(
                    blocks_overlap(days, start_min, end_min, list(o_days), o_s, o_e)
                    for (o_days, o_s, o_e, _oid) in other_reps
                )
                for other_cn, other_reps in group_reps[grp].items()
            )
            if not conflict:
                reps.append((frozenset(days), start_min, end_min, section_id))
                course_claimed_sections.setdefault(cn, set()).add(section_id)

    # ── integrated state ───────────────────────────────────────────────────────
    faculty_load: Dict[str, int] = {f: 0 for f in faculty_limits}
    # Running tallies for C3 (≤2 sections of a course per prof) and C18 (≤2 grad
    # sections per prof). Kept incrementally because the alternative — rescanning
    # every lecture placed so far — is O(N) inside a loop that already runs
    # O(N × faculty) times, which is the dominant quadratic term at scale.
    fac_course_count: Dict[Tuple[str, str], int] = {}
    fac_grad_count: Dict[str, int] = {}
    # (course, days, start, end) → how many sections already sit there. C14 caps
    # this at 2: three sections of one course at the same hour on the same days
    # means no student with a clash has a third option. It was validated after
    # the run but never enforced during it, so the checker could only report it.
    course_time_count: Dict[Tuple[str, Tuple[str, ...], int, int], int] = {}

    def _time_key(course_number: str, days: List[str], slot: "TimeSlot"):
        return (course_number, tuple(sorted(days)), t2m(slot.start), t2m(slot.stop))

    def course_time_ok(sec_course: str, days: List[str], slot: "TimeSlot") -> bool:
        return course_time_count.get(_time_key(sec_course, days, slot), 0) < 2

    def _tally(fac: str, course_number: str, delta: int) -> None:
        key = (fac, course_number)
        fac_course_count[key] = fac_course_count.get(key, 0) + delta
        if is_grad(course_number):
            fac_grad_count[fac] = fac_grad_count.get(fac, 0) + delta
    faculty_days_map: Dict[str, set] = {}   # {faculty → set of days they teach}
    day_count: Dict[str, int] = {d: 0 for d in ALL_DAYS}  # sections per day (C16)

    # AM/PM balance (undergrad only)
    total_ug = sum(
        1 + (1 if s.lab_hours > 0 else 0)
        for s in sections if not is_grad(s.course_number)
    )
    max_am = math.ceil(AM_TARGET_RATIO * total_ug)
    am_used = 0

    # Intra-window (early/late) balance, undergrad only. The boundary is derived from
    # the start hours timings.csv actually offers rather than hardcoded, so it stays
    # correct if the slot menu changes: the distinct undergrad start hours in each
    # window are sorted and the first half (rounded down, min 1) counts as "early".
    faculty_time_prefs = faculty_time_prefs or {}
    _ug_hours = sorted({t.start.hour for t in time_sched.slots if t.start.hour < GRAD_START_HR})
    _am_hours = [h for h in _ug_hours if h < AM_CUTOFF_HR]
    _pm_hours = [h for h in _ug_hours if h >= AM_CUTOFF_HR]
    early_hours = set(_am_hours[:max(1, len(_am_hours) // 2)] + _pm_hours[:max(1, len(_pm_hours) // 2)])

    # Counted against what has actually landed in each window, not against a projected
    # cap: far fewer meetings reach the AM window than max_am allows, so a fixed
    # ceiling there would never bind and every AM section would pile onto 08:00.
    pm_used = 0
    am_early_used = 0
    pm_early_used = 0

    def on_roster(fac: str) -> bool:
        """faculty_load.csv is the authoritative list of who teaches this term.

        A name that appears only in prof_preferences.csv is not a professor the
        scheduler may use: previously such a name was silently given a load of
        DEFAULT_FACULTY_LOAD and started receiving sections, which is how CS
        faculty (Abdullah, Salem) ended up teaching maths courses, and how the
        misspelling 'Youssef' taught sections that belonged to 'Youseff'.
        Excluded names are listed by the INPUT CHECK before the run.
        """
        return faculty_limits.get(fac, 0) > 0

    # Scheduling order (C19 / "CYBR2500 never gets a faculty"): a section competes
    # for the same small pool of preferred professors as every other section of
    # its course, so the scarcest combinations must be placed first. Ordering by
    # course number instead meant 2000-level courses were reached only after their
    # preferred faculty were already at capacity, and fell through to TBA.
    #   demand = sections of this course / professors eligible to teach it
    # Highest demand first. Courses with no eligible faculty at all go last —
    # they are TBA regardless, so they should not take prime slots from courses
    # that can actually be staffed.
    def assign_priority(sec: Section) -> Tuple:
        pool = [f for f in fac_prefs.get(sec.course_number, []) if on_roster(f)]
        # A section with a lab needs a lecture slot AND a 105-min lab slot that
        # starts at the same clock time on a different day (C17) — far fewer
        # placements satisfy that than a lecture-only section, so labs go first
        # regardless of how contended their faculty pool is.
        lab_first = 0 if sec.lab_hours > 0 else 1
        if not pool:
            return (1, lab_first, 0.0, sec.course_number, sec.id)
        demand = course_section_counts.get(normalize(sec.course_number), 1) / len(pool)
        # On equal demand, the course with fewer eligible professors goes first.
        # Without this the tie broke alphabetically, so a course with a single
        # candidate could lose to one with three and find its only candidate
        # already full — the exact failure CYBR2500 was showing.
        return (0, lab_first, -demand, len(pool), sec.course_number, sec.id)

    ordered = sorted(sections, key=assign_priority)

    # ── helpers scoped to this function ───────────────────────────────────────

    def max_load(fac: str) -> int:
        return faculty_limits.get(fac, DEFAULT_FACULTY_LOAD)

    def can_assign(fac: str, sec: Section) -> bool:
        """Faculty has remaining load capacity, hasn't taught 2 sections of this
        course yet, and — for grad courses — hasn't already been given 2 grad sections."""
        faculty_load.setdefault(fac, 0)
        if faculty_load[fac] >= max_load(fac):
            return False
        if fac_course_count.get((fac, sec.course_number), 0) >= 2:          # C3
            return False
        # ≤ 2 graduate (5000+) sections per professor, across all grad courses.
        if is_grad(sec.course_number) and fac_grad_count.get(fac, 0) >= 2:  # C18
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
            if f not in seen and on_roster(f):
                pref.append(f)
                seen.add(f)
                faculty_load.setdefault(f, 0)

        def fill_ratio(f: str) -> float:
            cap = max_load(f)
            return faculty_load.get(f, 0) / cap if cap > 0 else float("inf")

        return sorted(pref, key=fill_ratio)

    def _patterns_for(sec: Section) -> List[List[str]]:
        """Ordered pool of day patterns to try for a section, using the days/week
        declared in the course list. Any N of the five weekdays is acceptable for
        N >= 3 (MWF, MTTh, TThF, TWF, ...) — the requirement is the *count*, not a
        specific combination — so patterns are generated and ranked by spread.
        Grad courses with 1 day/week prefer single evenings, falling back to 2-day
        patterns for courses too long for one evening slot.
        """
        n = lecture_days_for(sec.course_number, sec.lecture_days_per_week)
        if is_grad(sec.course_number):
            # One evening, Mon-Thu (no Friday evening slots exist). No 2-day
            # fallback: a grad session is always GRAD_MEETING_MIN, which always
            # fits one evening slot, and a 2-day placement would contradict the
            # days/week recorded on the section and fail C15.
            return [list(p) for p in GRAD_SINGLE_DAY_PATTERNS]
        return DAY_PATTERNS.get(n, DAY_PATTERNS[DEFAULT_LECTURE_DAYS])

    def viable_patterns(fac: str, sec: Section) -> List[List[str]]:
        """
        Return patterns that keep faculty ≤ 4 days (C4), sorted by load added to
        already-busy days (lightest first, for C16 balance).
        For grad single-day courses, single-day patterns are ranked first; 2-day patterns
        follow as a fallback (e.g. 4-credit courses that exceed the longest evening slot).
        Falls back to all patterns if C4 cannot be satisfied.
        """
        current = faculty_days_map.get(fac, set())
        max_days = teaching_day_allowance(
            [None] * lecture_days_for(sec.course_number, sec.lecture_days_per_week))

        def _rank(pool: List[List[str]]) -> List[List[str]]:
            ok, over = [], []
            for p in pool:
                score = sum(day_count.get(d, 0) for d in p)
                (ok if len(current | set(p)) <= max_days else over).append((score, p))
            ok.sort(key=lambda x: x[0])
            over.sort(key=lambda x: x[0])
            return [p for _, p in ok] or [p for _, p in over]

        return _rank(_patterns_for(sec))

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
        max_days = teaching_day_allowance(lecture_days + ["<lab>"])
        c4_safe = sorted(
            [d for d in non_lecture if len(current | {d}) <= max_days],
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
                if not course_time_ok(f"{sec.course_number}-LAB", [lab_day], lab_s):  # C14
                    continue
                if not time_sched._faculty_free(fac, [lab_day], lab_s.start, lab_s.stop):
                    continue
                if time_sched._would_exceed_span(fac, [lab_day], lab_s.start, lab_s.stop):
                    continue
                lab_room = room_assigner.find_room(sec, [lab_day], lab_s.start, lab_s.stop, is_lab=True)
                if lab_room is not None:
                    return lab_day, lab_s, lab_room
        return None

    def _window_balance_bias(t: "TimeSlot") -> int:
        """0 if this slot sits in the half of its own window (AM or PM) that still
        has early/late quota left, 1 otherwise. Purely an ordering preference — it
        never removes a slot, because unlike force_pm there is no guaranteed
        fallback once a section's window is already fixed."""
        if t.start.hour >= GRAD_START_HR:
            return 0
        if t.start.hour < AM_CUTOFF_HR:
            early_used, window_used = am_early_used, am_used
        else:
            early_used, window_used = pm_early_used, pm_used
        want_early = early_used < math.ceil(WINDOW_EARLY_RATIO * (window_used + 1))
        return 0 if (t.start.hour in early_hours) == want_early else 1

    def _faculty_time_bias(fac: str, force_pm: bool, sec: Section):
        """Sort key preferring a professor's declared AM/PM window, or None when it
        doesn't apply (TBA has no identity; grad courses are evening-only; and a
        section already forced into PM by the global 60 % AM quota can't honor an
        AM preference — the hard quota wins)."""
        if fac == "TBA" or force_pm or is_grad(sec.course_number):
            return None
        pref = faculty_time_prefs.get(fac)
        if pref not in ("AM", "PM"):
            return None
        want_am = pref == "AM"
        return lambda t: 0 if (t.start.hour < AM_CUTOFF_HR) == want_am else 1

    def _order_fallback_slots(cands: List["TimeSlot"], days: List[str], force_pm: bool) -> List["TimeSlot"]:
        """Order last-resort candidates the way _try_assign does, so forced
        placements spread out instead of all landing on the first slot of the day.

        Slots that are already at the concurrency ceiling are pushed to the back
        rather than dropped. A forced section is by definition one nothing else
        would take, so it must land somewhere — but it must not evict a validly
        scheduled section by pushing a slot past MAX_CONCURRENT, which is what
        produced the "Hidden (too many overlaps)" cards in the day view."""
        ordered_c = sorted(cands, key=lambda t: (time_sched._busyness(t, days), t.start.hour, t.start.minute))
        ordered_c.sort(key=_window_balance_bias)
        if force_pm:
            ordered_c.sort(key=lambda t: 0 if t.start.hour >= AM_CUTOFF_HR else 1)
        # Strongest key: never pick a full slot while a slot with room exists.
        ordered_c.sort(key=lambda t: 0 if time_sched._slot_capacity_ok(days, t) else 1)
        return ordered_c

    def _try_assign(
        sec: Section,
        fac: str,
        days: List[str],
        force_pm: bool,
    ) -> Optional[Tuple]:
        """
        Find (days, lec_slot, lec_room, lab_day, lab_slot, lab_room).
        When the section has a lab, the lab slot must start at the SAME CLOCK TIME
        as the lecture slot (hard constraint). Returns None if impossible.
        lab_day/slot/room are None when lab_hours == 0.
        """
        # Per-meeting length depends on how many days a week the course meets
        # (70 min over 3 days, 105 min over 2 for math, 90/80 for COMP), so it is
        # resolved here rather than divided out of a weekly total.
        per_day = meeting_minutes(sec.course_number, sec.lecture_hours, len(days))

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
        # Sorts are stable, so each later .sort() is a higher-priority key. Priority,
        # weakest to strongest: busyness → intra-window early/late balance → this
        # professor's AM/PM preference → non-overlap groups. Faculty preference outranks
        # the early/late balance because it is an explicit per-person input while the
        # balance is a statistical goal that self-corrects over the whole term; in
        # practice they rarely fight, since the preference picks the window (AM vs PM)
        # and the balance key only picks a half *within* whichever window is chosen.
        raw.sort(key=_window_balance_bias)
        fac_bias = _faculty_time_bias(fac, force_pm, sec)
        if fac_bias is not None:
            raw.sort(key=fac_bias)
        raw.sort(key=lambda t: _group_bias(sec.course_number, sec.id, days, t2m(t.start), t2m(t.stop)))

        for lec_slot in raw:
            if lec_slot.days_allowed and not all(d in lec_slot.days_allowed for d in days):
                continue
            if overlaps_reserved(days, lec_slot.start, lec_slot.stop):
                continue
            if not time_sched._slot_capacity_ok(days, lec_slot):
                continue
            if not course_time_ok(sec.course_number, days, lec_slot):     # C14
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
        # Candidate pool, filtered and re-sorted lazily. Rebuilding it for every
        # leftover TBA lecture was the second hot spot in over-subscribed runs;
        # the set only shrinks as loads fill, so it is cached and invalidated on
        # each assignment.
        _pool_cache: List[List[str]] = []

        def _invalidate_pool() -> None:
            _pool_cache.clear()

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
            if fac_course_count.get((fac, lec.course_number), 0) >= 2:          # C3
                return False
            # C18 — ≤ 2 graduate sections per professor. The main loop enforces
            # this in can_assign(); top-up must too. Before the preferred-first
            # pass this was unreachable (pass 2 only ever touched undergraduate
            # foundation courses), but pass 1 can reach any leftover TBA lecture.
            if is_grad(lec.course_number) and fac_grad_count.get(fac, 0) >= 2:
                return False
            new_days = set(faculty_days_map.get(fac, set())) | set(lec.days)
            if lab:
                new_days |= set(lab.days)
            if len(new_days) > teaching_day_allowance(lec.days, lab.days if lab else None):
                return False                                                   # C4
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

        def _assign(lec: ScheduledSection, fac: str, lab: Optional[ScheduledSection],
                    note: str, exception: bool = True) -> None:
            previous = lec.faculty          # captured before the reassignment below
            time_sched._block_faculty(fac, lec.days, lec.start_time, lec.end_time)
            faculty_days_map.setdefault(fac, set()).update(lec.days)
            # `topup` marks a *preference exception* for C19. A pass-1 assignment
            # goes to a professor already on the course's preference row, so it is
            # an ordinary assignment and must not be reported as an exception.
            lec.faculty, lec.topup = fac, exception
            if lab:
                time_sched._block_faculty(fac, lab.days, lab.start_time, lab.end_time)
                faculty_days_map[fac].update(lab.days)
                lab.faculty, lab.topup = fac, exception
            faculty_load[fac] = faculty_load.get(fac, 0) + 1
            _tally(previous, lec.course_number, -1)     # released from TBA
            _tally(fac, lec.course_number, +1)
            _invalidate_pool()
            print(f"[TOPUP] {lec.section_id} ({lec.course_number}) → {fac} "
                  f"({note}; load now {faculty_load[fac]}/{max_load(fac)})")

        def _prefers(fac: str, lec: ScheduledSection) -> bool:
            return on_roster(fac) and fac in fac_prefs.get(lec.course_number, [])

        # Pass 1 — PREFERRED top-up. Any leftover TBA section whose preference row
        # already names this professor. This runs before the foundation exception
        # so nobody is handed a course they never asked for while a course they did
        # ask for sits unstaffed.
        for lec in [s for s in lectures.values() if s.faculty == "TBA" and not s.is_lab]:
            lab = lab_by_parent.get(lec.section_id)
            for fac in underloaded():
                if not _prefers(fac, lec) or not feasible(fac, lec, lab):
                    continue
                _assign(lec, fac, lab, "was TBA; preferred", exception=False)
                break

        # Pass 2 — FOUNDATION exception. Only now, with every preferred pairing
        # already made, do we hand out CS1 / CS2 / Data Structures to professors
        # outside their preference row.
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
                _assign(lec, fac, lab, "was TBA; foundation exception")
                break

    # ── main scheduling loop ──────────────────────────────────────────────────

    for sec in ordered:
        sec_days = lecture_days_for(sec.course_number, sec.lecture_days_per_week)
        lab_min = lab_minutes(sec.course_number, sec.lecture_hours, sec.lab_hours, sec_days)
        force_pm = not is_grad(sec.course_number) and am_used >= max_am
        forced = False

        # chosen = (fac, days, lec_slot, lec_room, lab_day|None, lab_slot|None, lab_room|None)
        chosen = None

        # Search jointly over (faculty × day_pattern) until all constraints satisfied
        for fac in faculty_candidates(sec):
            if not can_assign(fac, sec):
                continue
            for days in viable_patterns(fac, sec):
                result = _try_assign(sec, fac, days, force_pm)
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
                result = _try_assign(sec, "TBA", days, force_pm)
                if result:
                    chosen = ("TBA", *result)
                    break

        # Hard fallback: force something rather than crash. This is never a valid
        # assignment — the section is flagged so it is reported as UNPLACED at the
        # end of the run instead of quietly appearing in the schedule.
        if not chosen:
            forced = True
            per_day_f = meeting_minutes(sec.course_number, sec.lecture_hours, sec_days)
            has_slot = any(s.duration_min == per_day_f for s in time_sched.slots)
            reason = (
                f"no {per_day_f}-minute slot exists in timings.csv — add one, or change "
                f"this course's meeting length in meeting_patterns.csv"
                if not has_slot else
                f"every {per_day_f}-minute slot is already at the "
                f"{MAX_CONCURRENT}-section limit on all {sec_days}-day patterns "
                f"(raise max_concurrent_sections in settings.csv, or add time slots)"
            )
            print(f"[CRITICAL] {sec.id} ({sec.course_number}): cannot be placed — {reason}. "
                  f"Forcing a placeholder; this section needs manual attention.")
            unplaced.append((sec.id, sec.course_number, reason))
            # Try every day pattern, not just the first, so a section is only
            # forced into a full slot when no pattern has room anywhere. The slot
            # menu does not depend on the day pattern, so it is built once.
            pattern_pool = _patterns_for(sec)
            base_cands = time_sched._eligible_slots(sec, per_day_f, force_pm=force_pm, max_duration=None)
            if not base_cands:  # PM filter left nothing — better a placed AM section than none
                base_cands = time_sched._eligible_slots(sec, per_day_f, force_pm=False, max_duration=None)
            if not base_cands:  # nothing long enough at all — take the longest slot there is
                base_cands = sorted(time_sched.slots, key=lambda s: -s.duration_min)[:1]

            days_f, slot_f, best_load = pattern_pool[0], None, None
            for cand_days in pattern_pool:
                # A forced placement still has to respect the days a slot allows
                # and the reserved Tue/Thu block; only capacity may be exceeded.
                legal = [c for c in base_cands
                         if (not c.days_allowed or all(d in c.days_allowed for d in cand_days))
                         and not overlaps_reserved(cand_days, c.start, c.stop)]
                cands = _order_fallback_slots(legal or base_cands, cand_days, force_pm)
                if not cands:
                    continue
                if time_sched._slot_capacity_ok(cand_days, cands[0]):
                    days_f, slot_f = cand_days, cands[0]
                    break
                # Nothing with room here — keep the least-crowded option seen so
                # far, across ALL patterns, so forced sections spread out instead
                # of stacking on whichever pattern happened to come first.
                load = time_sched._concurrency(cand_days, cands[0])
                if best_load is None or load < best_load:
                    days_f, slot_f, best_load = cand_days, cands[0], load
            if slot_f is None:
                slot_f = base_cands[0] if base_cands else time_sched.slots[0]
            lab_info_f = _find_lab_at("TBA", days_f, slot_f) if sec.lab_hours > 0 else None
            chosen = ("TBA", days_f, slot_f, "UNPLACED",
                      *(lab_info_f if lab_info_f else (None, None, None)))

        fac, days, slot, room, pre_lab_day, pre_lab_slot, pre_lab_room = chosen

        # Commit lecture
        room_assigner.book_room(room, days, slot.start, slot.stop)
        time_sched.book(fac, days, slot)
        faculty_load[fac] = faculty_load.get(fac, 0) + 1
        _tally(fac, sec.course_number, +1)
        _k = _time_key(sec.course_number, days, slot)
        course_time_count[_k] = course_time_count.get(_k, 0) + 1
        faculty_days_map.setdefault(fac, set()).update(days)
        for d in days:
            day_count[d] += 1
        if not is_grad(sec.course_number):
            am_used += 1 if slot.start.hour < AM_CUTOFF_HR else 0
            pm_used += 1 if slot.start.hour >= AM_CUTOFF_HR else 0
            if slot.start.hour in early_hours:
                if slot.start.hour < AM_CUTOFF_HR:
                    am_early_used += 1
                else:
                    pm_early_used += 1

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
            days_per_week=sec_days,
            forced=forced,
        )
        _record_group_rep(sec.course_number, sec.id, days, t2m(slot.start), t2m(slot.stop))

        # ── lab ───────────────────────────────────────────────────────────────
        if sec.lab_hours > 0:
            if pre_lab_day is not None:
                # Same-start lab found during assignment — commit it directly.
                lab_day, lab_slot, lab_room = pre_lab_day, pre_lab_slot, pre_lab_room
                lab_forced = False
                room_assigner.book_room(lab_room, [lab_day], lab_slot.start, lab_slot.stop)
            else:
                # Hard-fallback path: no same-start lab found; place lab anywhere available.
                print(f"[WARN] {sec.id}-LAB: could not match lecture start time; scheduling independently.")
                lab_day_candidates = _lab_day_candidates(fac, days)
                lab_slot = None
                lab_day = lab_day_candidates[0] if lab_day_candidates else ALL_DAYS[0]
                # Honor the AM quota first, but retry without it rather than let a lab
                # fall through to the CRITICAL path purely because PM was full.
                for pm_only in ((True, False) if force_pm else (False,)):
                    for ld in lab_day_candidates:
                        lab_slot = time_sched.find_slot(sec, fac, [ld], lab_min, force_pm=pm_only,
                                                        max_duration=LAB_MAX_MIN, prefer=_window_balance_bias)
                        if lab_slot:
                            lab_day = ld
                            break
                    if lab_slot:
                        break
                lab_forced = False
                if lab_slot is None:
                    lab_forced = True
                    reason = f"no lab slot of {lab_min} min is free on any day"
                    print(f"[CRITICAL] {sec.id}-LAB: cannot be placed — {reason}. "
                          f"Forcing a placeholder; this lab needs manual attention.")
                    unplaced.append((f"{sec.id}-LAB", sec.course_number, reason))
                    cands = time_sched._eligible_slots(sec, lab_min, force_pm=force_pm, max_duration=LAB_MAX_MIN)
                    if not cands:
                        cands = time_sched._eligible_slots(sec, lab_min, force_pm=False, max_duration=LAB_MAX_MIN)
                    cands = _order_fallback_slots(cands, [lab_day], force_pm)
                    lab_slot = cands[0] if cands else time_sched.slots[0]
                lab_room = room_assigner.find_room(sec, [lab_day], lab_slot.start, lab_slot.stop, is_lab=True)
                if lab_room is None:
                    if not lab_forced:
                        reason = "no room is free at the only time this lab could take"
                        print(f"[CRITICAL] {sec.id}-LAB: cannot be placed — {reason}.")
                        unplaced.append((f"{sec.id}-LAB", sec.course_number, reason))
                    lab_forced = True
                    lab_room = "UNPLACED"
                else:
                    room_assigner.book_room(lab_room, [lab_day], lab_slot.start, lab_slot.stop)

            time_sched.book(fac, [lab_day], lab_slot)
            _lk = _time_key(f"{sec.course_number}-LAB", [lab_day], lab_slot)
            course_time_count[_lk] = course_time_count.get(_lk, 0) + 1
            faculty_days_map.setdefault(fac, set()).add(lab_day)
            day_count[lab_day] += 1
            if not is_grad(sec.course_number):
                am_used += 1 if lab_slot.start.hour < AM_CUTOFF_HR else 0
                pm_used += 1 if lab_slot.start.hour >= AM_CUTOFF_HR else 0
                if lab_slot.start.hour in early_hours:
                    if lab_slot.start.hour < AM_CUTOFF_HR:
                        am_early_used += 1
                    else:
                        pm_early_used += 1

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
                forced=lab_forced or forced,
            ))

    lab_by_parent = {lab.section_id.replace("-LAB", ""): lab for lab in labs}

    # Top up under-target profs with leftover TBA foundational sections.
    _topup_underloaded(lab_by_parent)

    if unplaced:
        print("\n── ⚠ SECTIONS THAT COULD NOT BE PLACED ──")
        for sid, cn, reason in unplaced:
            print(f"  {sid} ({cn}): {reason}")
        print(f"  {len(unplaced)} section(s) hold a placeholder slot and room "
              f"'UNPLACED'. Fix the inputs above and re-run.")
        print("────────────────────────────────────────\n")

    # Interleave labs right after their parent lecture
    result: Dict[str, ScheduledSection] = {}
    for sid, s in lectures.items():
        result[sid] = s
        if sid in lab_by_parent:
            lab = lab_by_parent[sid]
            result[lab.section_id] = lab

    return result


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

    by_course: Dict[str, List[ScheduledSection]] = {}
    for s in sections.values():
        if not s.is_lab:
            by_course.setdefault(normalize(s.course_number), []).append(s)

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
            if c not in known_sections:
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

    check_inputs(courses, fac_prefs, faculty_limits)

    course_titles = {normalize(c.number): c.name for c in courses}
    sections      = build_sections(courses, fac_prefs)

    # Faculty, time, and room are now assigned jointly inside build_schedule.
    time_sched    = TimeSlotScheduler(timeslots)
    room_assigner = RoomAssigner(rooms, room_prefs)
    scheduled     = build_schedule(sections, fac_prefs, faculty_limits, time_sched, room_assigner,
                                    non_overlap_groups=overlap_groups,
                                    faculty_time_prefs=faculty_tprefs)

    print_summary(scheduled, courses, faculty_limits, time_sched.slot_load)
    ConstraintChecker(fac_prefs, timeslots).run_all(scheduled, faculty_limits)
    check_non_overlap_groups(scheduled, overlap_groups, courses)
    export_json(scheduled, courses, os.path.join(base, "schedule.json"))
    export_csv(scheduled, course_titles, os.path.join(base, "schedule.csv"))
    export_simple_csv(scheduled, os.path.join(base, "schedule_simple.csv"))


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
