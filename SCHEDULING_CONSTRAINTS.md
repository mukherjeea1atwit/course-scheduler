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

| # | Constraint | What it means | Where enforced |
|---|---|---|---|
| C1 | **Faculty load ≤ their configured max** | A professor is never assigned more course sections than their `CS Course Load` value in `faculty_load.csv` (default 3 if unlisted). *(The other half of C1 — reaching the target, not just staying under it — is a soft goal; see §2.)* | `can_assign()` (~L670); checked by `_c1_load` (~L1093) |
| C2 | **Faculty daily teaching span ≤ 9 hours** | On any single day, the gap between a professor's earliest class start and latest class end can't exceed 9 hours. | `_would_exceed_span()` (~L576); checked by `_c2_daily` (~L1111) |
| C3 | **Faculty teaches ≤ 2 sections of the same course** | A professor can't be assigned 3+ sections of the identical course number. | `can_assign()` (~L670); checked by `_c3_duplicates` (~L1131) |
| C4 | **Faculty teaches on ≤ 4 distinct days/week** | Adding a class can't push a professor's teaching days above 4 (M/T/W/Th/F). | `viable_patterns()` (~L722), `_lab_day_candidates()` (~L747); checked by `_c4_days` (~L1144) |
| C5 | **Every section has a faculty value** | No section is ever left with a blank faculty field — worst case it gets `"TBA"`. | Structural (fallback chain always assigns something, ~L913-953); checked by `_c5_assigned` (~L1155) |
| C7 | **Lecture/lab durations fall in valid ranges** | Labs run ~105 min (100–110 accepted); grad lectures ~155 min (145–165); undergrad lectures ~80 or 90 min (75–95). | `lecture_lab_minutes()` (~L177) drives slot search; checked by `_c7_durations` (~L1163) |
| C9 | **Lab meets on a different day than its lecture** | A course's lab session is never scheduled on a day the lecture also meets. | `_lab_day_candidates()` only offers non-lecture days (~L747); checked by `_c9_lab_day` (~L1183) |
| C10 | **Lab occupies exactly one day** | A lab is a single weekly meeting, never split across multiple days. | Structural (labs are built as one `ScheduledSection` with one day, ~L1014-1025); checked by `_c10_lab_one_day` (~L1196) |
| C11 | **≤ 10 concurrent sections in any time window** | At most 10 classes can be running at once (overlapping start/end), per day, across the whole schedule. | `_slot_capacity_ok()` (~L554); checked by `_c11_concurrency` (~L1204) |
| C12 | **Graduate (5000+) courses start at 18:00 (6 PM)** | Every grad-level section's lecture begins at 6 PM. | `_eligible_slots()` restricts grad courses to `18:00 ≤ start < 19:00` (~L528); checked by `_c12_grad_time` (~L1222) |
| C13 | **Lab taught by the same faculty as its lecture** | A course's lab section is never assigned to a different professor than its lecture. | Structural (`fac` reused when booking the lab, ~L1008); checked by `_c13_lab_faculty` (~L1231) |
| C14 | **≤ 2 sections of the same course at the exact same day/time** | No more than 2 sections of one course number can share an identical (days, start, end). | Not actively filtered during search; checked post-hoc by `_c14_time_dupes` (~L1241) |
| C15 | **Lecture day patterns limited to MW / TTh / WF** | A 2-day lecture only ever meets Mon+Wed, Tue+Thu, or Wed+Fri — no other 2-day combination. | `LECTURE_PATTERNS` is the only pool offered (~L37, `_patterns_for` ~L712); checked by `_c15_patterns` (~L1255) |
| C17 | **Lab starts at the same clock time as its lecture** | If a course has a lab, the lab's start time (hour:minute) must match the lecture's start time — only the day differs. | `_find_lab_at()` only searches slots with matching `start` (~L757-792); checked by `_c17_lab_same_start` (~L1277) |
| C18 | **≤ 2 graduate sections per faculty member** | Across *all* 5000+ courses combined, one professor teaches at most 2 grad sections. | `can_assign()` (~L682-689); checked by `_c18_grad_per_faculty` (~L1292) |
| C19 | **Faculty preference is mandatory** | A section can only go to a professor explicitly listed for that course in `prof_preferences.csv` — never to an unlisted professor (falls to `TBA` instead). One narrow, deliberate exception: the load top-up mechanism (see §2) may assign an unlisted professor to a leftover **TBA** section of a foundational course (`COMP1000`/`COMP1050`/`COMP2000`) to help them reach their target load; those sections are flagged `topup` and excused from this check. | `faculty_candidates()` only returns preferred profs (~L692); checked by `_c19_pref_honored` (~L1304) |
| — | **Faculty gap ≥ 15 minutes between back-to-back classes** | Two classes for the same professor must be separated by at least `FACULTY_GAP_MIN` (15 min), not just non-overlapping. | `times_conflict()` (~L199), used by `_faculty_free()` (~L535) — no separate post-hoc check |
| — | **No double-booked room** | A room already booked for an overlapping day/time is never assigned to a second section. | `RoomAssigner.is_free()` (~L388) — no separate post-hoc check |
| — | **No double-booked faculty** | A professor already booked for an overlapping day/time is never assigned a second section then. | `TimeSlotScheduler._faculty_free()` (~L535) — no separate post-hoc check |
| — | **Tue/Thu 12:00–13:00 is reserved** | No class (any course, any faculty) may be scheduled on Tuesday or Thursday if it overlaps the 12:00 PM–1:00 PM common hour. | `overlaps_reserved()` (~L191), applied to both lecture and lab placement |
| — | **Only pre-defined time slots are ever used** | Every class start/end time must come from a row in `timings.csv` (including that slot's own `days_allowed` restriction) — the scheduler never invents an ad-hoc time. | `TimeSlotScheduler` is seeded entirely from `timings.csv` (~L466) |
| — | **Grad courses never meet Friday evening** | Single-day grad evening options are Monday, Tuesday, Wednesday, or Thursday only — Friday is intentionally excluded (no evening slots exist that day). | `GRAD_SINGLE_DAY_PATTERNS` (~L39) omits `"F"` |

---

## 2. Soft constraints / preferences

These shape the search order or are pursued opportunistically, but a violation
never blocks a schedule from being generated — at worst it's logged as a
`⚠` warning or a `✗` in a best-effort report that doesn't stop the run.

| Constraint | What it means | Where implemented |
|---|---|---|
| **Faculty load target (the "reach it," not just "don't exceed it," half of C1)** | The scheduler tries to get every professor *up to* their configured target load, not just avoid exceeding it. Under-target is logged as a warning only. | Candidate ordering by `fill_ratio()` in `faculty_candidates()` (~L706) tries the most-underloaded preferred professor first; `_topup_underloaded()` (~L850) does a final pass reassigning leftover TBA foundational-course sections to whoever is still under target |
| **C16 — Sections balanced across weekdays (≤ 40% deviation from average)** | No single weekday should end up with dramatically more or fewer classes than the others. Checked and reported, but scheduling never refuses a slot solely because it would trip this. | Day-pattern ordering favors the least-loaded days (`viable_patterns()` ~L722, `_lab_day_candidates()` ~L747); reported by `_c16_balance` (~L1264) |
| **AM/PM balance for undergrad courses (~60% AM target)** | Roughly 60% of undergraduate class meetings should land before noon; once that quota is hit, later placements are steered toward the afternoon. | `AM_TARGET_RATIO` (~L46); `force_pm` flag computed per-section (~L915) and threaded into slot search |
| **Room preferences (`room_preferences.csv`)** | Each course/room-type combination has a ranked list of preferred rooms, tried in rank order before falling back to any capacity-appropriate free room, and finally (if nothing is free) to the smallest available room with a logged `[ROOM-OVERBOOK]` warning — a room assignment is always made. | `RoomAssigner.find_room()` (~L402) |
| **Non-overlap groups (`non_overlap_groups.csv`)** | For each named group of courses (e.g., all Year-2-Fall Cybersecurity courses), the scheduler tries to keep at least one non-conflicting section pairing per pair of courses in the group, so a student following that curriculum can register for all of them without a time clash. This only *biases* which slot is tried first — it never removes a slot from consideration, and an unsatisfiable group just prints a `✗ FAIL` line without affecting the rest of the run. | `_group_bias()` / `_record_group_rep()` inside `build_schedule()` (~L623-648); reported by `check_non_overlap_groups()` (~L1319) — see also the note on silently-skipped courses below |
| **Preference CSV rank order (secondary to load balancing)** | Within a course's list of preferred professors, the CSV's own ordering is only a tie-breaker — the primary sort is "most underloaded professor first" (`fill_ratio`). | `faculty_candidates()` (~L692) |
| **Scheduling order by course level** | Upper-level undergrad courses (3000–4999) are placed first, then graduate (5000+), then lower-level undergrad — a heuristic to give the most schedule-constrained courses first pick of slots, not a formal rule. | `schedule_priority()` (~L163), used to sort the section queue (~L663) |
| **Foundational-course load top-up (deliberate, narrow exception to C19)** | If a professor is still under their target load after the main pass, the scheduler may reassign an already-placed **TBA** section of `COMP1000`, `COMP1050`, or `COMP2000` (courses anyone can teach) to that professor, even though they weren't on that course's preference list. | `FOUNDATION_COURSES` (~L51), `_topup_underloaded()` (~L850) |
| **Non-overlap group members that don't resolve to a scheduled course are silently dropped** | If a course number in `non_overlap_groups.csv` doesn't match the course list (typo, renamed course code, etc.) or matches a course with 0 sections, that entry simply doesn't constrain anything — no error is raised. `check_non_overlap_groups()` now prints an explicit `⚠ SKIP` line distinguishing "not in the course list" (likely a typo/renamed code) from "0 sections" (real course, just not offered), so this is now visible in the run log instead of silent. | `check_non_overlap_groups()` (~L1319) |

---

## 3. Data files that shape these constraints

None of the above are hardcoded beyond the rules themselves — the specific numbers
come from these CSVs in `data/`, editable via the Input Editor UI:

- **`course-list-Spring 27(Sheet1) (1).csv`** — course numbers, names, lecture/lab hours, section counts, preferred room.
- **`prof_preferences.csv`** — the hard-constraint list of eligible professors per course (C19).
- **`faculty_load.csv`** — each professor's target course load (C1).
- **`timings.csv`** — the fixed menu of time slots (start, stop, duration, evening flag, allowed days) the scheduler can ever choose from.
- **`rooms.csv`** — room names, types, capacities.
- **`room_preferences.csv`** — ranked preferred rooms per course/type.
- **`non_overlap_groups.csv`** — curriculum groups of courses that shouldn't overlap for students in the same program/term.
