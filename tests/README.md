# Tests

    python3 pre_push.py                                    # everything, before pushing
    python3 -m unittest discover -s tests -p 'test_*.py'   # just the suite
    python3 -m unittest tests.test_scheduling -v           # one file

Stdlib `unittest` only — no install needed. `pytest` also runs them if you prefer.

## What is here

| File | Covers |
|---|---|
| `test_units.py` | Pure helpers: day patterns, meeting-length rules, section-id parsing, settings loading |
| `test_scheduling.py` | End-to-end behaviour: days/week, meeting length, grad rules, faculty roster, unplaced sections, concurrency |
| `test_inputs_and_exports.py` | The INPUT CHECK report and both CSV exports |
| `test_server.py` | API endpoints, save round-trip, save failure messages |
| `test_shipped.py` | The data in `data/` schedules cleanly; static checks on the UI |
| `test_math_dataset.py` | The maths department's real files — skipped if `math-test-data/` is absent |

## How the end-to-end tests work

`main.py` resolves every input path relative to its own location, so a test
cannot point it at a different data set in place. Each end-to-end test copies
`main.py` into a temp directory, writes the CSVs it wants, and runs it there as
a subprocess.

This means **the tests never touch `data/` or overwrite the schedule you are
looking at** — a hard requirement, since they run before a push.

## Writing a new test

Most tests only need `helpers.run_scheduler`:

```python
run = self.run_scheduler(
    courses=course_list(["MATH1500,Precalculus,3,4,0,4,"]),
    prefs=preferences(['MATH1500,Precalculus,"Ann, Bob"']),
    loads=faculty_load(["Ann,3,", "Bob,3,"]),
    overrides={"timings.csv": "..."},      # optional
)
self.assertAllConstraintsPass(run)
```

Two conventions worth keeping:

- **Use `assertRanCleanly` / `assertAllConstraintsPass`.** They check the exit
  code and for a traceback. A real regression was once missed by grepping output
  for `✗ FAIL`: a run that dies with a `NameError` produces zero FAIL lines and
  reads as a perfect pass.
- **Assert on the schedule, not only on the checker's verdict.** For anything the
  scheduler is supposed to *avoid*, read `run.rows` / `run.events` and check it
  directly — otherwise the test passes as long as the checker notices, which is
  not the same as the scheduler getting it right.

Fixtures are deliberately tiny, so C16 (day balance vs the term average) is
meaningless on them; those tests pass `allow=("C16",)`.
