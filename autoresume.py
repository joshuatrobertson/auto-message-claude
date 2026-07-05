#!/usr/bin/env python3
"""claude-auto-resume: nudge usage-limited Claude Code sessions back to life.

One file on purpose. A Claude Code StopFailure hook records interrupted
sessions here; a detached waiter sleeps until the limit resets, then types a
continue message into the original cmux pane. Design rationale lives in
docs/superpowers/specs/2026-07-05-auto-resume-design.md.
"""
import os
import sys
from pathlib import Path

MESSAGE = ("Usage limits have reset — continue where you left off. "
           "If the task is already complete, ignore this message.")
MAX_ATTEMPTS = 3


def state_dir() -> Path:
    """Where pending sessions, locks, logs and captures live.

    Overridable so tests never touch the real one.
    """
    return Path(os.environ.get(
        "CLAUDE_AUTO_RESUME_STATE_DIR",
        str(Path.home() / ".local" / "state" / "claude-auto-resume"),
    ))


def main(argv=None) -> int:
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
