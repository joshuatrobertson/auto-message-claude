import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import autoresume
from tests.helpers import TempStateMixin

NOW = datetime(2026, 7, 5, 13, 0, tzinfo=ZoneInfo("America/Chicago"))

PAYLOAD = {
    "session_id": "sess-123",
    "transcript_path": "/tmp/t/sess-123.jsonl",
    "cwd": "/Users/josh/proj",
    "error": "rate_limit",
    "error_details": "Your limit will reset at 6pm (America/Chicago).",
}
ENV = {"CMUX_SURFACE_ID": "surf-9", "CMUX_WORKSPACE_ID": "ws-2"}


def run_hook(payload=PAYLOAD, env=ENV, now=NOW, spawns=None):
    spawns = spawns if spawns is not None else []
    rc = autoresume.cmd_hook_stop_failure(
        json.dumps(payload), env=env, now=now,
        spawn=lambda: spawns.append(True))
    return rc, spawns


class TestStopFailureHook(TempStateMixin, unittest.TestCase):
    def test_creates_entry_with_schema_and_spawns_waiter(self):
        rc, spawns = run_hook()
        self.assertEqual(rc, 0)
        self.assertEqual(spawns, [True])
        with autoresume.locked_state() as entries:
            e = entries["sess-123"]
        self.assertEqual(e["surface_id"], "surf-9")
        self.assertEqual(e["workspace_id"], "ws-2")
        self.assertEqual(e["cwd"], "/Users/josh/proj")
        self.assertEqual(e["attempts"], 0)
        # 6pm Chicago + 2 min buffer
        self.assertEqual(autoresume.from_iso(e["next_attempt_at"]),
                         NOW.replace(hour=18, minute=2))

    def test_unparseable_reset_uses_ladder_hour_one(self):
        p = dict(PAYLOAD, error_details="tough luck")
        run_hook(payload=p)
        with autoresume.locked_state() as entries:
            e = entries["sess-123"]
        self.assertIsNone(e["reset_at"])
        self.assertEqual(autoresume.from_iso(e["next_attempt_at"]),
                         NOW + timedelta(hours=1))

    def test_reupsert_preserves_attempts(self):
        # A nudge-too-early triggers a second StopFailure; the retry count
        # must survive or the loop never terminates.
        run_hook()
        with autoresume.locked_state() as entries:
            entries["sess-123"]["attempts"] = 2
        run_hook()
        with autoresume.locked_state() as entries:
            self.assertEqual(entries["sess-123"]["attempts"], 2)

    def test_outside_cmux_records_null_surface(self):
        rc, _ = run_hook(env={})
        self.assertEqual(rc, 0)
        with autoresume.locked_state() as entries:
            self.assertIsNone(entries["sess-123"]["surface_id"])

    def test_raw_payload_is_captured(self):
        run_hook()
        captures = list((self.state / "captures").glob("*.json"))
        self.assertEqual(len(captures), 1)
        self.assertEqual(json.loads(captures[0].read_text()), PAYLOAD)

    def test_garbage_stdin_never_raises_and_exits_zero(self):
        rc = autoresume.cmd_hook_stop_failure("not json{", env={}, now=NOW)
        self.assertEqual(rc, 0)
        self.assertIn("hook error", (self.state / "log").read_text())


class TestStopHook(TempStateMixin, unittest.TestCase):
    def test_clears_pending_entry_for_session(self):
        run_hook()
        rc = autoresume.cmd_hook_stop(json.dumps({"session_id": "sess-123"}))
        self.assertEqual(rc, 0)
        with autoresume.locked_state() as entries:
            self.assertEqual(entries, {})

    def test_unknown_session_is_a_quiet_noop(self):
        rc = autoresume.cmd_hook_stop(json.dumps({"session_id": "nope"}))
        self.assertEqual(rc, 0)

    def test_garbage_stdin_exits_zero(self):
        self.assertEqual(autoresume.cmd_hook_stop("]["), 0)
