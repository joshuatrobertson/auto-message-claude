import contextlib
import io
import unittest

import autoresume
from tests.helpers import TempStateMixin


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = autoresume.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class TestCli(TempStateMixin, unittest.TestCase):
    def test_no_args_prints_usage_ok(self):
        rc, out, _ = run_main()
        self.assertEqual(rc, 0)
        self.assertIn("claude-auto-resume", out)
        self.assertIn("install", out)

    def test_unknown_command_is_exit_2(self):
        rc, _, err = run_main("frobnicate")
        self.assertEqual(rc, 2)
        self.assertIn("claude-auto-resume", err)

    def test_status_empty(self):
        rc, out, _ = run_main("status")
        self.assertEqual(rc, 0)
        self.assertIn("waiter: not running", out)
        self.assertIn("nothing pending", out)

    def test_status_lists_entries(self):
        with autoresume.locked_state() as s:
            s["sess-abc"] = {
                "session_id": "sess-abc", "transcript_path": "/t",
                "cwd": "/tmp", "surface_id": "surf-1", "workspace_id": None,
                "detected_at": "2026-07-05T13:00:00+00:00",
                "reset_at": None, "attempts": 1,
                "next_attempt_at": "2026-07-05T14:00:00+00:00",
            }
        rc, out, _ = run_main("status")
        self.assertEqual(rc, 0)
        self.assertIn("sess-abc", out)
        self.assertIn("attempts=1", out)

    def test_nudge_unknown_session_fails(self):
        rc, _, err = run_main("nudge", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no pending entry", err)

    def test_hook_commands_read_stdin(self):
        import json
        import sys
        with autoresume.locked_state() as s:
            s["sess-x"] = {"session_id": "sess-x", "attempts": 0}
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"session_id": "sess-x"}))
        try:
            rc, _, _ = run_main("hook-stop")
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        with autoresume.locked_state() as s:
            self.assertEqual(s, {})

    def test_nudge_resumed_returns_zero_and_pops(self):
        with autoresume.locked_state() as s:
            s["sess-ok"] = {"session_id": "sess-ok", "attempts": 0}
        real = autoresume.nudge_entry
        autoresume.nudge_entry = lambda entry, **kw: "resumed"
        self.addCleanup(setattr, autoresume, "nudge_entry", real)
        rc, out, _ = run_main("nudge", "sess-ok")
        self.assertEqual(rc, 0)
        self.assertIn("resumed", out)
        with autoresume.locked_state() as s:
            self.assertNotIn("sess-ok", s)
