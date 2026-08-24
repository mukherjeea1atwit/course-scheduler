#!/usr/bin/env python3
"""Pre-push checks for the WIT Class Scheduler.

    python3 pre_push.py

Runs everything that should pass before code leaves this machine:

  1. Syntax check on every Python file.
  2. The full test suite (tests/), including the real maths data set if present.
  3. A guard that generated output is not about to be committed.

Exits non-zero if anything fails, so it can be wired to a git hook:

    printf '#!/bin/sh\\nexec python3 pre_push.py\\n' > .git/hooks/pre-push
    chmod +x .git/hooks/pre-push

Nothing here writes to data/ or to the schedule files in the repo — the tests
build throwaway copies of the app in a temp directory.
"""
from __future__ import annotations

import py_compile
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Output the scheduler regenerates on every run. Committing it produces noisy
# diffs and merge conflicts on files nobody edits by hand.
GENERATED = ["schedule.json", "schedule.csv", "schedule_simple.csv", "result.txt"]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def head(title: str) -> None:
    print(f"\n{DIM}{'─' * 60}{RESET}\n{title}\n{DIM}{'─' * 60}{RESET}")


def check_syntax() -> tuple[bool, str]:
    head("1/3  Syntax")
    files = sorted(p for p in REPO.rglob("*.py")
                   if ".git" not in p.parts and "__pycache__" not in p.parts)
    bad = []
    for p in files:
        try:
            py_compile.compile(str(p), doraise=True, cfile=str(p) + "c")
        except py_compile.PyCompileError as e:
            bad.append(f"{p.relative_to(REPO)}: {e.msg.strip().splitlines()[-1]}")
        finally:
            Path(str(p) + "c").unlink(missing_ok=True)
    for b in bad:
        print(f"  {RED}✗{RESET} {b}")
    if not bad:
        print(f"  {GREEN}✓{RESET} {len(files)} file(s) compile")
    return not bad, f"{len(files)} file(s)"


def run_tests() -> tuple[bool, str]:
    head("2/3  Test suite")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        cwd=REPO, capture_output=True, text=True,
    )
    out = proc.stdout + proc.stderr

    ran = ok = skipped = 0
    for line in out.splitlines():
        if " ... ok" in line:
            ran += 1; ok += 1
        elif " ... skipped" in line:
            ran += 1; skipped += 1
        elif " ... FAIL" in line or " ... ERROR" in line:
            ran += 1

    # Only the failures are worth printing; the suite is chatty by design.
    failing = [l for l in out.splitlines() if l.startswith(("FAIL:", "ERROR:"))]
    for f in failing:
        print(f"  {RED}✗{RESET} {f}")
    if failing:
        print(f"\n{DIM}Re-run for detail:{RESET}\n"
              f"  python3 -m unittest discover -s tests -p 'test_*.py'")
    else:
        print(f"  {GREEN}✓{RESET} {ok} passed" + (f", {skipped} skipped" if skipped else ""))
        if skipped:
            print(f"  {DIM}(skips are usually math-test-data/ or fastapi missing){RESET}")
    return proc.returncode == 0, f"{ok} passed, {len(failing)} failed"


def check_generated_not_staged() -> tuple[bool, str]:
    head("3/3  Generated files")
    try:
        # --diff-filter=ACMR excludes deletions: a staged *removal* of a
        # generated file is the fix, not the problem.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=REPO, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"  {YELLOW}·{RESET} not a git repo, or git unavailable — skipped")
        return True, "skipped"

    names = {n.strip() for n in staged.stdout.splitlines() if n.strip()}
    offenders = sorted(names & set(GENERATED))
    for o in offenders:
        print(f"  {RED}✗{RESET} {o} is staged — it is regenerated on every run")
    if offenders:
        print(f"\n{DIM}Un-stage with:{RESET}\n  git rm --cached {' '.join(offenders)}")
    else:
        print(f"  {GREEN}✓{RESET} no generated output staged")
    return not offenders, f"{len(offenders)} offender(s)"


def main() -> int:
    start = time.time()
    print(f"{DIM}Pre-push checks — {REPO}{RESET}")

    results = []
    for fn in (check_syntax, run_tests, check_generated_not_staged):
        passed, summary = fn()
        results.append((fn.__name__, passed, summary))

    head("Summary")
    for name, passed, summary in results:
        mark = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        label = name.replace("_", " ").replace("check ", "").replace("run ", "")
        print(f"  {mark}  {label:<26} {DIM}{summary}{RESET}")

    ok = all(p for _, p, _ in results)
    secs = time.time() - start
    print(f"\n{(GREEN + 'Ready to push') if ok else (RED + 'Do not push')}"
          f"{RESET} {DIM}({secs:.1f}s){RESET}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
