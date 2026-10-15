# Scheduling Constraints Reference

This document lists every constraint the WIT Class Scheduler (`main.py`) uses when
building a schedule — both the **hard constraints** it actively refuses to violate
while assigning classes, and the **soft constraints/preferences** it tries to
satisfy on a best-effort basis without ever blocking a schedule from being produced.

Line references point at `main.py` as of this writing so they can be re-located if
the code moves.

---

## 1. Hard constraints

These are enforced *during* assignment (the scheduler will only place a class into
a slot/room/faculty combination that satisfies them) and/or checked afterward by
`ConstraintChecker`, where a violation prints `✗ FAIL` in the run log. A few are
enforced structurally (there's simply no code path that could produce a violation)
and don't have their own numbered check.

**Who counts as graduate.** Every rule below that says "graduate" or "grad" means a
course number of **5000 or above, except 5500**. 5500 is the senior project / senior
design course in every subject (COMP5500, CYBR5500, DATA5500, AAIN5500, ...) and is
always taken by undergraduates, so it follows the undergraduate rules: daytime slots,
its lab(s) if it has any, the `meeting_patterns.csv` lengths, AM/PM balance, and it does
not count toward C18. (COMP5500 itself is the "1-6-4" multi-lab course described in
§1a.) It is scheduled in the upper-level undergraduate tier (with
3000/4000). Defined once per engine as `GRAD_MIN_NUMBER` / `UNDERGRAD_EXCEPTIONS` /
`is_grad()` in `main.py` and `main_cpsat.py`, mirrored in `tests/validate_schedule.py`.

| # | Constraint | What it means | Where enforced |
|---|---|---|---|
| C1 | **Faculty load ≤ their configured max** | A professor is never assigned more course sections than their `CS Course Load` value in `faculty_load.csv` (default 3 if unlisted). *(The other half of C1 — reaching the target, not just staying under it — is a soft goal; see §2.)* | `can_assign()` (~L670); checked by `_c1_load` (~L1093) |
| C2 | **Faculty daily teaching span ≤ 9 hours** | On any single day, the gap between a professor's earliest class start and latest class end can't exceed 9 hours. | `_would_exceed_span()` (~L576); checked by `_c2_daily` (~L1111) |
| C3 | **Faculty teaches ≤ 2 sections of the same course** | A professor can't be assigned 3+ sections of the identical course number. | `can_assign()` (~L670); checked by `_c3_duplicates` (~L1131) |
| C4 | **Faculty teaches on ≤ 4 distinct days/week** | Adding a class can't push a professor's teaching days above 4 (M/T/W/Th/F). Lab days count. A single section that itself needs more days (lecture days + lab meetings) raises the allowance to that number — `teaching_day_allowance()`, applied to a section's lecture and lab days together by the lab search, the top-up, CP-SAT and the checker. | `viable_patterns()` (~L722), `_lab_day_candidates()` (~L747); checked by `_c4_days` (~L1144) |
| C5 | **Every section has a faculty value** | No section is ever left with a blank faculty field — worst case it gets `"TBA"`. | Structural (fallback chain always assigns something, ~L913-953); checked by `_c5_assigned` (~L1155) |
| C7 | **Lecture/lab durations fall in valid ranges** | Every meeting must match a `timings.csv` duration. A single weekly lab runs ~105 min (100–110 accepted); the labs of a multi-lab course, and a single lab longer than 105 min, must be exactly their `meeting_patterns.csv` `lab_minutes` (120 for COMP5500) — `lab_meeting_min()`; grad lectures ~155 min (145–165). | `lecture_lab_minutes()` (~L177) drives slot search; checked by `_c7_durations` (~L1163) |
| C9 | **Lab meets on a different day than its lecture** | No lab meeting of a section is ever on a day its lecture also meets. | `_lab_day_candidates()` only offers non-lecture days (~L747); checked by `_c9_lab_day` (~L1183) |
| C10 | **Lab meets its declared number of distinct days** | One day for an ordinary lab; `lab_meetings_per_week` distinct days for a multi-lab course (3 for COMP5500). Two lab meetings on the same day, or too few/many, fail. | Lab day-sets come from `lab_day_patterns()` (distinct weekdays by construction) in both engines; checked by `_c10_lab_one_day` and by check 16 of `tests/validate_schedule.py` |
| C11 | **≤ 10 concurrent sections in any time window** | At most 10 classes can be running at once (overlapping start/end), per day, across the whole schedule. Every lab meeting counts on its own day. | `_slot_capacity_ok()` (~L554); checked by `_c11_concurrency` (~L1204) |
| C12 | **Graduate (5000+, except 5500) courses start at 18:00 (6 PM)** | Every grad-level section's lecture begins at 6 PM. | `_eligible_slots()` restricts grad courses to `18:00 ≤ start < 19:00` (~L528); checked by `_c12_grad_time` (~L1222) |
| C13 | **Lab taught by the same faculty as its lecture** | A course's lab section is never assigned to a different professor than its lecture. | Structural (`fac` reused when booking the lab, ~L1008); checked by `_c13_lab_faculty` (~L1231) |
| C14 | **≤ `max_same_time_sections` sections of the same course at the exact same day/time** (default 2) | No more sections of one course number than the cap can share an identical (days, start, end). The cap is set in `settings.csv` as either a whole number (`2`) or a percentage of that course's "number of sections" (`25%`): 25% of 10 sections = 2.5, rounded half up to 3. The cap is never below 1 (`0` and `0%` mean 1); an invalid value (`abc`, `-5`, `150%`) prints a `[WARN]` and uses 2. A course's labs share its cap. | `same_time_cap()` resolves the cap per course. Greedy: `course_time_ok()` in `build_schedule` rejects a slot that would exceed it. CP-SAT: one `sum(lits) <= cap` per (course, days, start, end). Checked by `_c14_time_dupes` in both engines and by check 15 of `tests/validate_schedule.py` |
| C15 | **Lecture day patterns limited to MW / TTh / WF** | A 2-day lecture only ever meets Mon+Wed, Tue+Thu, or Wed+Fri — no other 2-day combination. | `LECTURE_PATTERNS` is the only pool offered (~L37, `_patterns_for` ~L712); checked by `_c15_patterns` (~L1255) |
| C17 | **A single weekly lab starts at the same clock time as its lecture** | If a course has one lab a week, the lab's start time (hour:minute) must match the lecture's start time — only the day differs. Multi-lab courses (§1a) are exempt. | `_find_lab_at()` only searches slots with matching `start` for a single lab; CP-SAT ties the lab start to the lecture start only when `n_labs == 1`; checked by `_c17_lab_same_start`, which skips multi-lab blocks |
| C18 | **≤ 2 graduate sections per faculty member** | Across *all* graduate courses combined (5000+, not 5500), one professor teaches at most 2 grad sections. | `can_assign()` (~L682-689); checked by `_c18_grad_per_faculty` (~L1292) |
| C19 | **Faculty preference is mandatory** | A section can only go to a professor explicitly listed for that course in `prof_preferences.csv` — never to an unlisted professor (falls to `TBA` instead). One narrow, deliberate exception: the load top-up mechanism (see §2) may assign an unlisted professor to a leftover **TBA** section of a foundational course (`COMP1000`/`COMP1050`/`COMP2000`) to help them reach their target load; those sections are flagged `topup` and excused from this check. | `faculty_candidates()` only returns preferred profs (~L692); checked by `_c19_pref_honored` (~L1304) |
| — | **Faculty gap ≥ 15 minutes between back-to-back classes** | Two classes for the same professor must be separated by at least `FACULTY_GAP_MIN` (15 min), not just non-overlapping. | `times_conflict()` (~L199), used by `_faculty_free()` (~L535) — no separate post-hoc check |
| — | **No double-booked room** | A room already booked for an overlapping day/time is never assigned to a second section. | `RoomAssigner.is_free()` (~L388) — no separate post-hoc check |
| — | **No double-booked faculty** | A professor already booked for an overlapping day/time is never assigned a second section then. | `TimeSlotScheduler._faculty_free()` (~L535) — no separate post-hoc check |
| — | **Tue/Thu 12:00–13:00 is reserved** | No class (any course, any faculty) may be scheduled on Tuesday or Thursday if it overlaps the 12:00 PM–1:00 PM common hour. | `overlaps_reserved()` (~L191), applied to both lecture and lab placement |
| — | **Only pre-defined time slots are ever used** | Every class start/end time must come from a row in `timings.csv` (including that slot's own `days_allowed` restriction) — the scheduler never invents an ad-hoc time. | `TimeSlotScheduler` is seeded entirely from `timings.csv` (~L466) |
| — | **Grad courses never meet Friday evening** | Single-day grad evening options are Monday, Tuesday, Wednesday, or Thursday only — Friday is intentionally excluded (no evening slots exist that day). | `GRAD_SINGLE_DAY_PATTERNS` (~L39) omits `"F"` |

### 1a. Courses with more than one lab meeting a week (the "1-6-4" senior project)

COMP5500 Senior Project is one 60-minute lecture plus three 120-minute labs a week,
per section. It is expressed entirely as data, so any future course can do the same:

- `course-list.csv`: `COMP5500,Senior Project,1,1,6,6,` — 1 lecture day, 1 lecture hour,
  6 lab hours, 6 sections.
- `meeting_patterns.csv`: `*,1,1,60,120,3,...` — the optional `lab_meetings_per_week`
  column (blank or absent = 1) gives the lab count; `meeting_minutes` and `lab_minutes`
  give the lengths.
- `timings.csv`: `lec_60` and `lab_120` rows at every daytime start (08:00, 10:00, 13:00,
  15:00, 17:15), Monday–Friday except 17:15, which is Monday–Thursday (no Friday evening classes; true of every 17:15 slot). No existing course uses those lengths, so other courses
  are unaffected.

Rules (working assumptions; the client has not confirmed them — each is one place to
change):

| Assumption | Where it lives |
|---|---|
| Each lab meeting is on its own weekday, none on a lecture day (lecture day + 3 lab days = 4 distinct days). | `lab_day_patterns()` in both engines; C9/C10; validator check 16 |
| All lab meetings of one section share ONE time (e.g. labs M/W/F 13:00–15:00), stored as one `-LAB` entry with several days, exactly like a multi-day lecture. | Section id stays `COMP5500-1-LAB`, so every export, the UI and the validator work unchanged |
| C17 does not apply: the lab block may start at a different time from the lecture (greedy tries the lecture's start first). | `_find_lab_at()` / CP-SAT `lab_start`; `_c17_lab_same_start` |
| The same professor teaches the lecture and every lab (C13); the section counts as 1 course toward load (C1). | Structural, as for single labs |
| Labs use ordinary rooms via `room_preferences.csv` (Type `Lab`), no special lab room. | `RoomAssigner.find_room(..., is_lab=True)` |
| A lab block counts once per lab meeting toward the AM/PM 60 % target and weekday balance (a single lab still counts 1). | `total_ug` / `am_used` in `main.py`; `ug_meetings` / `am_expr` in `main_cpsat.py` |
| C14 caps identical lab blocks of one course (same day-set and time) at `max_same_time_sections`, as for lectures. | `course_time_ok()` / CP-SAT C14 key `(course, sorted days, start, end)` |

If the lecture days plus lab meetings exceed five weekdays the section is reported
UNPLACED with that reason rather than invented.

---

## 2. Soft constraints / preferences

These shape the search order or are pursued opportunistically, but a violation
never blocks a schedule from being generated — at worst it's logged as a
`⚠` warning or a `✗` in a best-effort report that doesn't stop the run.

| Constraint | What it means | Where implemented |
|---|---|---|
| **Faculty load target (the "reach it," not just "don't exceed it," half of C1)** | The scheduler tries to get every professor *up to* their configured target load, not just avoid exceeding it. Under-target is logged as a warning only. | Candidate ordering by `fill_ratio()` in `faculty_candidates()` (~L706) tries the most-underloaded preferred professor first; `_topup_underloaded()` (~L850) does a final pass reassigning leftover TBA foundational-course sections to whoever is still under target. The top-up only ever touches sections that are really on the timetable: an UNPLACED placeholder (or a lecture whose lab is one) stays TBA, in both engines. Greedy also protects sole teachers: while a professor is the only qualified name for a course with sections still to be placed, they are tried after the other qualified professors for any other course (and last of all when taking it would leave too little load for those sections) — `would_strand_sole_duty()` / `has_sole_duty_pending()`; ordering only, nobody is excluded |
| **C16 — Sections balanced across weekdays (≤ 40% deviation from average)** | No single weekday should end up with dramatically more or fewer classes than the others. Checked and reported, but scheduling never refuses a slot solely because it would trip this. | Day-pattern ordering favors the least-loaded days (`viable_patterns()` ~L722, `_lab_day_candidates()` ~L747); reported by `_c16_balance` (~L1264) |
| **AM/PM balance for undergrad courses (~60% AM target)** | Roughly 60% of undergraduate class meetings should land before noon; once that quota is hit, later placements are steered toward the afternoon. | `AM_TARGET_RATIO` (~L46); `force_pm` flag computed per-section (~L1002) and threaded into slot search — including the TBA fallback (~L1026) and the forced-assignment paths (~L1035, ~L1090), which previously bypassed it |
| **Per-faculty AM/PM preference (`faculty_load.csv` → `Time Preference`)** | A professor may declare `AM` or `PM` (blank = no preference) and the scheduler tries that half of the day first for their sections. Purely an ordering bias: it never overrides a hard constraint, and it is skipped entirely for `TBA` sections (no professor identity), for graduate courses (evening-only by C12), and for any section the global 60% AM quota has already forced into the afternoon — in that case the quota wins and the preference simply goes unsatisfied for that section. | `load_faculty_time_prefs()` (~L308); `_faculty_time_bias()` (~L848), applied as a sort key in `_try_assign()` (~L906) |
| **Intra-AM and intra-PM early/late balance (~60/40 target)** | Beyond splitting AM vs PM, meetings are also spread *within* each window so they don't all pile onto the earliest slot (8:00, or 13:00). The distinct undergrad start hours offered by `timings.csv` are read at run time and each window's sorted hours are split in half (rounded down, minimum one) into "early" and "late" — with the current file that is early = {08:00} / late = {10:00} for AM, and early = {13:00} / late = {15:00, 17:15} for PM, so it stays correct if the slot menu changes. A running counter per window steers the next placement toward whichever half is currently below the 60% early target. Unlike the AM/PM quota this is *only* an ordering bias, never a filter — once a section's window is fixed there is no guaranteed fallback, so it must not be able to make a section unplaceable. Undergrad only; grad sections are excluded. | `WINDOW_EARLY_RATIO` (~L50); early/late hours derived at ~L694; `_window_balance_bias()` (~L834), applied in `_try_assign()` (~L905), in the TBA fallback (~L1026) and in `_order_fallback_slots()` (~L861) for the forced-assignment paths |
| **Room preferences (`room_preferences.csv`)** | Each course/room-type combination has a ranked list of preferred rooms, tried in rank order before falling back to any capacity-appropriate free room, and finally (if nothing is free) to the smallest available room with a logged `[ROOM-OVERBOOK]` warning — a room assignment is always made. | `RoomAssigner.find_room()` (~L402) |
| **Non-overlap groups (`non_overlap_groups.csv`)** | For each named group of courses (e.g., all Year-1-Fall AI courses: `COMP1000` + `AAIN1000`), the scheduler does **not** require every section of the shared/gateway course to dodge every section of the cohort-specific course — only enough of the shared course's sections to give *each section* of the cohort-specific course a non-conflicting option, since that's the number of students actually affected. Which course is "the shared one" is decided by how many different groups it belongs to (a structural fact — a gateway course like `COMP1000` serves several cohorts and sits in several groups; a cohort-specific course like `AAIN1000` sits in just one), not by section count, which can vary run to run for reasons unrelated to which course is actually shared. A course in several groups at once (e.g. `COMP1000` also pairs with `COMP1100` for Cyber and `COMP1010` for IT) shares one pool of reservations across all of them, so two different pairings don't quietly reserve the *same* physical section and stack both cohorts into it. This only *biases* which slot is tried first — it never removes a slot from consideration, and an unsatisfiable pairing just prints a `✗ FAIL` line without affecting the rest of the run. The final report also flags (informationally) when the same section ends up as the *sole* non-conflicting option for two or more different pairings — a possible seat-overflow risk the tool can't confirm on its own, since it has no enrollment data, only timing. | `_group_target()`, `_group_bias()` / `_record_group_rep()` inside `build_schedule()` (~L634-712); reported by `check_non_overlap_groups()` (~L1469), which derives group membership counts locally to pick the cohort-specific side, including the overflow-risk note — see also the note on silently-skipped courses below |
| **Preference CSV rank order (secondary to load balancing)** | Within a course's list of preferred professors, the CSV's own ordering is only a tie-breaker — the primary sort is "most underloaded professor first" (`fill_ratio`). | `faculty_candidates()` (~L692) |
| **Scheduling order by course level** | Upper-level undergrad courses (3000–4999, plus 5500 senior project) are placed first, then graduate (5000+ except 5500), then lower-level undergrad — a heuristic to give the most schedule-constrained courses first pick of slots, not a formal rule. | `schedule_priority()` (~L163), used to sort the section queue (~L663) |
| **Foundational-course load top-up (deliberate, narrow exception to C19)** | If a professor is still under their target load after the main pass, the scheduler may reassign an already-placed **TBA** section of `COMP1000`, `COMP1050`, or `COMP2000` (courses anyone can teach) to that professor, even though they weren't on that course's preference list. | `FOUNDATION_COURSES` (~L51), `_topup_underloaded()` (~L850) |
| **Non-overlap group members that don't resolve to a scheduled course are silently dropped** | If a course number in `non_overlap_groups.csv` doesn't match the course list (typo, renamed course code, etc.) or matches a course with 0 sections, that entry simply doesn't constrain anything — no error is raised. `check_non_overlap_groups()` now prints an explicit `⚠ SKIP` line distinguishing "not in the course list" (likely a typo/renamed code) from "0 sections" (real course, just not offered), so this is now visible in the run log instead of silent. | `check_non_overlap_groups()` (~L1319) |

---

## 3. Data files that shape these constraints

None of the above are hardcoded beyond the rules themselves — the specific numbers
come from these CSVs in `data/`, editable via the Input Editor UI:

- **`course-list.csv`** — course numbers, names, lecture/lab hours, section counts, preferred room.
- **`prof_preferences.csv`** — the hard-constraint list of eligible professors per course (C19).
- **`faculty_load.csv`** — each professor's target course load (C1), plus an optional `Time Preference` column (`AM`, `PM`, or blank for no preference) used as a soft bias on which half of the day their sections are placed in.
- **`timings.csv`** — the fixed menu of time slots (start, stop, duration, evening flag, allowed days) the scheduler can ever choose from.
- **`meeting_patterns.csv`** — meeting length per (subject, days/week, lecture hours), lab length, and the optional `lab_meetings_per_week` (blank = 1; see §1a).
- **`rooms.csv`** — room names, types, capacities.
- **`room_preferences.csv`** — ranked preferred rooms per course/type.
- **`non_overlap_groups.csv`** — curriculum groups of courses that shouldn't overlap for students in the same program/term.
