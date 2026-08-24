"""API surface: the endpoints the web UI depends on, and save error handling."""
import csv
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:                                    # pragma: no cover
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/httpx not installed")
class ServerApi(unittest.TestCase):
    """Runs against a COPY of the app so the user's data/ is never written to."""

    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="sched-api-"))
        for name in ("main.py", "server.py", "index.html"):
            shutil.copy2(REPO / name, cls.work / name)
        shutil.copytree(REPO / "data", cls.work / "data")
        shutil.copytree(REPO / "web", cls.work / "web")
        for gen in ("schedule.json", "schedule.csv", "schedule_simple.csv"):
            if (REPO / gen).exists():
                shutil.copy2(REPO / gen, cls.work / gen)

        # server.py resolves paths from its own location, so import it from the copy.
        sys.path.insert(0, str(cls.work))
        for mod in ("server", "main"):
            sys.modules.pop(mod, None)
        import importlib
        cls.server = importlib.import_module("server")
        cls.client = TestClient(cls.server.app)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(cls.work))
        for mod in ("server", "main"):
            sys.modules.pop(mod, None)
        shutil.rmtree(cls.work, ignore_errors=True)

    # ── pages ────────────────────────────────────────────────────────────────
    def test_pages_load(self):
        for path in ("/", "/schedule"):
            self.assertEqual(self.client.get(path).status_code, 200, msg=path)

    # ── data tables ──────────────────────────────────────────────────────────
    def test_every_editor_tab_has_a_backing_file(self):
        """The Inputs page builds a tab per slug; a missing file is a 500 the
        user sees as a blank table."""
        for slug in self.server.FILE_MAP:
            r = self.client.get(f"/api/data/{slug}")
            self.assertEqual(r.status_code, 200, msg=f"{slug}: {r.text[:200]}")
            self.assertIsInstance(r.json(), list)

    def test_settings_and_meeting_patterns_are_editable(self):
        for slug in ("settings", "meeting_patterns"):
            self.assertIn(slug, self.server.FILE_MAP)

    def test_unknown_slug_is_404(self):
        self.assertEqual(self.client.get("/api/data/nope").status_code, 404)

    def test_save_round_trips_and_reports_the_row_count(self):
        rows = self.client.get("/api/data/settings").json()
        r = self.client.put("/api/data/settings", json=rows)
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["rows"], len(rows))
        self.assertEqual(self.client.get("/api/data/settings").json(), rows)

    def test_empty_table_is_rejected(self):
        r = self.client.put("/api/data/settings", json=[])
        self.assertEqual(r.status_code, 422)

    def test_unwritable_folder_produces_a_readable_error(self):
        """A save that fails must say so. Silent failure is the whole reason this
        code path exists — the user edits a table, sees nothing, and the
        scheduler keeps reading the old file."""
        data_dir = self.work / "data"
        original = stat.S_IMODE(os.stat(data_dir).st_mode)
        rows = self.client.get("/api/data/settings").json()
        os.chmod(data_dir, 0o500)
        try:
            r = self.client.put("/api/data/settings", json=rows)
            self.assertEqual(r.status_code, 500, msg=r.text)
            detail = r.json()["detail"]
            # The message must name the file and say something the user can act on.
            self.assertIn("settings.csv", detail)
            self.assertIn("writable", detail.lower())
        finally:
            os.chmod(data_dir, original)

    # ── schedule + CSV download ──────────────────────────────────────────────
    def test_schedule_json_carries_the_unplaced_flag(self):
        events = self.client.get("/api/schedule").json()
        self.assertTrue(events)
        self.assertIn("unplaced", events[0])

    def test_csv_download_defaults_to_the_simple_format(self):
        r = self.client.get("/api/schedule/csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        header = r.text.splitlines()[0]
        self.assertTrue(header.startswith("Course Designation/Number,Type,Days,Times,Faculty"))

    def test_banner_format_is_available(self):
        r = self.client.get("/api/schedule/csv", params={"format": "banner"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.text.splitlines()[0].startswith("CRN,Subj,Crse,Section"))

    def test_unknown_format_is_rejected_by_name(self):
        r = self.client.get("/api/schedule/csv", params={"format": "bogus"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("bogus", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
