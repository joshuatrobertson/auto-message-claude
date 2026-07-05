"""Shared test scaffolding.

Every test gets an isolated state dir (via env var) and, where needed, a
directory of fake executables prepended to PATH so autoresume never touches
the real cmux/claude/osascript during tests.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path


class TempStateMixin(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self._old_env = dict(os.environ)
        os.environ["CLAUDE_AUTO_RESUME_STATE_DIR"] = str(self.state)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def make_fake_bin(self, name, script):
        """Drop an executable shell script into tmp/fakebin and put it on PATH."""
        fakebin = self.tmp / "fakebin"
        fakebin.mkdir(exist_ok=True)
        path = fakebin / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = f"{fakebin}:{os.environ['PATH']}"
        return path
