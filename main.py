import csv
from dataclasses import dataclass
from typing import List, Optional, Dict
from datetime import time
import re
import json
import math


# ---------- tiny helpers ----------
def to_int(s: str, default: int = 0) -> int:
    try:
        return int(s.strip())
    except Exception:
        return default

def to_bool(s: str) -> bool:
    s = (s or "").strip().lower()
    return s in ("true", "1", "yes", "y")

def parse_time(hhmmss: str) -> time:
    # accepts "08:00:00" etc.
    hh, mm, ss = [int(x) for x in hhmmss.strip().split(":")]
    return time(hh, mm, ss)

def split_list(s: str) -> List[str]:
    # turns 'M,T,W,Th,F' -> ['M','T','W','Th','F']
    if not s:
        return []
    return [part.strip() for part in s.split(",") if part.strip()]

def choose_day_pattern(days_per_week: int) -> list[list[str]]:
    if days_per_week == 2:
        return [
            ["M", "W"],
            ["T", "Th"],
            ["W", "F"],
        ]
    elif days_per_week == 1:
        return [["M"], ["T"], ["W"], ["Th"], ["F"]]
    else:
        return [["M", "W", "F"]]
    

def is_grad_course(course_number: str) -> bool:
    """
    Treat any course with a numeric level >= 5000 as graduate.
    Works for COMP5500, DATA6100, etc.
    """
    m = re.search(r"(\d+)", course_number or "")
    return bool(m and int(m.group(1)) >= 5000)


    
def course_durations(lecture_hours: int, lab_hours: int) -> tuple[int, int]:
    """
    Convert lecture/lab credit hours into approximate clock minutes.
    (3 hrs → 150 min, 2 hrs → 105 min, 4 hrs → 240 min)
    """
    lecture_min = 150 if lecture_hours == 3 else 240 if lecture_hours == 4 else lecture_hours * 60
    lab_min = 105 if lab_hours == 2 else lab_hours * 60
    return lecture_min, lab_min

def evening_slots(times: list["TimeSlotRow"]) -> list["TimeSlotRow"]:
    """Return only evening slots (starting at or after 17:00)."""
    return [t for t in times if t.start.hour >= 17]





# ---------- data models ----------
@dataclass
class CourseRow:
    number: str
    name: str
    lecture_days_per_week: int
    lecture_hours: int
    lab_hours: int
    sections: int
    preferred_room: Optional[str]  # may be empty

@dataclass
class PreferenceRow:
    course_number: str
    course_name: str
    faculty_ranked: List[str]  # ranked by preference order

@dataclass
class RoomRow:
    room: str
    type: str         # e.g., Lecture, Lab
    capacity: int

@dataclass
class TimeSlotRow:
    start: time
    stop: time
    duration_min: int
    slot_label: str   # e.g., lecture_75 / lab_~110
    evening: bool
    days_allowed: List[str]   # e.g., ['M','T','W','Th','F']

@dataclass
class Section:
    id: str                     # e.g. COMP1000-1
    course_number: str
    course_name: str
    preferred_room: Optional[str]
    faculty_options: List[str]  # ranked professors who can teach it
    lecture_days_per_week: int
    lecture_hours: int
    lab_hours: int

@dataclass
class SectionAssignment:
    section_id: str
    course_number: str
    faculty: str
    room: Optional[str]
    days: list[str]
    start_time: Optional[time]
    end_time: Optional[time]
    has_lab: bool
    islab: bool = False  # flag to mark lab sections

@dataclass
class FacultySchedule:
    name: str
    assigned_sections: List[str]
    days: Dict[str, List[tuple]]  # {"M": [(start,end,section_id), ...], ...}

@dataclass
class RoomSchedule:
    room: str
    bookings: Dict[str, List[tuple]]  # {"M": [(start,end,section_id), ...], ...}

@dataclass
class RoomPreference:
    course: str         # normalized course key, e.g. "COMP1000"
    type: str           # "Lab" / "Lecture"
    rank: int           # PreferenceRank
    location: str       # room name, e.g. "DOBBS 203"
    max_cap: int        # preferred capacity cap (from max_cap column)



def normalize_course_number(s: str) -> str:
    return (s or "").replace(" ", "").strip()

def load_room_preferences(path: str) -> dict[tuple[str, str], List[RoomPreference]]:
    """
    Returns a dict keyed by (course_key, type_lower) -> [RoomPreference, ...]
    where course_key is normalized like "COMP1000".
    """
    prefs: dict[tuple[str, str], List[RoomPreference]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            course_raw = (r["Course"] or "").strip()
            type_raw = (r["Type"] or "").strip()
            key = (normalize_course_number(course_raw), type_raw.lower())

            pref = RoomPreference(
                course=normalize_course_number(course_raw),
                type=type_raw,
                rank=to_int(r["PreferenceRank"], 0),
                location=(r["Location"] or "").strip(),
                max_cap=to_int(r.get("max_cap", "0")),
            )
            prefs.setdefault(key, []).append(pref)

    # sort each list by rank so we can just iterate in preference order
    for key in prefs:
        prefs[key].sort(key=lambda p: p.rank)
    return prefs


# ---------- loaders ----------
def load_courses(path: str) -> List[CourseRow]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            out.append(
                CourseRow(
                    number=r["Course number"].strip(),
                    name=r["Course Name"].strip(),
                    lecture_days_per_week=to_int(r["lecture days per week"]),
                    lecture_hours=to_int(r["lecture hours"]),
                    lab_hours=to_int(r["lab hours"]),
                    sections=to_int(r["number of sections"]),
                    preferred_room=(r.get("Preferred Room") or "").strip() or None,
                )
            )
    return out

def load_preferences(path: str) -> Dict[str, PreferenceRow]:
    # keyed by course number for quick lookup
    out: Dict[str, PreferenceRow] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            fac = r.get("Faculty") or r.get("faculty") or ""
            ranked = [p.strip() for p in fac.split(",") if p.strip()]
            row = PreferenceRow(
                course_number=r["Course Number"].strip(),
                course_name=r["Course Name"].strip(),
                faculty_ranked=ranked,
            )
            out[row.course_number] = row
    return out

def load_rooms(path: str) -> List[RoomRow]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            out.append(
                RoomRow(
                    room=r["Room"].strip(),
                    type=r["Type"].strip(),
                    capacity=to_int(r["Capacity"]),
                )
            )
    return out

def load_times(path: str) -> List[TimeSlotRow]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            out.append(
                TimeSlotRow(
                    start=parse_time(r["start_time"]),
                    stop=parse_time(r["stop_time"]),
                    duration_min=to_int(r["duration_min"]),
                    slot_label=r["slot_label"].strip(),
                    evening=to_bool(r["evening"]),
                    days_allowed=split_list(r["Days Allowed"].strip().strip('"')),
                )
            )
    return out

def create_sections(courses: List[CourseRow], prefs: Dict[str, PreferenceRow]) -> List[Section]:
    sections: List[Section] = []
    for course in courses:
        for i in range(1, course.sections + 1):
            pref_row = prefs.get(course.number)
            faculty = pref_row.faculty_ranked if pref_row else []
            sections.append(
                Section(
                    id=f"{course.number}-{i}",
                    course_number=course.number,
                    course_name=course.name,
                    preferred_room=course.preferred_room,
                    faculty_options=faculty,
                    lecture_days_per_week=course.lecture_days_per_week,
                    lecture_hours=course.lecture_hours,
                    lab_hours=course.lab_hours,
                )
            )
    return sections

def initialize_schedule(sections: List[Section]) -> dict:
    """Prepare an empty schedule structure before assigning times/faculty."""
    schedule = {"sections": {}, "faculty": {}, "rooms": {}}

    for sec in sections:
        schedule["sections"][sec.id] = SectionAssignment(
            section_id=sec.id,
            course_number=sec.course_number,
            faculty="",            # initially unassigned
            room=None,             # will be filled later
            days=[],               # to be filled later
            start_time=None,
            end_time=None,
            has_lab=False,         # default until assigned
        )       
    return schedule

def assign_faculty(
    sections: List[Section],
    prefs: Dict[str, PreferenceRow],
    faculty_load_limits: Dict[str, int],
) -> Dict[str, str]:
    """
    Assign faculty to each section based on preferences and per-faculty load limits
    from faculty_load.csv, using a balanced (lowest-load-first) strategy.
    Returns a mapping {section_id: faculty_name}
    """
    # Initialize load for all faculty we know about from the limits file
    faculty_load: Dict[str, int] = {name: 0 for name in faculty_load_limits}
    assignment: Dict[str, Optional[str]] = {}

    def max_for(fac: str) -> int:
        # Default to 3 if not specified in CSV
        return faculty_load_limits.get(fac, 3)

    # Stable ordering: higher-priority courses first, then by course/section id
    sorted_secs = sorted(
        sections,
        key=lambda sec: (course_priority(sec), sec.course_number, sec.id),
    )

    # ---- Step 1: preference-based pass (balanced by load) ----
    for sec in sorted_secs:
        pref_row = prefs.get(sec.course_number)
        ranked = pref_row.faculty_ranked if pref_row else []

        best_fac = None
        best_load = None

        for f in ranked:
            # Ensure faculty is tracked
            if f not in faculty_load:
                faculty_load[f] = 0

            load = faculty_load[f]
            if load >= max_for(f):
                continue

            # How many of this exact course does this faculty already teach?
            same_course_count = sum(
                1 for sid, fac in assignment.items()
                if fac == f and sec.course_number in sid
            )
            if same_course_count >= 2:
                continue

            # Choose the eligible faculty with the LOWEST current load
            if best_fac is None or load < best_load:
                best_fac = f
                best_load = load

        if best_fac is not None:
            assignment[sec.id] = best_fac
            faculty_load[best_fac] += 1
        else:
            assignment[sec.id] = None  # will be handled in fallback

    # ---- Step 2: fallback pass (still balanced) ----
    for sec in sorted_secs:
        if assignment[sec.id] is not None:
            continue

        # Faculty who still have capacity
        candidates = [
            f for f, load in faculty_load.items()
            if load < max_for(f)
        ]

        if candidates:
            # Pick the faculty with minimum current load
            best_fac = min(candidates, key=lambda f: faculty_load[f])
            assignment[sec.id] = best_fac
            faculty_load[best_fac] += 1
        else:
            # Nobody has capacity; mark TBA
            assignment[sec.id] = "TBA"

    # Final mapping is now all strings
    return assignment  # type: ignore[return-value]




def build_faculty_schedule(assignment: Dict[str, str], sections: List[Section]) -> Dict[str, FacultySchedule]:
    """Construct faculty schedule objects from assignments."""
    faculty_map: Dict[str, FacultySchedule] = {}

    for sec in sections:
        f = assignment.get(sec.id, "TBA")
        if f not in faculty_map:
            faculty_map[f] = FacultySchedule(name=f, assigned_sections=[], days={})
        faculty_map[f].assigned_sections.append(sec.id)

    return faculty_map

def update_schedule_with_faculty(schedule: dict, assignment: Dict[str, str]) -> None:
    """Fill the faculty field of each section in schedule."""
    for sid, faculty in assignment.items():
        if sid in schedule["sections"]:
            schedule["sections"][sid].faculty = faculty


def assign_times_and_labs(schedule: dict,
                          sections: list[Section],
                          times: list[TimeSlotRow],
                          rooms: list[RoomRow],
                          room_prefs: dict[tuple[str, str], List[RoomPreference]]) -> None:


    """
    Sequential scheduler:
      - Assign earliest available lecture slot per faculty
      - Immediately assign lab on a different day (F/M/etc.)
      - Prevent faculty overlaps
      - Follow MW/TTh/WF pattern rotation
      - Handle evening rule for 5000+ courses
    """

    all_days = ["M", "T", "W", "Th", "F"]
    patterns = [["M", "W"], ["T", "Th"], ["W", "F"]]
    pattern_index = 0

    # Track faculty busy times per day
    faculty_availability: dict[str, dict[str, list[tuple[int, int]]]] = {}

    # Track room availability per day
    room_availability: dict[str, dict[str, list[tuple[int, int]]]] = {}

    # Sort time slots by actual start time
    times_sorted = sorted(times, key=lambda t: (t.start.hour, t.start.minute))

    # ---------- AM/PM BALANCING FOR UNDERGRAD CLASSES (LEC + LAB) ----------
    # Each undergrad section with a lab counts as *two* meetings (lec + lab)
    total_ug_classes = sum(
        (1 + (1 if sec.lab_hours > 0 else 0))
        for sec in sections
        if not is_grad_course(sec.course_number)
    )
    max_am_ug_classes = math.ceil(0.6 * total_ug_classes)
    am_ug_classes_used = 0
    pm_ug_classes_used = 0

    # Track global concurrency (for constraint c11, not implemented here)
    global_concurrency = {d: [] for d in all_days}

    # Track how many sections occupy each (day, time) slot
    slot_load = {d: {} for d in all_days}  # e.g., slot_load["M"]["08:00-09:15"] = 3

    def slot_is_available(days: list[str], slot: TimeSlotRow) -> bool:
        """Return True if all days in `days` have this slot below the limit of 10."""
        for d in days:
            key = slot_key(slot)
            if slot_load[d].get(key, 0) >= 10:
                return False
        return True

    def increment_slot_load(days: list[str], slot: TimeSlotRow):
        """Increment load for all days in this slot."""
        for d in days:
            key = slot_key(slot)
            slot_load[d][key] = slot_load[d].get(key, 0) + 1


    def slot_key(t: TimeSlotRow) -> str:
        """Generate a readable key for a time slot."""
        return f"{t.start.strftime('%H:%M')}-{t.stop.strftime('%H:%M')}"

    def t2m(t: time) -> int:
        return t.hour * 60 + t.minute
    
    def ranges_overlap(s1: int, e1: int, s2: int, e2: int, *, gap_min: int = 15) -> bool:
        """
        True if intervals are closer than `gap_min` minutes.
        That is, we consider it a conflict unless there is at least `gap_min`
        minutes of free time between them.
        """
        # Require: e1 + gap <= s2  OR  e2 + gap <= s1 to be "safe"
        # So conflict = NOT safe.
        return not (e1 + gap_min <= s2 or e2 + gap_min <= s1)

    
    def per_meeting_minutes(total_minutes: int, num_meetings: int) -> int:
        # Split total weekly minutes across the number of meetings (ceil to be safe).
        # e.g., 150 min across 2 days -> 75 min; 240 across 2 days -> 120
        return (total_minutes + num_meetings - 1) // num_meetings
    
    
    def is_free(faculty: str, days: list[str], start_t: time, end_t: time) -> bool:
        """
        Faculty is free if, for every day in `days`, the (start,end) does not overlap
        any existing (s,e) stored for that same day.
        """

        if faculty == "TBA":
            return True

        start = t2m(start_t)
        end = t2m(end_t)
        avail = faculty_availability.setdefault(faculty, {d: [] for d in all_days})
        for d in days:
            for (s, e) in avail.get(d, []):
                if ranges_overlap(start, end, s, e):
                    # DEBUG (optional):
                    # print(f"[BUSY] {faculty} on {d} {start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')} overlaps {s//60:02d}:{s%60:02d}-{e//60:02d}:{e%60:02d}")
                    return False
        return True
    
    def count_concurrent_sections(day: str, start_t: time, end_t: time) -> int:
        """Count how many sections already occupy this time on a given day."""
        start = t2m(start_t)
        end = t2m(end_t)
        overlaps = 0
        for s, e in global_concurrency.get(day, []):
            if not (end <= s or start >= e):
                overlaps += 1
        return overlaps

    def block_concurrency(days: list[str], start_t: time, end_t: time):
        """Record that this timeslot is now taken by a section on these days."""
        start = t2m(start_t)
        end = t2m(end_t)
        for d in days:
            global_concurrency.setdefault(d, []).append((start, end))


    
    def block_time(faculty: str, days: list[str], start_t: time, end_t: time):
        start = t2m(start_t)
        end = t2m(end_t)
        avail = faculty_availability.setdefault(faculty, {d: [] for d in all_days})
        for d in days:
            avail[d].append((start, end))

    def room_is_free(room: str, days: list[str], start_t: time, end_t: time) -> bool:
        """Check if room is available on all given days for the time range."""
        start = start_t.hour * 60 + start_t.minute
        end = end_t.hour * 60 + end_t.minute
        avail = room_availability.setdefault(room, {d: [] for d in all_days})

        for d in days:
            for (s, e) in avail[d]:
                if not (end <= s or start >= e):
                    return False
        return True

    def block_room(room: str, days: list[str], start_t: time, end_t: time):
        """Reserve a room during the time range for given days."""
        start = start_t.hour * 60 + start_t.minute
        end = end_t.hour * 60 + end_t.minute
        avail = room_availability.setdefault(room, {d: [] for d in all_days})
        for d in days:
            avail[d].append((start, end))

    RES_START_MIN = 12 * 60       # 12:00
    RES_END_MIN = 13 * 60 + 30    # 13:30

    def overlaps_reserved(days: list[str], start_t: time, end_t: time) -> bool:
        """
        Returns True if this (days, time-range) overlaps Tue/Thu 12:00–13:30.
        """
        if not any(d in ("T", "Th") for d in days):
            return False
        start = t2m(start_t)
        end = t2m(end_t)
        return not (end <= RES_START_MIN or start >= RES_END_MIN)


    def find_available_room(
        sec: Section,
        rooms: list[RoomRow],
        days: list[str],
        start_t: time,
        end_t: time,
        *,
        is_lab: bool,
        needed_capacity: int = 25,
    ) -> Optional[str]:
        """
        Room selection strategy:

        1) Use room_preferences.csv if entries exist for (course, type),
           in PreferenceRank order.
        2) If none of those are free, fall back to ANY free room
           that meets capacity (ignore type).
        3) If still nothing, as a last resort pick ANY room
           (may overbook) but never return None so scheduling
           can always proceed.
        """

        needed_type = "Lab" if is_lab else "Lecture"
        course_key = normalize_course_number(sec.course_number)

        prefs_key = (course_key, needed_type.lower())
        prefs_for_course = room_prefs.get(prefs_key, [])

        # effective capacity requirement
        eff_needed_capacity = max(
            needed_capacity,
            getattr(sec, "expected_capacity", 0) or 0,
        )

        # 1️⃣ Try preferred rooms for this course+type
        for pref in prefs_for_course:
            room_name = pref.location
            pref_cap = pref.max_cap or eff_needed_capacity
            for r in rooms:
                if (
                    r.room == room_name
                    and r.capacity >= pref_cap
                    and room_is_free(r.room, days, start_t, end_t)
                ):
                    block_room(r.room, days, start_t, end_t)
                    return r.room

        # 2️⃣ No preferred room free: pick ANY free room that meets capacity
        #     (ignore type completely so we don't get stuck)
        capacity_candidates = [
            r for r in rooms
            if r.capacity >= eff_needed_capacity
            and room_is_free(r.room, days, start_t, end_t)
        ]

        if capacity_candidates:
            # pick the smallest-capacity suitable room
            best_room = min(capacity_candidates, key=lambda r: r.capacity)
            block_room(best_room.room, days, start_t, end_t)
            return best_room.room

        # 3️⃣ Last resort: overbook some room instead of failing completely.
        #    This is exactly "if prefs aren't available, pick any room".
        if rooms:
            best_room = min(rooms, key=lambda r: r.capacity)
            print(
                f"[ROOM-OVERBOOK] No truly free room for {sec.id} on {days} "
                f"{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')}; "
                f"using {best_room.room} anyway."
            )
            # we still block it so later logic sees it as taken
            block_room(best_room.room, days, start_t, end_t)
            return best_room.room

        # no rooms at all in the data
        return None


    def preferred_lab_day(lecture_days: list[str]) -> str:
        """Smart mapping: MW→F, TTh→F/M, WF→M, else next free day."""
        joined = "".join(lecture_days)
        if joined == "MW":
            return "F"
        elif joined == "TTh":
            return "F"
        elif joined == "WF":
            return "M"
        for d in all_days:
            if d not in lecture_days:
                return d
        return "F"
    
    

    # go section by section
    new_entries = []
    for sec in sorted(sections, key=course_priority):
        sec_assignment = schedule["sections"][sec.id]
        faculty = sec_assignment.faculty or "TBA"

        # pick day pattern (rotating)
        pattern_days = patterns[pattern_index % len(patterns)]
        pattern_index += 1

        # total minutes from your course_durations()
        lecture_min, lab_min = course_durations(sec.lecture_hours, sec.lab_hours)
        
                # per-meeting (e.g., MW => 2 meetings; TTh => 2; WF => 2)
        meet_min = per_meeting_minutes(lecture_min, len(pattern_days))

        # ---------- BASE SLOTS: grad vs undergrad ----------
        if is_grad_course(sec.course_number):
            # Grad: only 5–8 PM
            base_slots = [
                t for t in times_sorted
                if 17 <= t.start.hour < 20 and t.duration_min >= meet_min
            ]
        else:
            # Undergrad: anything before 5 PM
            base_slots = [
                t for t in times_sorted
                if t.start.hour < 17 and t.duration_min >= meet_min
            ]

        # ---------- AM/PM balancing for UNDERGRAD LECTURES ----------
        if not is_grad_course(sec.course_number):
            am_slots = [t for t in base_slots if t.start.hour < 12]
            pm_slots = [t for t in base_slots if t.start.hour >= 12]

            # If we've already hit the AM cap, and we have PM options,
            # force this course to pick from PM slots.
            if am_ug_classes_used >= max_am_ug_classes and pm_slots:
                usable_times = pm_slots
            else:
                usable_times = base_slots
        else:
            # Grad: no AM/PM balancing, already forced to 5–8 PM
            usable_times = base_slots



        # ---- LECTURE SCHEDULING ----
        chosen_slot = None
        chosen_room = None

        # Prefer slots that are currently less loaded across the pattern days,
        # then earlier times within those.
        def slot_busyness(slot: TimeSlotRow) -> int:
            key = slot_key(slot)
            return max(slot_load[d].get(key, 0) for d in pattern_days)

        usable_times_sorted = sorted(
            usable_times,
            key=lambda t: (slot_busyness(t), t.start.hour, t.start.minute)
        )

        for slot in usable_times_sorted:

            # Skip reserved Tue/Thu 12–1:30
            if overlaps_reserved(pattern_days, slot.start, slot.stop):
                continue

            if not slot_is_available(pattern_days, slot):
                continue
            if not is_free(faculty, pattern_days, slot.start, slot.stop):
                continue
            
            # 2️⃣ Try to find a room for this slot
            candidate_room = find_available_room(
                sec, rooms, pattern_days, slot.start, slot.stop,
                is_lab=False, needed_capacity=25
            )

            # If a valid room is found, assign it and break
            if candidate_room:
                chosen_slot = slot
                chosen_room = candidate_room
                increment_slot_load(pattern_days, chosen_slot)
                for day in pattern_days:
                    block_concurrency([day], slot.start, slot.stop)
                break
            
        # 3️⃣ Fallback handling (none of the usable_times worked)
        if not chosen_slot:
            print(f"[WARN] Could not find valid room/time for {sec.id} with faculty {faculty}; searching fallback.")

            if is_grad_course(sec.course_number):
                # Grad fallback: still only 5–8 PM
                fallback_slots = [
                    t for t in times_sorted
                    if 17 <= t.start.hour < 20 and t.duration_min >= meet_min
                ]
            else:
                # Undergrad fallback: still strictly before 5 PM
                fallback_slots = [
                    t for t in times_sorted
                    if t.start.hour < 17 and t.duration_min >= meet_min
                ]

            for slot in fallback_slots:
                if not is_free(faculty, pattern_days, slot.start, slot.stop):
                    continue
                candidate_room = find_available_room(
                    sec, rooms, pattern_days, slot.start, slot.stop,
                    is_lab=False, needed_capacity=25
                )
                if candidate_room :
                    chosen_slot = slot
                    chosen_room = candidate_room
                    break

                    
        # 4️⃣ Assign final lecture details
        if not chosen_slot or not chosen_room:
            print(f"[CRITICAL] {sec.id}: No available time+room combination found.")
            chosen_slot = usable_times[0] if usable_times else times_sorted[0]
            chosen_room = "FORCE_ASSIGN_ROOM"

        sec_assignment.days = pattern_days
        sec_assignment.start_time = chosen_slot.start
        sec_assignment.end_time = chosen_slot.stop
        sec_assignment.has_lab = sec.lab_hours > 0
        sec_assignment.islab = False
        sec_assignment.room = chosen_room

        block_time(faculty, pattern_days, chosen_slot.start, chosen_slot.stop)
        block_concurrency(pattern_days, chosen_slot.start, chosen_slot.stop)

        # Update AM/PM counts for UNDERGRAD lectures only
        if not is_grad_course(sec.course_number):
            if chosen_slot.start.hour < 12:
                am_ug_classes_used += 1
            else:
                pm_ug_classes_used += 1




        # ---- LAB SCHEDULING ----
        if sec.lab_hours > 0:
            lab_day = preferred_lab_day(pattern_days)
            lab_slot = None
            lab_room = None

            if is_grad_course(sec.course_number):
                # Grad labs (if any): 5–8 PM
                lab_times = [
                    t for t in times_sorted
                    if 17 <= t.start.hour < 20 and t.duration_min >= lab_min
                ]
            else:
                # Undergrad labs: strictly before 5 PM
                lab_times = [
                    t for t in times_sorted
                    if t.start.hour < 17 and t.duration_min >= lab_min
                ]

            # Prefer lab slots that are less loaded on that lab_day,
            # then earlier times within those.
            def lab_slot_busyness(slot: TimeSlotRow) -> int:
                key = slot_key(slot)
                return slot_load[lab_day].get(key, 0)

            lab_times_sorted = sorted(
                lab_times,
                key=lambda t: (lab_slot_busyness(t), t.start.hour, t.start.minute)
            )
        
            # Try to find a lab slot where both faculty and room are available
            for slot in lab_times_sorted:
                # Apply AM/PM balance to UNDERGRAD labs as well
                if not is_grad_course(sec.course_number):
                    # This lab belongs to an undergrad course
                    if am_ug_classes_used >= max_am_ug_classes and slot.start.hour < 12:
                        # We’ve already hit our AM quota → skip extra AM labs if possible
                        continue
                if overlaps_reserved([lab_day], slot.start, slot.stop):
                    continue
                if not slot_is_available([lab_day], slot):
                    continue
                if not is_free(faculty, [lab_day], slot.start, slot.stop):
                    continue
                
                # Try to find a valid room
                candidate_room = find_available_room(
                    sec, rooms, [lab_day], slot.start, slot.stop,
                    is_lab=True, needed_capacity=25
                )

        
                if candidate_room :
                    lab_slot = slot
                    lab_room = candidate_room
                    increment_slot_load([lab_day], lab_slot)
                    block_concurrency([lab_day], slot.start, slot.stop)
                    break
                
            # Fallback: try any slot that works with a room, but still obey UG/Grad windows
            if not lab_slot:
                print(f"[WARN] Could not find valid room/time for LAB {sec.id}, searching fallback.")

                if is_grad_course(sec.course_number):
                    fallback_lab_times = [
                        t for t in times_sorted
                        if 17 <= t.start.hour < 20 and t.duration_min >= lab_min
                    ]
                else:
                    fallback_lab_times = [
                        t for t in times_sorted
                        if t.start.hour < 17 and t.duration_min >= lab_min
                    ]

                for slot in fallback_lab_times:
                    if not is_free(faculty, [lab_day], slot.start, slot.stop):
                        continue
                    candidate_room = find_available_room(
                        sec, rooms, [lab_day], slot.start, slot.stop,
                        is_lab=True, needed_capacity=25
                    )
                    if candidate_room :
                        lab_slot = slot
                        lab_room = candidate_room
                        break


            # Critical fallback: no slot or room found at all
            if not lab_slot or not lab_room:
                print(f"[CRITICAL] LAB {sec.id}: No available time+room combination found.")
                lab_slot = lab_times[0] if lab_times else times_sorted[0]
                lab_room = "FORCE_ASSIGN_ROOM"
        
            # Create the lab section assignment
            lab_id = f"{sec.id}-LAB"
            lab_assignment = SectionAssignment(
                section_id=lab_id,
                course_number=sec.course_number,
                faculty=faculty,
                room=lab_room,
                days=[lab_day],
                start_time=lab_slot.start,
                end_time=lab_slot.stop,
                has_lab=False,
                islab=True,
            )
        
            # Block faculty and record
            block_time(faculty, [lab_day], lab_slot.start, lab_slot.stop)
            new_entries.append((sec.id, lab_assignment))
            # Count this lab toward AM/PM balance for undergrad
            if not is_grad_course(sec.course_number):
                if lab_slot.start.hour < 12:
                    am_ug_classes_used += 1
                else:
                    pm_ug_classes_used += 1
        

    # insert labs right after lectures
    ordered = {}
    for sid in schedule["sections"].keys():
        ordered[sid] = schedule["sections"][sid]
        for lec_id, lab_entry in new_entries:
            if lec_id == sid:
                ordered[lab_entry.section_id] = lab_entry
    schedule["sections"] = ordered
    # expose concurrency info to callers
    schedule["global_concurrency"] = global_concurrency
    schedule["slot_load"] = slot_load
    schedule["am_pm_counts"] = {
        "AM": am_ug_classes_used,
        "PM": pm_ug_classes_used,
    }



def export_schedule_to_json(schedule, courses, filename="schedule.json"):
    """
    Use CourseRow.sections to decide whether to append -<section_num> to course name.
    If a course has only 1 section, keep it as COMP3350.
    If it has >1 sections, use COMP3350-1, COMP3350-2, etc.
    Labs use the same section number but don't affect the count.
    """
    # Map normalized course number -> number of sections from CourseRow
    sections_per_course = {
        normalize_course_number(c.number): c.sections
        for c in courses
    }

    events = []
    for sid, s in schedule["sections"].items():
        if not s.start_time or not s.end_time:
            continue

        # Base display name from the course object (keep formatting as-is)
        base_display_course = s.course_number

        # Key for lookup in sections_per_course (strip spaces, etc.)
        course_key = normalize_course_number(s.course_number)
        total_sections = sections_per_course.get(course_key, 1)

        # Extract section number from the ID, e.g. "COMP3350-2-LAB" -> "2"
        parts = sid.split("-")  # ["COMP3350", "2", "LAB"] or ["COMP3350", "1"]
        section_num = parts[1] if len(parts) >= 2 and parts[1].isdigit() else None

        # Default: just "COMP3350"
        display_course = base_display_course

        # If the course has more than one section, append "-<section_num>"
        if total_sections > 1 and section_num is not None:
            display_course = f"{base_display_course}-{section_num}"

        for day in s.days:
            events.append({
                "id": sid,
                "day": day,
                "course": display_course,  # 👈 now includes -1/-2 only if multi-section
                "prof": s.faculty,
                "room": s.room,
                "start": s.start_time.strftime("%H:%M"),
                "end": s.end_time.strftime("%H:%M"),
                "isLab": getattr(s, "islab", False)
            })

    with open(filename, "w") as f:
        json.dump(events, f, indent=2)
    print(f"✅ Exported {len(events)} events to {filename}")

def course_priority(sec: Section) -> int:
    """
    Priority for scheduling:
      0 = upper-level undergrad (3000–4999)
      1 = masters (5000+)
      2 = lower-level undergrad (0–2999)
    """
    num = sec.course_number
    m = re.search(r"(\d+)", num)
    if not m:
        return 3  # unknown pattern → lowest priority
    level = int(m.group(1))

    if 3000 <= level < 5000:
        return 0      # juniors/seniors
    if level >= 5000:
        return 1      # masters
    if level < 3000:
        return 2      # freshman/sophomore
    return 3

from typing import Dict  # already imported

def load_faculty_load(path: str) -> Dict[str, int]:
    """
    Load max CS course load per faculty from CSV:
    Faculty,CS Course Load
    Sunjae,3
    Micah,3
    """
    limits: Dict[str, int] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            name = (r.get("Faculty") or "").strip()
            if not name:
                continue
            limits[name] = to_int(r.get("CS Course Load", "0"), 0)
    return limits




# ---------- Constraint Checker (Phase 2 skeleton) ----------
class ConstraintChecker:
    """
    Holds all constraints for schedule validation.
    Each method returns True if the schedule satisfies the constraint.
    All constraints can later be toggled on/off by setting active flags.
    """

    def __init__(self, active=True):
        self.active = active

    # ---------------- Faculty Load & Assignment ----------------
    
    # ------------------------------------------------------------
    # C1: Each faculty must be assigned exactly 3 courses
    # ------------------------------------------------------------
    def c1_faculty_three_courses(self, schedule) -> bool:
        """
        C1: Each faculty must be assigned exactly their configured course load
            (from schedule['faculty_load_limits'] if available, else default 3).
        """
        sections = schedule.get("sections", {})
        faculty_count = {}
        for s in sections.values():
            if getattr(s, "islab", False):
                continue  # labs don’t count toward course total
            fac = s.faculty or "TBA"
            faculty_count[fac] = faculty_count.get(fac, 0) + 1

        # load limits from schedule, if present
        limits: Dict[str, int] = schedule.get("faculty_load_limits", {})

        def expected_for(fac: str) -> int:
            if fac in limits:
                return limits[fac]
            if fac == "TBA":
                return 0
            return 3

        ok = True
        for fac, count in faculty_count.items():
            exp = expected_for(fac)
            if fac != "TBA" and count != exp:
                print(f"[C1] ❌ {fac} has {count} assigned courses (expected {exp}).")
                ok = False
        return ok


    # ------------------------------------------------------------
    # C2: Faculty cannot teach more than 9 hours per day
    # ------------------------------------------------------------
    def c2_faculty_daily_limit(self, schedule) -> bool:
        from datetime import datetime, timedelta

        def t2m(t): return t.hour * 60 + t.minute
        sections = schedule["sections"]
        faculty_days = {}

        for s in sections.values():
            if not s.start_time or not s.end_time:
                continue
            fac = s.faculty or "TBA"
            for d in s.days:
                start = t2m(s.start_time)
                end = t2m(s.end_time)
                faculty_days.setdefault(fac, {}).setdefault(d, []).append((start, end))

        ok = True
        for fac, days in faculty_days.items():
            for d, slots in days.items():
                earliest = min(s for s, _ in slots)
                latest = max(e for _, e in slots)
                duration_hr = (latest - earliest) / 60
                if duration_hr > 9:
                    print(f"[C2] ❌ {fac} exceeds 9 hr limit on {d} ({duration_hr:.1f} hr).")
                    ok = False
        return ok

    # ------------------------------------------------------------
    # C3: Faculty cannot teach >2 sections of the same course
    # ------------------------------------------------------------
    def c3_faculty_duplicate_sections(self, schedule) -> bool:
        sections = schedule["sections"]
        faculty_courses = {}
        for s in sections.values():
            fac = s.faculty or "TBA"
            if getattr(s, "islab", False):
                continue
            key = (fac, s.course_number)
            faculty_courses[key] = faculty_courses.get(key, 0) + 1

        ok = True
        for (fac, course), count in faculty_courses.items():
            if fac != "TBA" and count > 2:
                print(f"[C3] ❌ {fac} teaches {count} sections of {course}.")
                ok = False
        return ok

    # ------------------------------------------------------------
    # C4: Faculty cannot teach 5 days a week
    # ------------------------------------------------------------
    def c4_faculty_five_days(self, schedule) -> bool:
        sections = schedule["sections"]
        faculty_days = {}
        for s in sections.values():
            fac = s.faculty or "TBA"
            for d in s.days:
                faculty_days.setdefault(fac, set()).add(d)

        ok = True
        for fac, days in faculty_days.items():
            if fac != "TBA" and len(days) >= 5:
                print(f"[C4] ❌ {fac} teaches {len(days)} days a week ({','.join(sorted(days))}).")
                ok = False
        return ok

    # ------------------------------------------------------------
    # C5: Unassigned courses must be labeled TBA
    # ------------------------------------------------------------
    def c5_unassigned_to_TBA(self, schedule) -> bool:
        sections = schedule["sections"]
        ok = True
        for sid, s in sections.items():
            if not s.faculty:
                print(f"[C5] ⚠️  {sid} has no faculty — auto-set to 'TBA'")
                s.faculty = "TBA"
        return ok

    # ------------------------------------------------------------
    # C6: If a faculty lacks preferred courses, assign COMP1000/1050/2000
    # ------------------------------------------------------------
    def c6_faculty_fill_with_defaults(self, schedule) -> bool:
        """
        Any faculty without at least one of their preferred core courses (1000/1050/2000)
        should automatically get one of them if available.
        """
        sections = schedule["sections"]
        faculty_courses = {}
        for s in sections.values():
            if getattr(s, "islab", False):
                continue
            fac = s.faculty or "TBA"
            faculty_courses.setdefault(fac, []).append(s.course_number)

        ok = True
        for fac, courses in faculty_courses.items():
            if fac == "TBA":
                continue
            # If they lack the intro/core courses
            if not any(c.startswith("COMP1000") or c.startswith("COMP1050") or c.startswith("COMP2000")
                       for c in courses):
                print(f"[C6] ⚠️ {fac} missing core course; suggest assigning COMP1000/1050/2000.")
                ok = False
        return ok

    # ---------------- Course Structure ----------------
    
    # ------------------------------------------------------------
    # C7: Follow lecture/lab duration rules
    # ------------------------------------------------------------
    def c7_course_duration_rules(self, schedule) -> bool:
        """
        Lecture = ~150 min (2.5 hr) or ~180 min (3 hr)
        Lab = ~105 min (1.75 hr) to 120 min (2 hr)
        """
        def t2m(t): return t.hour * 60 + t.minute
        sections = schedule["sections"]
        ok = True

        for sid, s in sections.items():
            if not s.start_time or not s.end_time:
                continue
            duration = t2m(s.end_time) - t2m(s.start_time)
            label = "LAB" if getattr(s, "islab", False) else "LEC"

            if label == "LEC" and not (140 <= duration <= 190):
                print(f"[C7] ❌ {sid}: lecture duration {duration} min invalid (should be ~150–180).")
                ok = False
            elif label == "LAB" and not (100 <= duration <= 130):
                print(f"[C7] ❌ {sid}: lab duration {duration} min invalid (should be ~105–120).")
                ok = False

        return ok

    # ------------------------------------------------------------
    # C8: Match actual hours to defined lecture/lab hours
    # ------------------------------------------------------------
    def c8_follow_course_hours(self, schedule) -> bool:
        """
        Checks that lecture/lab hours match what’s in course_list.
        Here we’ll approximate using name patterns since course_list isn’t passed in.
        """
        def t2m(t): return t.hour * 60 + t.minute
        sections = schedule["sections"]
        ok = True

        # expected hours (as a placeholder rule of thumb)
        # could later be replaced by a proper "course_list" lookup
        expected = {
            "COMP1000": (3, 2),
            "COMP1050": (3, 2),
            "COMP2000": (4, 0),
            "COMP2350": (3, 2),
        }

        for sid, s in sections.items():
            base = s.course_number[:8] if s.course_number.startswith("COMP") else s.course_number
            if base not in expected:
                continue

            lecture_hr, lab_hr = expected[base]
            label = "LAB" if getattr(s, "islab", False) else "LEC"
            duration_hr = (t2m(s.end_time) - t2m(s.start_time)) / 60

            if label == "LEC" and duration_hr < lecture_hr - 0.5:
                print(f"[C8] ❌ {sid}: lecture short ({duration_hr:.1f}h < {lecture_hr}h expected).")
                ok = False
            elif label == "LAB" and duration_hr < lab_hr - 0.5:
                print(f"[C8] ❌ {sid}: lab short ({duration_hr:.1f}h < {lab_hr}h expected).")
                ok = False
        return ok



    # ------------------------------------------------------------
    # C9: Lab must be on a different day than lecture
    # ------------------------------------------------------------
    def c9_lab_separate_day(self, schedule) -> bool:
        sections = schedule["sections"]
        ok = True
        for sid, s in sections.items():
            if not getattr(s, "islab", False):
                continue
            base_id = sid.replace("-LAB", "")
            if base_id in sections:
                lecture_days = sections[base_id].days
                if set(lecture_days) & set(s.days):
                    print(f"[C9] ❌ {sid}: lab and lecture share a day {set(lecture_days) & set(s.days)}.")
                    ok = False
        return ok

    # ------------------------------------------------------------
    # C10: Split lecture evenly across 2 days; lab must be 1-day
    # ------------------------------------------------------------
    def c10_split_lecture_days(self, schedule) -> bool:
        def t2m(t): return t.hour * 60 + t.minute
        sections = schedule["sections"]
        ok = True

        for sid, s in sections.items():
            if not s.start_time or not s.end_time:
                continue
            duration = t2m(s.end_time) - t2m(s.start_time)
            label = "LAB" if getattr(s, "islab", False) else "LEC"
            day_count = len(s.days)

            # lecture: should have 2 days with total ≈ 150–180 min split
            if label == "LEC" and day_count == 2 and not (70 <= duration <= 100):
                print(f"[C10] ❌ {sid}: 2-day lecture not evenly split (~{duration} min each).")
                ok = False
            # lab: must be only one day
            if label == "LAB" and day_count > 1:
                print(f"[C10] ❌ {sid}: lab assigned to multiple days ({s.days}).")
                ok = False
        return ok

    # ---------------- Scheduling Logistics ----------------
    # ------------------------------------------------------------
    # C11: Do not schedule more than 10 sections at the same time on any day
    # ------------------------------------------------------------
    def c11_max_10_sections_same_time(self, schedule) -> bool:
        """
        Uses schedule['global_concurrency'] if available.
        Otherwise, recalculates overlap counts per day/time.
        """
        def overlaps(a1, a2, b1, b2): return not (a2 <= b1 or b2 <= a1)
        def t2m(t): return t.hour * 60 + t.minute

        sections = schedule["sections"]
        day_slots = {d: [] for d in ["M", "T", "W", "Th", "F"]}
        ok = True

        # Populate per-day intervals
        for s in sections.values():
            if not s.start_time or not s.end_time:
                continue
            for d in s.days:
                day_slots[d].append((t2m(s.start_time), t2m(s.end_time), s.section_id))

        # Check concurrency
        for d, slots in day_slots.items():
            for i, (s1, e1, id1) in enumerate(slots):
                concurrent = sum(
                    overlaps(s1, e1, s2, e2) for (s2, e2, _) in slots
                )
                if concurrent > 10:
                    print(f"[C11] ❌ {d} {id1}: {concurrent} concurrent sections (>10).")
                    ok = False
        return ok

    # ------------------------------------------------------------
    # C12: All 5000+ courses must be scheduled after 5 PM
    # ------------------------------------------------------------
    def c12_evening_for_5000plus(self, schedule) -> bool:
        """
        All graduate-level courses (level >= 5000) must start in the 5–8 PM window.
        """
        ok = True
        for sid, s in schedule["sections"].items():
            if not s.start_time:
                continue
            if is_grad_course(s.course_number):
                if not (17 <= s.start_time.hour < 20):
                    print(f"[C12] ❌ {sid}: {s.course_number} starts outside 5–8 PM ({s.start_time}).")
                    ok = False
        return ok


    # ------------------------------------------------------------
    # C13: Assign same faculty for lecture and lab
    # ------------------------------------------------------------
    def c13_same_faculty_for_lab_lecture(self, schedule) -> bool:
        sections = schedule["sections"]
        ok = True
        for sid, s in sections.items():
            if not getattr(s, "islab", False):
                continue
            base = sid.replace("-LAB", "")
            if base in sections:
                lec_fac = sections[base].faculty
                if s.faculty != lec_fac:
                    print(f"[C13] ❌ {sid}: lab faculty {s.faculty} ≠ lecture faculty {lec_fac}.")
                    ok = False
        return ok


    # ------------------------------------------------------------
    # C14: No more than 2 same course numbers at same days/times
    # ------------------------------------------------------------
    def c14_duplicate_course_time_conflict(self, schedule) -> bool:
        def t2m(t): return t.hour * 60 + t.minute
        sections = schedule["sections"]
        ok = True
        seen = {}  # key = (course, tuple(days), start, end)
        for sid, s in sections.items():
            if not s.start_time or not s.end_time:
                continue
            key = (s.course_number, tuple(sorted(s.days)),
                   t2m(s.start_time), t2m(s.end_time))
            seen[key] = seen.get(key, 0) + 1

        for key, count in seen.items():
            course, days, start, end = key
            if count > 2:
                print(f"[C14] ❌ {course} has {count} sections at same time ({days} {start//60:02d}:{start%60:02d}).")
                ok = False
        return ok

    # ------------------------------------------------------------
    # C15: Lecture day patterns must be MW, TTh, or WF
    # ------------------------------------------------------------
    def c15_lecture_day_patterns(self, schedule) -> bool:
        valid_patterns = [set(["M", "W"]), set(["T", "Th"]), set(["W", "F"])]
        ok = True
        for sid, s in schedule["sections"].items():
            if getattr(s, "islab", False):
                continue
            if len(s.days) == 2 and set(s.days) not in valid_patterns:
                print(f"[C15] ❌ {sid}: invalid 2-day pattern {s.days}.")
                ok = False
        return ok

    # ------------------------------------------------------------
    # C16: Balance total number of courses offered per day
    # ------------------------------------------------------------
    def c16_balance_courses_per_day(self, schedule) -> bool:
        """
        Each weekday should have a roughly similar number of total sections.
        Flags if any day deviates >40% from the average.
        """
        day_count = {d: 0 for d in ["M", "T", "W", "Th", "F"]}
        for s in schedule["sections"].values():
            for d in s.days:
                day_count[d] += 1

        avg = sum(day_count.values()) / len(day_count)
        ok = True
        for d, c in day_count.items():
            if abs(c - avg) > 0.4 * avg:
                print(f"[C16] ⚠️  {d}: {c} sections (unbalanced vs avg {avg:.1f}).")
                ok = False
        return ok

    # Utility: run all checks
    def run_all(self, schedule) -> bool:
        """Run all active constraints (c1–c16) and return True if all pass."""
        constraint_methods = []
        for name in dir(self):
            match = re.match(r"c(\d+)_", name)
            if match:
                number = int(match.group(1))
                constraint_methods.append((number, name, getattr(self, name)))

        # Sort numerically (C1 → C16)
        constraint_methods.sort(key=lambda x: x[0])

        print("\n================== RUNNING CONSTRAINT VALIDATION ==================")
        results = []

        for num, name, method in constraint_methods:
            title = f"C{num:02d} - {method.__doc__.strip() if method.__doc__ else ''}"
            print(f"\n▶️ {title}")
            try:
                result = method(schedule)
            except Exception as e:
                print(f"   ⚠️ Error while running {name}: {e}")
                result = False
            print(f"   {'✅ PASS' if result else '❌ FAIL'}")
            results.append((name, result))

        failed = [name for name, ok in results if not ok]

        print("\n====================== SUMMARY REPORT ========================")
        print(f"Checked {len(results)} total constraints.")
        if failed:
            print(f"❌ Failed: {', '.join(failed)}")
            print("==============================================================\n")
            return False
        else:
            print("✅ All constraints passed successfully.")
            print("==============================================================\n")
            return True
        





# ---------- quick preview ----------
# def main():
#     courses = load_courses("data/course_list.csv")
#     prefs = load_preferences("data/prof_preferences.csv")
#     rooms = load_rooms("data/rooms.csv")
#     times = load_times("data/timings.csv")

#     print("\n--- Loaded summary ---")
#     print(f"Courses: {len(courses)}   Rooms: {len(rooms)}   TimeSlots: {len(times)}   Pref rows: {len(prefs)}")

#     print("\nFirst 3 courses:")
#     for c in courses[:3]:
#         print(f"  {c.number} | {c.name} | days/wk={c.lecture_days_per_week} "
#               f"lec_hrs={c.lecture_hours} lab_hrs={c.lab_hours} sections={c.sections} "
#               f"pref_room={c.preferred_room}")

#     print("\nFirst 3 rooms:")
#     for r in rooms[:3]:
#         print(f"  {r.room} ({r.type}) cap={r.capacity}")

#     print("\nFirst 3 time slots:")
#     for t in times[:3]:
#         day_str = "".join(t.days_allowed)
#         print(f"  {t.slot_label} {t.start.strftime('%H:%M')}–{t.stop.strftime('%H:%M')} "
#               f"{t.duration_min}min days={day_str} evening={t.evening}")

#     # show preferences for the first 3 courses if present
#     if courses:
#         print()
#         for c in courses[:3]:
#             pr = prefs.get(c.number)
#             print(f"Prefs for {c.number}: {pr.faculty_ranked if pr else '—'}")

#         # ---------- Create section objects ----------
#     sections = create_sections(courses, prefs)

#     print(f"\nTotal sections created: {len(sections)}")
#     print("Preview of first few sections:")
#     for s in sections[:5]:
#         print(f"  {s.id} | {s.course_name} | faculty={s.faculty_options} | "
#               f"lec_days={s.lecture_days_per_week}, hrs={s.lecture_hours}, "
#               f"lab={s.lab_hours}, pref_room={s.preferred_room}")
        
#      # --- Test ConstraintChecker (temporary demo) ---
#     print("\n--- Constraint Checker Test ---")
#     dummy_schedule = {}
#     checker = ConstraintChecker()

#     result = checker.run_all(dummy_schedule)
#     print("All constraints passed:", result)

#     print("\n--- Constraint Checker Test ---")
#     dummy_schedule = {}
#     checker = ConstraintChecker()
#     checker.run_all(dummy_schedule)

#     print("\n--- Initialize Schedule Test ---")
#     # Example: create sections and build the base schedule dict
#     courses = load_courses("data/course_list.csv")
#     prefs = load_preferences("data/prof_preferences.csv")
#     sections = create_sections(courses, prefs)
#     schedule = initialize_schedule(sections)

#     # Preview 3 sections
#     for sid, s in list(schedule["sections"].items())[:3]:
#         print(f"{sid}: faculty={s.faculty}, room={s.room}, days={s.days}")

def export_schedule_to_excel(schedule, filename="schedule_excel.csv"):
    """
    Export schedule to a CSV that opens cleanly in Excel with columns:
    CRN, Subj, Crse, Section, Location, Credit, Title, Days, Time,
    Cap., Act., Rem, Instructor, Date (MM/DD).

    Per your instructions: CRN, Credit, Act., Rem, Date left empty.
    """
    import csv

    sections = schedule.get("sections", {})
    # course_titles is added to schedule in main() below
    course_titles = schedule.get("course_titles", {})

    def split_subj_crse(course_number: str) -> tuple[str, str]:
        # e.g. "COMP1000" -> ("COMP", "1000"), "DATA6100" -> ("DATA", "6100")
        subj = ""
        crse = ""
        m_subj = re.match(r"([A-Za-z]+)", course_number or "")
        if m_subj:
            subj = m_subj.group(1)
        m_num = re.search(r"(\d+)", course_number or "")
        if m_num:
            crse = m_num.group(1)
        return subj, crse

    def section_from_id(section_id: str) -> str:
        # "COMP1000-1" -> "1", "COMP1000-1-LAB" -> "1L"
        parts = section_id.split("-")
        if len(parts) < 2:
            return ""
        base_sec = parts[1]
        if len(parts) >= 3 and parts[2].upper() == "LAB":
            return f"{base_sec}L"
        return base_sec

    def format_days(days: list[str]) -> str:
        # ["M","W","F"] -> "MWF", ["T","Th"] -> "TTh"
        return "".join(days) if days else ""

    def format_time_range(start_t: time, end_t: time) -> str:
        # 14:00, 14:50 -> "02:00 pm-02:50 pm"
        def fmt(t: time) -> str:
            h = t.hour
            m = t.minute
            ampm = "am" if h < 12 else "pm"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            return f"{h12:02d}:{m:02d} {ampm}"
        return f"{fmt(start_t)}-{fmt(end_t)}"

    # default capacity if nothing else is known
    DEFAULT_CAP = 25

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Header row
        writer.writerow([
            "CRN", "Subj", "Crse", "Section", "Location",
            "Credit", "Title", "Days", "Time",
            "Cap.", "Act.", "Rem", "Instructor", "Date (MM/DD)"
        ])

        for sid, s in sections.items():
            if not s.start_time or not s.end_time:
                continue

            course_number = s.course_number
            subj, crse = split_subj_crse(course_number)
            section = section_from_id(sid)
            location = s.room or ""
            title = course_titles.get(normalize_course_number(course_number), "")

            # 👉 Append " - LAB" for lab sections
            if getattr(s, "islab", False):
                if title:
                    title = f"{title} - LAB"
                else:
                    title = "LAB"


            days_str = format_days(s.days)
            time_str = format_time_range(s.start_time, s.end_time)

            # Per your request, keep these blank:
            crn = ""
            credit = ""
            act = ""
            rem = ""
            date_str = ""

            cap = DEFAULT_CAP  # or you can customize later if you track per-course caps

            instructor = s.faculty or ""

            writer.writerow([
                crn,          # CRN (empty)
                subj,         # Subj (e.g. COMP)
                crse,         # Crse (e.g. 1000)
                section,      # Section (e.g. 1, 1L for labs)
                location,     # Location (room)
                credit,       # Credit (empty)
                title,        # Title (course name)
                days_str,     # Days (MWF, TTh, etc.)
                time_str,     # Time ("02:00 pm-02:50 pm")
                cap,          # Cap. (default 25)
                act,          # Act. (empty)
                rem,          # Rem (empty)
                instructor,   # Instructor
                date_str      # Date (MM/DD) empty
            ])

    print(f"✅ Exported Excel-style CSV to {filename}")

if __name__ == "__main__":
    import sys, io, contextlib

    class Tee(io.TextIOBase):
        def __init__(self, *streams):
            self.streams = streams

        def write(self, s):
            for st in self.streams:
                try:
                    if not getattr(st, "closed", False):
                        st.write(s)
                        st.flush()
                except ValueError:
                    # Stream may already be closed
                    pass
            return len(s)

        def flush(self):
            for st in self.streams:
                try:
                    if not getattr(st, "closed", False):
                        st.flush()
                except ValueError:
                    # Stream may already be closed
                    pass


    with open("result.txt", "w", encoding="utf-8") as log_file:
        tee = Tee(sys.stdout, log_file)
        with contextlib.redirect_stdout(tee):
            # ================== LOAD ALL INPUT DATA ==================
            courses = load_courses("data/course_list.csv")
            prefs = load_preferences("data/prof_preferences.csv")
            times = load_times("data/timings.csv")
            faculty_limits = load_faculty_load("data/faculty_load.csv")
            rooms = load_rooms("data/rooms.csv")
            room_prefs = load_room_preferences("data/room_preferences.csv")

            # Build a map course_number -> title for Excel export
            course_titles = {
                normalize_course_number(c.number): c.name
                for c in courses
            }

            # ================== BUILD SECTIONS & BASE SCHEDULE ==================
            sections = create_sections(courses, prefs)
            schedule = initialize_schedule(sections)

            # Faculty assignment (using faculty_load.csv limits)
            assignment = assign_faculty(sections, prefs, faculty_limits)
            update_schedule_with_faculty(schedule, assignment)
            schedule["faculty"] = build_faculty_schedule(assignment, sections)
            schedule["faculty_load_limits"] = faculty_limits  # for ConstraintChecker C1
            schedule["course_titles"] = course_titles         # for Excel export

            # Time and lab assignment
            assign_times_and_labs(schedule, sections, times, rooms, room_prefs)

            sections_map = schedule.get("sections", {})

            # ================== 1️⃣ GLOBAL SUMMARY ==================
            total_courses = len({c.number for c in courses})
            total_sections = sum(1 for s in sections_map.values() if not getattr(s, "islab", False))
            total_labs = sum(1 for s in sections_map.values() if getattr(s, "islab", False))
            total_faculty = len({s.faculty for s in sections_map.values() if s.faculty and s.faculty != "TBA"})
            am_pm = schedule.get("am_pm_counts", {})
            


            print("\n====================== GLOBAL SUMMARY ======================")
            print(f"Courses in course_list.csv      : {total_courses}")
            print(f"Lecture sections (no labs)      : {total_sections}")
            print(f"Lab sections                    : {total_labs}")
            print(f"Unique assigned faculty (≠ TBA) : {total_faculty}")
            print(f"AM/PM balance                   : {am_pm}")
            print("============================================================\n")

            # ================== 2️⃣ COURSE SECTION COUNTS ==================
            print("================ COURSE SECTION COUNTS (LEC vs LAB) ================")
            course_counts: dict[str, dict[str, int]] = {}

            for s in sections_map.values():
                course = s.course_number
                entry = course_counts.setdefault(course, {"lec": 0, "lab": 0})
                if getattr(s, "islab", False):
                    entry["lab"] += 1
                else:
                    entry["lec"] += 1

            for course in sorted(course_counts.keys()):
                lec = course_counts[course]["lec"]
                lab = course_counts[course]["lab"]
                if lab == 0:
                    lab_str = "no labs"
                else:
                    lab_str = f"{lab} lab section(s)"
                print(f"{course}: {lec} lecture section(s), {lab_str}")
            print("====================================================================\n")

            # ================== 3️⃣ GRADUATE (5000+) CLASS TIMINGS ==================
            print("=================== GRADUATE CLASS TIMINGS (5000+) ==================")
            grad_by_course: dict[str, list[tuple[str, SectionAssignment]]] = {}
            for sid, s in sections_map.items():
                if not s.start_time or not s.end_time:
                    continue
                if not is_grad_course(s.course_number):
                    continue
                grad_by_course.setdefault(s.course_number, []).append((sid, s))

            if not grad_by_course:
                print("No graduate-level (5000+) sections found in this schedule.")
            else:
                for course in sorted(grad_by_course.keys()):
                    print(f"\n{course}:")
                    for sid, s in sorted(grad_by_course[course], key=lambda x: (x[1].days, x[1].start_time)):
                        start = s.start_time.strftime("%H:%M")
                        end = s.end_time.strftime("%H:%M")
                        days = "".join(s.days) if s.days else "-"
                        label = "LAB" if getattr(s, "islab", False) else "LEC"
                        print(f"  - {sid} [{label}] {days} {start}-{end} | faculty={s.faculty} | room={s.room}")
            print("====================================================================\n")

            # ================== 4️⃣ SECTIONS WITH TBA FACULTY ==================
            print("====================== SECTIONS WITH TBA FACULTY ======================")
            tba_sections = [
                (sid, s) for sid, s in sections_map.items()
                if (s.faculty or "TBA") == "TBA"
            ]
            if not tba_sections:
                print("No sections with TBA faculty.")
            else:
                for sid, s in sorted(tba_sections, key=lambda x: (x[1].course_number, x[0])):
                    start = s.start_time.strftime("%H:%M") if s.start_time else "-"
                    end = s.end_time.strftime("%H:%M") if s.end_time else "-"
                    days = "".join(s.days) if s.days else "-"
                    label = "LAB" if getattr(s, "islab", False) else "LEC"
                    print(f"  - {sid}: {s.course_number} ({label}) {days} {start}-{end} in {s.room}")
            print("====================================================================\n")

            # ================== 5️⃣ FACULTY TEACHING ASSIGNMENTS ==================
            print("================== FACULTY TEACHING ASSIGNMENTS ==================")
            faculty_map: dict[str, list[tuple[str, SectionAssignment]]] = {}
            for sid, s in sections_map.items():
                fac = s.faculty or "TBA"
                faculty_map.setdefault(fac, []).append((sid, s))

            for fac, sec_list in sorted(faculty_map.items()):
                lec_count = sum(1 for sid, s in sec_list if not getattr(s, "islab", False))
                target = schedule.get("faculty_load_limits", {}).get(fac, None)
                if fac == "TBA":
                    header = f"\n{fac}: {len(sec_list)} section(s) [labs+lec]"
                elif target is not None:
                    header = f"\n{fac}: {len(sec_list)} section(s) [lec load={lec_count}, target={target}]"
                else:
                    header = f"\n{fac}: {len(sec_list)} section(s) [lec load={lec_count}]"
                print(header)

                for sid, s in sorted(sec_list, key=lambda x: (x[1].course_number, x[0])):
                    start = s.start_time.strftime("%H:%M") if s.start_time else "-"
                    end = s.end_time.strftime("%H:%M") if s.end_time else "-"
                    days = "".join(s.days) if s.days else "-"
                    label = "LAB" if getattr(s, "islab", False) else "LEC"
                    print(f"  - {sid}: {s.course_number} ({label}) {days} {start}-{end} in {s.room}")
            print("\n=================================================================\n")

            # ================== 6️⃣ SLOT UTILIZATION BY DAY ==================
            print("====================== SLOT UTILIZATION BY DAY ======================")
            slot_load = schedule.get("slot_load", {})
            if slot_load:
                for day, slots in slot_load.items():
                    print(f"\n{day}:")
                    for k, v in sorted(slots.items()):
                        print(f"  {k}: {v} section(s)")
            else:
                print("(No slot load data found.)")
            print("====================================================================\n")

            # ================== 7️⃣ EXPORTS ==================
            export_schedule_to_json(schedule, courses)
            export_schedule_to_excel(schedule, "schedule.csv")

            # Optional: constraints
            # checker = ConstraintChecker()
            # all_ok = checker.run_all(schedule)
            # if not all_ok:
            #     print("⚠️  Some constraints failed, review logs above.")
            # else:
            #     print("🎯 Schedule fully valid!")

