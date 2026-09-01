#!/usr/bin/env python3
"""Independent regression gate for the WIT class scheduler.

This validator deliberately does NOT import main.py and does not read
result.txt. It re-derives every rule from the *inputs* (data/*.csv) and checks
them against the *output* (schedule.json) only. That independence is the whole
point: if main.py's own self-checks are wrong, this file still catches it.

Usage:
    python3 tests/validate_schedule.py [run_dir]

`run_dir` defaults to the current directory and must contain schedule.json and
a data/ folder. Exits 0 if every check passes, 1 if any check FAILs, so it can
be wired straight into CI.

Checks that PASS/FAIL are hard rules. Anything reported as "INFO" is an
observation that is expected, sanctioned, or stricter than the design requires,
and never affects the exit code.
"""
import csv, json, os, re, sys, itertools
from collections import defaultdict

# ── run directory ────────────────────────────────────────────────────────────
RUN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
DATA = os.path.join(RUN, "data")
SCHEDULE = os.path.join(RUN, "schedule.json")
if not os.path.isfile(SCHEDULE) or not os.path.isdir(DATA):
    raise SystemExit(f"expected schedule.json and data/ in {RUN}")

DAYS = ["M", "T", "W", "Th", "F"]
GRAD_MEETING_MIN = 155          # institutional rule: one 155-min evening session
GRAD_MAX_DAY = {"M", "T", "W", "Th"}   # grad sessions never meet Friday
FOUNDATION_COURSES = {"COMP1000", "COMP1050", "COMP2000"}


def t2m(t):
    h, m = t.strip().split(":")[:2]
    return int(h) * 60 + int(m)


def norm_name(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def ncourse(s):
    return (s or "").replace(" ", "").strip()


_SECTION_ID_RE = re.compile(r"^(?P<course>.+)-(?P<sec>\d+)(?P<lab>-LAB)?$", re.IGNORECASE)


def split_section_id(sid):
    """'COMP1050-3' -> ('COMP1050','3',False); 'COMP1050-3-LAB' -> (...,True)."""
    m = _SECTION_ID_RE.match(sid or "")
    if not m:
        return (sid or ""), "", (sid or "").upper().endswith("-LAB")
    return m.group("course"), m.group("sec"), bool(m.group("lab"))


def course_of(sid):
    return ncourse(split_section_id(sid)[0])


def lecture_key(sid):
    """Section identity ignoring the lab suffix: COMP1050-3-LAB -> COMP1050-3."""
    c, n, _ = split_section_id(sid)
    return f"{c}-{n}" if n else c


def course_num(cn):
    m = re.search(r"(\d+)", cn or "")
    return int(m.group(1)) if m else -1


def is_grad(cn):
    return course_num(cn) >= 5000


def subject_of(cn):
    m = re.match(r"([A-Za-z]+)", cn or "")
    return m.group(1).upper() if m else ""


# ── inputs ───────────────────────────────────────────────────────────────────
def rows(prefix):
    for fn in sorted(os.listdir(DATA)):
        if fn.lower().startswith(prefix):
            with open(os.path.join(DATA, fn), newline="", encoding="utf-8-sig") as f:
                return list(csv.DictReader(f))
    raise SystemExit("missing " + prefix)


SETTINGS = {}
for r in rows("settings"):
    SETTINGS[r["setting"].strip()] = r["value"].strip()
MAX_CONCURRENT = int(SETTINGS["max_concurrent_sections"])
MAX_SPAN_H = float(SETTINGS["max_daily_span_hours"])
MAX_DAYS = int(SETTINGS["max_teaching_days"])
GAP_MIN = int(SETTINGS["faculty_gap_minutes"])
DEFAULT_LOAD = int(SETTINGS.get("default_faculty_load", 3))

PREF = {}
for r in rows("prof_preferences"):
    cn = ncourse(r.get("Course Number"))
    if cn:
        PREF[cn] = [n.strip() for n in (r.get("Faculty") or "").split(",") if n.strip()]
PREF_NORM = {c: {norm_name(n): n for n in v} for c, v in PREF.items()}

ROOMS = {}
for r in rows("rooms"):
    ROOMS[r["Room"].strip()] = (r["Type"].strip(), int(float(r["Capacity"] or 0)))

COURSES = {}
for r in rows("course-list"):
    cn = ncourse(r.get("Course number"))
    if not cn:
        continue

    def _f(k, r=r):
        v = (r.get(k) or "").strip()
        try:
            return float(v)
        except Exception:
            return None
    COURSES[cn] = dict(days=_f("lecture days per week"), lec_h=_f("lecture hours"),
                       lab_h=_f("lab hours"), nsec=_f("number of sections"))

SLOTS = []
for r in rows("timings"):
    SLOTS.append(dict(start=r["start_time"][:5], dur=int(r["duration_min"]),
                      label=r["slot_label"].strip(),
                      evening=(r.get("evening") or "").strip().upper() == "TRUE",
                      days=[d.strip() for d in r["Days Allowed"].split(",")]))
SLOT_KEYS = {(s["start"], s["dur"]) for s in SLOTS}
SLOT_DAYS = defaultdict(set)
SLOT_EVENING = defaultdict(bool)
for s in SLOTS:
    SLOT_DAYS[(s["start"], s["dur"])] |= set(s["days"])
    if s["evening"]:
        SLOT_EVENING[(s["start"], s["dur"])] = True

MPATS = []
for r in rows("meeting_patterns"):
    def _f(k, r=r):
        v = (r.get(k) or "").strip()
        return float(v) if v else None
    subj = (r.get("subject") or "").strip()
    MPATS.append(dict(subject=None if subj in ("", "*") else subj.upper(),
                      days=_f("lecture_days_per_week"), lec_h=_f("lecture_hours"),
                      meet=_f("meeting_minutes"), lab=_f("lab_minutes")))

GROUPS = defaultdict(list)
for r in rows("non_overlap_groups"):
    GROUPS[r["group"].strip()].append(ncourse(r["course_number"]))

FACULTY_RAW = rows("faculty_load")

# ── output ───────────────────────────────────────────────────────────────────
EV = json.load(open(SCHEDULE, encoding="utf-8"))
for e in EV:
    e["s"] = t2m(e["start"])
    e["e"] = t2m(e["end"])
    e["cn"] = course_of(e["id"])
    e["seckey"] = lecture_key(e["id"])
    e["ph"] = bool(e.get("unplaced")) or e["room"] == "UNPLACED"
PLACED = [e for e in EV if not e["ph"]]

# ── reporting ────────────────────────────────────────────────────────────────
RESULTS = []


def report(num, title, viols, info=None):
    print("\n" + "=" * 78)
    print(f"CHECK {num}: {title}")
    print("=" * 78)
    for line in (info or []):
        print("  INFO " + line)
    if not viols:
        print("  RESULT: PASS")
        RESULTS.append((num, title, "PASS", 0))
    else:
        print(f"  RESULT: FAIL  ({len(viols)} violation(s))")
        for v in viols[:15]:
            print("    - " + v)
        if len(viols) > 15:
            print(f"    ... and {len(viols)-15} more")
        RESULTS.append((num, title, "FAIL", len(viols)))


def overlaps(a, b):
    return a["s"] < b["e"] and b["s"] < a["e"]


# ── 1. ROOM DOUBLE-BOOKING ───────────────────────────────────────────────────
v = []
by = defaultdict(list)
for e in PLACED:
    by[(e["room"], e["day"])].append(e)
for (room, day), evs in sorted(by.items()):
    for a, b in itertools.combinations(sorted(evs, key=lambda x: x["s"]), 2):
        if overlaps(a, b):
            v.append(f"{room} {day}: {a['id']} {a['start']}-{a['end']} OVERLAPS "
                     f"{b['id']} {b['start']}-{b['end']}")
report(1, "ROOM DOUBLE-BOOKING", v)

# ── 2. PROFESSOR DOUBLE-BOOKING ──────────────────────────────────────────────
v = []
by = defaultdict(list)
for e in EV:
    if e["prof"] != "TBA":
        by[(e["prof"], e["day"])].append(e)
for (p, day), evs in sorted(by.items()):
    for a, b in itertools.combinations(sorted(evs, key=lambda x: x["s"]), 2):
        if overlaps(a, b):
            v.append(f"{p} {day}: {a['id']} {a['start']}-{a['end']} @{a['room']} OVERLAPS "
                     f"{b['id']} {b['start']}-{b['end']} @{b['room']}")
report(2, "PROFESSOR DOUBLE-BOOKING", v)

# ── 3. PROFESSOR QUALIFICATION (top-up is a sanctioned exception) ────────────
v, sanctioned, dq = [], [], []
seen = set()
for e in EV:
    if e["prof"] == "TBA":
        continue
    key = (e["seckey"], e["prof"])
    if key in seen:
        continue
    seen.add(key)
    cn = e["cn"]
    if cn not in PREF:
        v.append(f"{e['id']} prof '{e['prof']}': course {cn} has NO row in prof_preferences.csv")
        continue
    nm = norm_name(e["prof"])
    if nm in PREF_NORM[cn]:
        raw = PREF_NORM[cn][nm]
        if raw != e["prof"]:
            dq.append(f"{cn}: schedule '{e['prof']}' vs CSV '{raw}'")
        continue
    # not on the preference row
    if e.get("topup") and cn in FOUNDATION_COURSES:
        sanctioned.append(f"{e['seckey']} ({cn}) -> {e['prof']}")
    elif e.get("topup"):
        pass          # non-foundation topup: reported as a violation just below
    else:
        v.append(f"{e['id']} ({cn}) prof '{e['prof']}' NOT in prof_preferences: {PREF[cn]}")

# a topup outside the three foundation courses is NOT sanctioned
topup_courses = sorted({e["cn"] for e in EV if e.get("topup")})
for cn in topup_courses:
    if cn not in FOUNDATION_COURSES:
        ids = sorted({e["seckey"] for e in EV if e.get("topup") and e["cn"] == cn})
        v.append(f"topup flag on NON-foundation course {cn} ({ids}); the sanctioned "
                 f"exception is limited to {sorted(FOUNDATION_COURSES)}")

info = [f"sanctioned top-up exceptions ({len(set(sanctioned))}): "
        + ("; ".join(sorted(set(sanctioned))) if sanctioned else "none"),
        f"courses carrying a topup flag: {topup_courses or 'none'} "
        f"(allowed: {sorted(FOUNDATION_COURSES)})",
        f"case/whitespace normalizations needed to match names: "
        + ("; ".join(sorted(set(dq))[:8]) if dq else "none")]
report(3, "PROFESSOR QUALIFICATION (topup = sanctioned exception)", v, info)

# ── 4. FACULTY LOAD CAP ──────────────────────────────────────────────────────
loadrows = defaultdict(list)
for r in FACULTY_RAW:
    loadrows[r["Faculty"].strip()].append(int(float(r["CS Course Load"] or 0)))
dups = {k: vv for k, vv in loadrows.items() if len(vv) > 1}
CAP = {k: vv[-1] for k, vv in loadrows.items()}

sec_by_prof = defaultdict(set)
for e in EV:
    if e["prof"] != "TBA":
        sec_by_prof[e["prof"]].add(e["seckey"])
v = []
for p, secs in sorted(sec_by_prof.items()):
    cap = CAP.get(p, DEFAULT_LOAD)
    if p not in CAP:
        v.append(f"{p}: {len(secs)} section(s) but NOT LISTED in faculty_load.csv")
        continue
    if len(secs) > cap:
        v.append(f"{p}: {len(secs)} distinct sections > cap {cap} -> {sorted(secs)}")
report(4, "FACULTY LOAD CAP", v,
       [f"faculty_load.csv duplicate rows (last-wins used): {dups if dups else 'none'}",
        f"named-prof sections = {len({e['seckey'] for e in EV if e['prof'] != 'TBA'})}, "
        f"TBA sections = {len({e['seckey'] for e in EV if e['prof'] == 'TBA'})}"])

# ── 5. MAX TEACHING DAYS ─────────────────────────────────────────────────────
v = []
days_by_prof = defaultdict(set)
for e in EV:
    if e["prof"] != "TBA":
        days_by_prof[e["prof"]].add(e["day"])
for p, ds in sorted(days_by_prof.items()):
    if len(ds) > MAX_DAYS:
        v.append(f"{p}: teaches {len(ds)} days {sorted(ds)} > max_teaching_days={MAX_DAYS}")
report(5, f"MAX TEACHING DAYS (<= {MAX_DAYS})", v)

# ── 6. MAX DAILY SPAN ────────────────────────────────────────────────────────
v = []
byd = defaultdict(list)
for e in EV:
    if e["prof"] != "TBA":
        byd[(e["prof"], e["day"])].append(e)
for (p, d), evs in sorted(byd.items()):
    span = max(x["e"] for x in evs) - min(x["s"] for x in evs)
    if span > MAX_SPAN_H * 60:
        v.append(f"{p} {d}: span {span/60:.2f}h > {MAX_SPAN_H}h")
report(6, f"MAX DAILY SPAN (<= {MAX_SPAN_H}h)", v)

# ── 7. FACULTY GAP ───────────────────────────────────────────────────────────
v = []
for (p, d), evs in sorted(byd.items()):
    evs = sorted(evs, key=lambda x: x["s"])
    for a, b in zip(evs, evs[1:]):
        gap = b["s"] - a["e"]
        if gap < GAP_MIN:
            v.append(f"{p} {d}: {a['id']} ends {a['end']} -> {b['id']} starts {b['start']} "
                     f"= {gap} min gap (< {GAP_MIN})")
report(7, f"FACULTY GAP (>= {GAP_MIN} min)", v,
       ["main.py's times_conflict() requires end1 + faculty_gap_minutes <= start2, "
        "so back-to-back with a 0-minute gap is a violation."])

# ── 8. MAX CONCURRENT SECTIONS (placed only) ─────────────────────────────────
def peak(events):
    best = (0, None, None, [])
    for d in DAYS:
        de = [e for e in events if e["day"] == d]
        for t in range(0, 24 * 60):
            n = [e for e in de if e["s"] <= t < e["e"]]
            if len(n) > best[0]:
                best = (len(n), d, t, sorted({e["id"] for e in n}))
    return best


PK_PLACED = peak(PLACED)
PK_ALL = peak(EV)
v = []
if PK_PLACED[0] > MAX_CONCURRENT:
    v.append(f"{PK_PLACED[1]} {PK_PLACED[2]//60:02d}:{PK_PLACED[2]%60:02d} -> "
             f"{PK_PLACED[0]} concurrent placed sections (cap {MAX_CONCURRENT}): {PK_PLACED[3]}")
report(8, f"MAX CONCURRENT SECTIONS (<= {MAX_CONCURRENT})", v,
       [f"peak concurrency among genuinely-placed events = {PK_PLACED[0]} "
        f"({PK_PLACED[1]} {PK_PLACED[2]//60:02d}:{PK_PLACED[2]%60:02d})",
        f"peak if UNPLACED placeholders were counted = {PK_ALL[0]} "
        f"({PK_ALL[1]} {PK_ALL[2]//60:02d}:{PK_ALL[2]%60:02d})"])

# ── 9. ROOM EXISTENCE + TYPE ─────────────────────────────────────────────────
v = []
type_counts = defaultdict(int)
for _r, (typ, _c) in ROOMS.items():
    type_counts[typ.strip().lower()] += 1
for e in PLACED:
    r = ROOMS.get(e["room"])
    if r is None:
        v.append(f"{e['id']} {e['day']} in room '{e['room']}' which is NOT in rooms.csv")
        continue
    typ = r[0].strip().lower()
    if e["isLab"] and typ not in ("lab", "both"):
        v.append(f"{e['id']} {e['day']} LAB placed in '{e['room']}' whose Type is '{r[0]}'")
    if (not e["isLab"]) and typ not in ("lecture", "both"):
        v.append(f"{e['id']} {e['day']} LECTURE placed in '{e['room']}' whose Type is '{r[0]}'")
report(9, "ROOM EXISTENCE + ROOM TYPE (Lab/Lecture/Both)", v,
       [f"rooms.csv Type distribution: {dict(type_counts)}",
        "If every room is 'Both' this check passes trivially and proves nothing "
        "about type enforcement; to prove it, re-run with some rooms restricted.",
        "schedule.json carries no enrollment figure, so seat capacity cannot be "
        "checked from the output alone."])

# ── 10. NON-OVERLAP GROUPS (directional, per the design) ─────────────────────
# Design intent: within a group, the COHORT-SPECIFIC course (the one belonging
# to FEWER groups) must have every one of its sections able to pair with SOME
# section of the shared/gateway course. The reverse is NOT required — a student
# in a conflicting section of the shared course simply picks another section.
sec_events = defaultdict(list)          # section key -> lecture events
sec_events_all = defaultdict(list)      # section key -> lecture + lab events
for e in EV:
    sec_events_all[e["seckey"]].append(e)
    if not e["isLab"]:
        sec_events[e["seckey"]].append(e)

by_course = defaultdict(list)           # course -> placed lecture section keys
placeholder_only = defaultdict(list)
for k, evs in sec_events.items():
    cn = course_of(k)
    if any(x["ph"] for x in evs):
        placeholder_only[cn].append(k)
    else:
        by_course[cn].append(k)

group_membership = defaultdict(int)
for courses in GROUPS.values():
    for c in courses:
        group_membership[c] += 1


def conflict(k1, k2, table):
    for a in table[k1]:
        for b in table[k2]:
            if a["day"] == b["day"] and overlaps(a, b):
                return True
    return False


v, info = [], []
for g, courses in sorted(GROUPS.items()):
    present = sorted(c for c in courses if c in by_course)
    for c in courses:
        if c in placeholder_only:
            info.append(f"{g}: {c} has UNPLACED placeholder section(s) "
                        f"{sorted(placeholder_only[c])} — pairings involving it are SKIPPED "
                        f"(a placeholder holds an invented time, so any verdict would be fake)")
    if len(present) < 2:
        info.append(f"{g}: only {len(present)} of {len(courses)} course(s) are placed — "
                    f"group has no effect")
        continue
    for c1, c2 in itertools.combinations(present, 2):
        g1, g2 = group_membership[c1], group_membership[c2]
        if g1 != g2:
            c1_small = g1 < g2
        else:
            c1_small = len(by_course[c1]) <= len(by_course[c2])
        small, big = (c1, c2) if c1_small else (c2, c1)
        uncovered = [k for k in sorted(by_course[small])
                     if not any(not conflict(k, k2, sec_events) for k2 in by_course[big])]
        if uncovered:
            v.append(f"{g}: {small} (cohort-specific, in {group_membership[small]} group(s)) / "
                     f"{big} (shared, in {group_membership[big]} group(s)) — {len(uncovered)} of "
                     f"{len(by_course[small])} {small} section(s) have NO non-conflicting {big} "
                     f"option: {uncovered}")
        # strict pairwise disjointness — reported as INFO only
        strict = []
        for k in sorted(by_course[big]):
            if not any(not conflict(k, k2, sec_events) for k2 in by_course[small]):
                strict.append(k)
        if strict:
            info.append(f"{g}: {strict} (sections of the SHARED course {big}) have no "
                        f"non-conflicting {small} option — stricter than the design requires, "
                        f"not a failure: students take a different {big} section")
        # same coverage recomputed including lab meetings
        unc_lab = [k for k in sorted(by_course[small])
                   if not any(not conflict(k, k2, sec_events_all) for k2 in by_course[big])]
        if unc_lab and unc_lab != uncovered:
            info.append(f"{g}: counting LAB meetings too, {small} section(s) {unc_lab} lose "
                        f"their {big} option — informational; the design check is lecture-only")
report(10, "NON-OVERLAP GROUPS (directional coverage, recomputed independently)", v, info[:25])

# ── 11. MEETING PATTERN / SLOT LEGALITY ──────────────────────────────────────
def rule_for(cn, days, hours):
    subj = subject_of(cn)
    matches = []
    for p in MPATS:
        if p["days"] is not None and days is not None and p["days"] != days:
            continue
        if p["subject"] and p["subject"] != subj:
            continue
        if p["lec_h"] is not None and hours is not None and p["lec_h"] != hours:
            continue
        matches.append(p)
    if not matches:
        return None
    best = max((2 if p["subject"] else 0) + (1 if p["lec_h"] is not None else 0) for p in matches)
    return [p for p in matches
            if (2 if p["subject"] else 0) + (1 if p["lec_h"] is not None else 0) == best][-1]


v, info = [], []
fallbacks = []
lect = defaultdict(list)
for e in EV:
    if not e["isLab"]:
        lect[e["seckey"]].append(e)

for k, evs in sorted(lect.items()):
    cn = course_of(k)
    if any(x["ph"] for x in evs):
        continue                      # placeholder times are invented; check 12 owns them
    ndays = len({x["day"] for x in evs})
    durs = sorted({x["e"] - x["s"] for x in evs})
    if len(durs) > 1:
        v.append(f"{k}: INCONSISTENT durations across its own meetings: {durs} min")
    if is_grad(cn):
        # Institutional rule, applied BEFORE meeting_patterns.csv:
        # exactly one 155-min session, Mon-Thu.
        if durs and durs[0] != GRAD_MEETING_MIN:
            v.append(f"{k} (grad): {durs[0]} min, must be exactly {GRAD_MEETING_MIN} min")
        if ndays != 1:
            v.append(f"{k} (grad): meets {ndays} day(s) {sorted({x['day'] for x in evs})}, "
                     f"must be exactly 1")
        for x in evs:
            if x["day"] not in GRAD_MAX_DAY:
                v.append(f"{k} (grad): meets on {x['day']}; grad sessions must be Mon-Thu")
    else:
        want_days = COURSES.get(cn, {}).get("days")
        hours = COURSES.get(cn, {}).get("lec_h")
        if want_days is not None and ndays != want_days:
            v.append(f"{k}: meets {ndays} day(s) {sorted({x['day'] for x in evs})} but the "
                     f"course list declares {int(want_days)} lecture days/week")
        rule = rule_for(cn, want_days, hours)
        if rule is None:
            fallbacks.append(f"{k} ({cn}, days={want_days}, hours={hours}) -> {durs} min")
        elif durs and durs[0] != rule["meet"]:
            v.append(f"{k} ({cn}): scheduled {durs[0]} min but meeting_patterns.csv requires "
                     f"{int(rule['meet'])} min (days={want_days}, hours={hours})")
    for x in evs:
        key = (x["start"], x["e"] - x["s"])
        if key not in SLOT_KEYS:
            v.append(f"{k} {x['day']} {x['start']}-{x['end']} ({x['e']-x['s']} min): NOT a slot "
                     f"in timings.csv")
        elif x["day"] not in SLOT_DAYS[key]:
            v.append(f"{k} {x['day']} {x['start']}: slot's Days Allowed is "
                     f"{sorted(SLOT_DAYS[key])} — {x['day']} NOT allowed")

for e in EV:
    if e["isLab"] and not e["ph"]:
        key = (e["start"], e["e"] - e["s"])
        if key not in SLOT_KEYS:
            v.append(f"{e['id']} LAB {e['day']} {e['start']}-{e['end']} "
                     f"({e['e']-e['s']} min): NOT a slot in timings.csv")
        elif e["day"] not in SLOT_DAYS[key]:
            v.append(f"{e['id']} LAB {e['day']} {e['start']}: day not in slot's Days Allowed")

info.append(f"graduate courses are validated against the institutional rule "
            f"({GRAD_MEETING_MIN} min, 1 day, Mon-Thu), NOT meeting_patterns.csv")
info.append(f"non-grad sections with no meeting_patterns.csv row (main.py's documented "
            f"even-spread fallback applies): {fallbacks if fallbacks else 'none'}")
report(11, "MEETING PATTERN / TIMING SLOT LEGALITY", v, info)

# ── 12. UNPLACED SECTION CONSISTENCY ─────────────────────────────────────────
unpl = [e for e in EV if e["ph"]]
v = []
for e in EV:
    if bool(e.get("unplaced")) != (e["room"] == "UNPLACED"):
        v.append(f"{e['id']} {e['day']}: unplaced flag={e.get('unplaced')} but room="
                 f"'{e['room']}' — the flag and the room disagree")
    if e.get("unplaced") and e["room"] not in ("UNPLACED", ""):
        v.append(f"{e['id']} {e['day']}: placeholder holds a REAL room '{e['room']}'")
for a in unpl:
    if a["room"] == "UNPLACED":
        continue
    for b in PLACED:
        if b["id"] == a["id"]:
            continue
        if a["day"] == b["day"] and a["room"] == b["room"] and overlaps(a, b):
            v.append(f"placeholder {a['id']} @{a['room']} {a['day']} {a['start']}-{a['end']} "
                     f"COLLIDES with genuinely-placed {b['id']}")
info = [f"unplaced sections ({len({e['seckey'] for e in unpl})}): "
        + (", ".join(sorted({f"{e['seckey']} [placeholder time {e['start']}-{e['end']}, "
                            f"invented — not the course's real requirement]" for e in unpl}))
           if unpl else "none")
        + " — an intended, clearly-flagged output for input the scheduler cannot satisfy",
        f"placeholder events holding a real room: "
        f"{len([e for e in unpl if e['room'] != 'UNPLACED'])}"]
report(12, "UNPLACED SECTION CONSISTENCY", v, info)

# ── 13. EVENING SLOT RESTRICTED TO GRADUATE COURSES ──────────────────────────
evening_keys = {k for k, ev in SLOT_EVENING.items() if ev}
v = []
for e in PLACED:
    key = (e["start"], e["e"] - e["s"])
    if key in evening_keys and not is_grad(e["cn"]):
        v.append(f"{e['id']} ({e['cn']}, undergraduate) {e['day']} {e['start']}-{e['end']} is "
                 f"in an evening=TRUE timings.csv slot")
report(13, "EVENING SLOTS CARRY NO UNDERGRADUATE SECTIONS", v,
       [f"evening=TRUE slots in timings.csv: "
        f"{sorted(f'{s} ({d} min)' for s, d in evening_keys) or 'none'}",
        "placeholders are excluded: their times are invented, not chosen"])

# ── 14. PLACEHOLDERS DO NOT CONSUME THE CONCURRENCY CEILING ──────────────────
v = []
if PK_PLACED[0] > MAX_CONCURRENT:
    v.append(f"peak {PK_PLACED[0]} > cap {MAX_CONCURRENT} counting only genuinely-placed "
             f"events at {PK_PLACED[1]} {PK_PLACED[2]//60:02d}:{PK_PLACED[2]%60:02d}")
rooms_held = sorted({e["id"] for e in unpl if e["room"] != "UNPLACED"})
if rooms_held:
    v.append(f"placeholder sections occupy a REAL room and therefore consume the room/"
             f"concurrency budget: {rooms_held}")
report(14, "PLACEHOLDERS DO NOT CONSUME THE CONCURRENCY / ROOM BUDGET", v,
       [f"placed-only peak = {PK_PLACED[0]} (cap {MAX_CONCURRENT}); "
        f"placeholder-inclusive peak = {PK_ALL[0]}"])

# ── summary ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("SUMMARY")
print("=" * 78)
print(f"  run dir: {RUN}")
for n, t, st, c in RESULTS:
    print(f"  {st:4}  check {n:2}  {t}" + (f"  [{c}]" if c else ""))
fails = [r for r in RESULTS if r[2] == "FAIL"]
print(f"  {len(RESULTS)-len(fails)}/{len(RESULTS)} checks PASS")
sys.exit(1 if fails else 0)
