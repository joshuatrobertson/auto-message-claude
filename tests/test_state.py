import json
import unittest

import autoresume
from tests.helpers import TempStateMixin


class TestLockedState(TempStateMixin, unittest.TestCase):
    def test_roundtrip(self):
        with autoresume.locked_state() as entries:
            entries["abc"] = {"attempts": 1}
        with autoresume.locked_state() as entries:
            self.assertEqual(entries["abc"]["attempts"], 1)

    def test_missing_file_is_empty_dict(self):
        with autoresume.locked_state() as entries:
            self.assertEqual(entries, {})

    def test_corrupt_file_is_quarantined_not_fatal(self):
        (self.state / "state.json").write_text("{nope")
        with autoresume.locked_state() as entries:
            self.assertEqual(entries, {})
        quarantined = list(self.state.glob("state.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(), "{nope")

    def test_write_is_atomic_no_tmp_left_behind(self):
        with autoresume.locked_state() as entries:
            entries["x"] = {}
        self.assertFalse((self.state / "state.json.tmp").exists())
        # And the file on disk is valid JSON.
        json.loads((self.state / "state.json").read_text())

    def test_log_appends_lines(self):
        autoresume.log("first")
        autoresume.log("second")
        lines = (self.state / "log").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("first", lines[0])

    def test_iso_roundtrip_preserves_tz(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(autoresume.from_iso(autoresume.iso(dt)), dt)
