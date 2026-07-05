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

import json  # noqa: F401  (used from Task 3 on; imported here to keep one block)
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"]

# The reset time arrives inside human-readable error text, and Anthropic has
# changed its shape before. Parsers are tried strictest-first; if none match
# we return None and the caller falls back to a retry ladder. Never guess.

_EPOCH_RE = re.compile(r"\b(1[6-9]\d{8}|20\d{8})\b")
_ISO_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")
_PROSE_RE = re.compile(
    r"resets?\s+(?:on\s+)?"
    r"(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?"
    r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)"
    r"(?:\s*\(([^)]+)\))?",
    re.IGNORECASE)


def parse_reset_at(text, now):
    """Extract a reset timestamp from limit-error text, or None.

    `now` anchors relative phrases ("at 3am" means the NEXT 3am) and supplies
    the timezone when the text names none.
    """
    m = _EPOCH_RE.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=now.tzinfo)

    m = _ISO_RE.search(text)
    if m:
        try:
            raw = m.group(0).replace(" ", "T").replace("Z", "+00:00")
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            return parsed
        except ValueError:
            pass  # shaped like a date, isn't one — try the other formats

    m = _PROSE_RE.search(text)
    if m:
        weekday, hour, minute, ampm, tzname = m.groups()
        hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
        minute = int(minute or 0)
        tz = now.tzinfo
        if tzname:
            try:
                tz = ZoneInfo(tzname.strip())
            except Exception:
                pass  # unknown zone name: the local zone is the best we have
        local_now = now.astimezone(tz)
        candidate = local_now.replace(hour=hour, minute=minute,
                                      second=0, microsecond=0)
        if weekday:
            days = (WEEKDAYS.index(weekday.lower()) - candidate.weekday()) % 7
            candidate += timedelta(days=days)
        if candidate <= local_now:
            candidate += timedelta(days=7 if weekday else 1)
        return candidate

    return None


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
