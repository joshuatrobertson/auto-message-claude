import json
import unittest
from datetime import datetime, timedelta, timezone

import autoresume
from tests.helpers import TempStateMixin

REPL_SCREEN = """
╭──────────────────────────────────────────╮
│ > Try "fix lint errors"                  │
╰──────────────────────────────────────────╯
  ? for shortcuts
"""

SHELL_SCREEN = """
[Process completed]
josh@mac proj %
"""


class TestClassifyPane(unittest.TestCase):
    def test_repl_markers_win(self):
        self.assertEqual(autoresume.classify_pane(REPL_SCREEN), "repl")

    def test_shell_prompt_detected(self):
        self.assertEqual(autoresume.classify_pane(SHELL_SCREEN), "shell")

    def test_empty_or_weird_is_unknown(self):
        self.assertEqual(autoresume.classify_pane(""), "unknown")
        self.assertEqual(autoresume.classify_pane("static\nnoise"), "unknown")


class TestCmuxWrapper(TempStateMixin, unittest.TestCase):
    def test_invokes_cmux_from_path_and_captures_stdout(self):
        self.make_fake_bin("cmux", 'echo "surf-1 surf-2"')
        out = autoresume.cmux("list-panels")
        self.assertEqual(out.returncode, 0)
        self.assertIn("surf-1", out.stdout)

    def test_nonzero_exit_does_not_raise(self):
        self.make_fake_bin("cmux", "exit 3")
        self.assertEqual(autoresume.cmux("send").returncode, 3)


class TestNotify(TempStateMixin, unittest.TestCase):
    def test_notify_survives_missing_binaries(self):
        # No fake cmux/osascript on PATH at all: must not raise.
        import os
        os.environ["PATH"] = str(self.tmp / "empty")
        autoresume.notify("t", "b")  # passes if no exception

    def test_notify_calls_both_channels(self):
        calls = self.tmp / "calls.log"
        self.make_fake_bin("cmux", f'echo "cmux $@" >> {calls}')
        self.make_fake_bin("osascript", f'echo "osa" >> {calls}')
        autoresume.notify("title", "body")
        text = calls.read_text()
        self.assertIn("cmux notify", text)
        self.assertIn("osa", text)


class TestTranscriptCheck(TempStateMixin, unittest.TestCase):
    def _write(self, *entries):
        p = self.tmp / "t.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in entries))
        return str(p)

    def test_new_assistant_message_counts(self):
        since = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
        p = self._write(
            {"type": "user", "timestamp": "2026-07-05T18:01:00Z"},
            {"type": "assistant", "timestamp": "2026-07-05T18:01:30Z"},
        )
        self.assertTrue(autoresume.transcript_has_assistant_after(p, since))

    def test_user_message_alone_is_not_proof(self):
        # The TUI logs the user message BEFORE the API call; if the limit is
        # still active that's all that appears. Must not count as success.
        since = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
        p = self._write(
            {"type": "user", "timestamp": "2026-07-05T18:01:00Z"},
        )
        self.assertFalse(autoresume.transcript_has_assistant_after(p, since))

    def test_old_assistant_messages_do_not_count(self):
        since = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
        p = self._write(
            {"type": "assistant", "timestamp": "2026-07-05T17:59:00Z"},
        )
        self.assertFalse(autoresume.transcript_has_assistant_after(p, since))

    def test_missing_file_and_junk_lines_are_false_not_fatal(self):
        since = datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc)
        self.assertFalse(
            autoresume.transcript_has_assistant_after("/nope.jsonl", since))
        p = self.tmp / "junk.jsonl"
        p.write_text("not json\n{\"type\": \"assistant\"}\n")
        self.assertFalse(
            autoresume.transcript_has_assistant_after(str(p), since))
