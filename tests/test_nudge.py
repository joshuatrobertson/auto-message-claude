import json
import unittest
from datetime import datetime, timezone

import autoresume
from tests.helpers import TempStateMixin

REPL_SCREEN = "│ > │\n? for shortcuts"
SHELL_SCREEN = "josh@mac proj %"


class NudgeHarness(TempStateMixin, unittest.TestCase):
    """Scripts a fake cmux: panels list and a queue of screens (one per
    read-screen call), and records every cmux invocation to calls.log."""

    def setUp(self):
        super().setUp()
        self.calls = self.tmp / "calls.log"
        self.panels = self.tmp / "panels.txt"
        self.screens = self.tmp / "screens"
        self.screens.mkdir()
        self.panels.write_text("surf-9\n")
        self.make_fake_bin("cmux", f"""
echo "$@" >> {self.calls}
case "$1" in
  list-panels) cat {self.panels} ;;
  read-screen)
    f=$(ls {self.screens} | head -1)
    if [ -n "$f" ]; then cat {self.screens}/$f; rm {self.screens}/$f; fi ;;
esac
exit 0
""")
        self.transcript = self.tmp / "t.jsonl"
        self.transcript.write_text("")
        self.entry = {
            "session_id": "sess-123",
            "transcript_path": str(self.transcript),
            "cwd": "/Users/josh/my proj",   # space is deliberate: quoting
            "surface_id": "surf-9",
            "workspace_id": "ws-2",
            "detected_at": "2026-07-05T13:00:00+00:00",
            "reset_at": None, "attempts": 0,
            "next_attempt_at": "2026-07-05T14:00:00+00:00",
        }

    def queue_screen(self, text):
        n = len(list(self.screens.iterdir()))
        (self.screens / f"{n:03d}.txt").write_text(text)

    def grow_transcript_on_sleep(self):
        # Simulate Claude answering: the verify-wait sleep writes an
        # assistant line stamped "now".
        def fake_sleep(_secs):
            stamp = datetime.now(timezone.utc).isoformat()
            with open(self.transcript, "a") as f:
                f.write(json.dumps(
                    {"type": "assistant", "timestamp": stamp}) + "\n")
        return fake_sleep

    def cmux_call_lines(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []


class TestNudge(NudgeHarness):
    def test_repl_alive_types_message_and_verifies(self):
        self.queue_screen(REPL_SCREEN)
        out = autoresume.nudge_entry(self.entry,
                                     sleep=self.grow_transcript_on_sleep())
        self.assertEqual(out, "resumed")
        sends = [l for l in self.cmux_call_lines() if l.startswith("send ")]
        self.assertIn(autoresume.MESSAGE, sends[0])
        self.assertTrue(any(l.startswith("send-key")
                            for l in self.cmux_call_lines()))

    def test_no_assistant_reply_means_retry(self):
        self.queue_screen(REPL_SCREEN)
        out = autoresume.nudge_entry(self.entry, sleep=lambda s: None)
        self.assertEqual(out, "retry")

    def test_shell_pane_relaunches_claude_first(self):
        self.queue_screen(SHELL_SCREEN)   # first read: shell
        self.queue_screen(REPL_SCREEN)    # after relaunch: REPL is back
        out = autoresume.nudge_entry(self.entry,
                                     sleep=self.grow_transcript_on_sleep())
        self.assertEqual(out, "resumed")
        sends = [l for l in self.cmux_call_lines() if l.startswith("send ")]
        # First send is the relaunch command, cwd-scoped and quoted.
        self.assertIn("claude --resume sess-123", sends[0])
        self.assertIn("'/Users/josh/my proj'", sends[0])
        self.assertIn(autoresume.MESSAGE, sends[1])

    def test_relaunch_that_never_reaches_repl_is_retry(self):
        self.queue_screen(SHELL_SCREEN)  # and nothing after: stays blank
        out = autoresume.nudge_entry(self.entry, sleep=lambda s: None)
        self.assertEqual(out, "retry")

    def test_gone_pane_is_dead(self):
        self.panels.write_text("other-surface\n")
        out = autoresume.nudge_entry(self.entry, sleep=lambda s: None)
        self.assertEqual(out, "dead_pane")

    def test_entry_without_surface_is_no_pane_id(self):
        self.entry["surface_id"] = None
        out = autoresume.nudge_entry(self.entry, sleep=lambda s: None)
        self.assertEqual(out, "no_pane_id")

    def test_unknown_screen_is_treated_as_repl(self):
        # Screen chrome we don't recognise: try typing anyway; the
        # transcript check is what decides success.
        self.queue_screen("static noise")
        out = autoresume.nudge_entry(self.entry,
                                     sleep=self.grow_transcript_on_sleep())
        self.assertEqual(out, "resumed")

    def test_cmux_binary_missing_is_retry_not_crash(self):
        import os
        os.environ["PATH"] = str(self.tmp / "nowhere")
        out = autoresume.nudge_entry(self.entry, sleep=lambda s: None)
        self.assertEqual(out, "retry")
