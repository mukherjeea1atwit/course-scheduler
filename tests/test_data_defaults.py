"""data-defaults/ must stay in step with data/.

data-defaults/ is the read-only copy the server restores from when a file in
data/ is missing or has been overwritten with another file's contents (see
_seed_missing_data_files and _repair_wrong_table in server.py). data/ is the
working copy the app writes to, so it stops being pristine the moment a user
saves anything.

That split only works while the two agree. If someone adds a time slot to
data/timings.csv and forgets data-defaults/, then a customer whose file needs
repairing is silently restored to a *stale* version — which is the same class of
bug that stranded every 3- and 4-day course on the customer's install when his
timings.csv predated the lec_70 rows. This test makes that drift a failing build
instead of a support ticket.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DEFAULTS = REPO / "data-defaults"


class DataDefaultsMirrorData(unittest.TestCase):
    def test_defaults_directory_exists(self):
        self.assertTrue(
            DEFAULTS.is_dir(),
            "data-defaults/ is missing — the server's repair path has nothing to "
            "restore from and will silently do nothing.",
        )

    def test_every_data_file_has_a_default(self):
        missing = sorted(
            p.name for p in DATA.glob("*.csv")
            if not p.name.endswith((".old1.csv", ".bak"))
            and ".old" not in p.name
            and not (DEFAULTS / p.name).exists()
        )
        self.assertEqual(
            [], missing,
            f"these files exist in data/ but not data-defaults/: {missing}. "
            f"Copy them across so a broken install can be repaired.",
        )

    def test_no_orphan_defaults(self):
        orphans = sorted(
            p.name for p in DEFAULTS.glob("*.csv") if not (DATA / p.name).exists()
        )
        self.assertEqual(
            [], orphans,
            f"these files exist in data-defaults/ but not data/: {orphans}. "
            f"Either they were renamed in data/ and not here, or they are dead.",
        )

    def test_contents_match(self):
        drifted = []
        for default in sorted(DEFAULTS.glob("*.csv")):
            live = DATA / default.name
            if not live.exists():
                continue          # reported by test_no_orphan_defaults
            # Compare parsed lines rather than raw bytes so a CRLF/LF difference
            # from a Windows round-trip is not reported as drift.
            a = default.read_text(encoding="utf-8-sig").splitlines()
            b = live.read_text(encoding="utf-8-sig").splitlines()
            if a != b:
                drifted.append(default.name)
        self.assertEqual(
            [], drifted,
            f"data/ and data-defaults/ have diverged for: {drifted}. Whichever you "
            f"edited, copy it to the other — a customer repaired from a stale "
            f"default gets a schedule full of UNPLACED sections.",
        )


if __name__ == "__main__":
    unittest.main()
