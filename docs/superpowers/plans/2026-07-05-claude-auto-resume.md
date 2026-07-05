# claude-auto-resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Claude Code session in cmux dies on a usage limit, automatically type a continue message into that pane once the limit resets — at most 3 attempts, then notify and give up.

**Architecture:** A Claude Code `StopFailure` hook upserts an entry into a JSON state file and spawns a detached waiter. The waiter (a flock-elected singleton) sleeps in 60s ticks and, when an entry comes due, nudges the pane via the `cmux` CLI, verifying success by finding a new assistant message in the session's transcript JSONL. Spec: `docs/superpowers/specs/2026-07-05-auto-resume-design.md`.

**Tech Stack:** Python 3.9+ stdlib only (`json`, `re`, `fcntl`, `subprocess`, `datetime`, `zoneinfo`, `argparse`, `unittest`). External commands: `cmux`, `claude`, `osascript` — all faked in tests via a temp dir prepended to `PATH`.

## Global Constraints

- Python 3 stdlib only. No pip installs, ever — not even for tests (stdlib `unittest`, run via `python3 -m unittest discover -s tests -v`).
- All logic lives in one importable module at repo root: `autoresume.py`. `bin/claude-auto-resume` is a two-line shim.
- Nudge message (exact copy, from spec): `Usage limits have reset — continue where you left off. If the task is already complete, ignore this message.`
- Max attempts per limit-event: 3. Reset buffer: 2 minutes. Retry backoff: 10 minutes. Parse-failure ladder: detection + 1h / 3h / 5h.
- State dir: `~/.local/state/claude-auto-resume/`, overridable via env `CLAUDE_AUTO_RESUME_STATE_DIR` (tests rely on this). Claude settings path overridable via `CLAUDE_SETTINGS_PATH`.
- Hooks must NEVER break a Claude session: top-level try/except, always exit 0, errors go to the log file.
- All timestamps are timezone-aware ISO-8601 strings in state; naive datetimes are a bug.
- Comments and docs in a plain human voice: explain why, admit caveats, no boilerplate. No AI-sounding filler.
- Commit after every task. Messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## File Structure

```
autoresume.py                    # all logic: parser, state, hooks, waiter, nudge, CLI
bin/claude-auto-resume           # shebang shim → autoresume.main()
tests/test_parser.py             # reset-time parsing (pure function, table-driven)
tests/test_state.py              # locked state file ops, corruption quarantine
tests/test_hooks.py              # hook-stop-failure / hook-stop behaviour
tests/test_pane.py               # cmux wrapper, pane classifier, transcript check
tests/test_nudge.py              # nudge decision flow against a fake cmux
tests/test_waiter.py             # waiter loop with injected clock/sleep
tests/test_install.py            # settings.json merge / manifest / uninstall
tests/helpers.py                 # temp state dir + fake-bin scaffolding shared by tests
README.md                        # Task 10
docs/superpowers/{specs,plans}/  # this document and the spec
```

A note on Phase 0 (recon) from the spec: the real `StopFailure` payload and real
pane screens can only be captured when an actual limit event happens on Josh's
machine. So the hook itself archives every raw payload it receives into
`<state dir>/captures/` (Task 4), and the README (Task 10) carries the manual
recon checklist. The parser ships against the historically known formats and
its fixtures get refreshed from captures — that is expected maintenance, not a
plan gap.

---

### Task 1: Repo skeleton, shim, and test harness

**Files:**
- Create: `autoresume.py`
- Create: `bin/claude-auto-resume`
- Create: `tests/__init__.py`, `tests/helpers.py`, `tests/test_parser.py` (smoke test only)
- Create: `.gitignore`

**Interfaces:**
- Produces: importable module `autoresume` with `MESSAGE`, `MAX_ATTEMPTS`, `state_dir() -> pathlib.Path`, `main(argv) -> int`. `tests.helpers.TempStateMixin` giving each test an isolated state dir.

- [ ] **Step 1: Write the failing smoke test**

`tests/__init__.py` is an empty file. `tests/helpers.py`:

```python
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
```

`tests/test_parser.py` (smoke test for now; real cases in Task 2):

```python
import unittest

import autoresume


class TestModule(unittest.TestCase):
    def test_module_constants_exist(self):
        self.assertEqual(autoresume.MAX_ATTEMPTS, 3)
        self.assertIn("ignore this message", autoresume.MESSAGE)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `python3 -m unittest discover -s tests -v`
Expected: ERROR — `ModuleNotFoundError: No module named 'autoresume'`

- [ ] **Step 3: Create the module skeleton and shim**

`autoresume.py`:

```python
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
```

`bin/claude-auto-resume`:

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autoresume import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
```

`.gitignore`:

```
__pycache__/
*.pyc
```

Then: `chmod +x bin/claude-auto-resume`

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: `OK` (1 test). Also run `./bin/claude-auto-resume; echo $?` → `0`.

- [ ] **Step 5: Commit**

```bash
git add autoresume.py bin/claude-auto-resume tests/ .gitignore
git commit -m "Add module skeleton, bin shim, and test harness

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Reset-time parser

**Files:**
- Modify: `autoresume.py`
- Modify: `tests/test_parser.py`

**Interfaces:**
- Produces: `parse_reset_at(text: str, now: datetime) -> datetime | None`. `now` must be tz-aware; result is tz-aware or None (None = caller uses the fallback ladder — never guess).

- [ ] **Step 1: Write the failing table-driven tests**

Replace `tests/test_parser.py` with:

```python
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import autoresume

CHI = ZoneInfo("America/Chicago")
# A fixed "now": Sunday 2026-07-05 13:00 Chicago time.
NOW = datetime(2026, 7, 5, 13, 0, tzinfo=CHI)


class TestParseResetAt(unittest.TestCase):
    def test_epoch_after_pipe(self):
        # Historical shape: "Claude AI usage limit reached|1751745600"
        got = autoresume.parse_reset_at(
            "Claude AI usage limit reached|1751745600", NOW)
        self.assertEqual(got, datetime.fromtimestamp(1751745600, tz=CHI))

    def test_bare_epoch(self):
        got = autoresume.parse_reset_at("limit reached 1751745600", NOW)
        self.assertEqual(int(got.timestamp()), 1751745600)

    def test_iso_timestamp(self):
        got = autoresume.parse_reset_at(
            "your limit resets at 2026-07-05T20:00:00Z", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc))

    def test_prose_time_with_tz_future_today(self):
        got = autoresume.parse_reset_at(
            "Your limit will reset at 6pm (America/Chicago).", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 18, 0, tzinfo=CHI))

    def test_prose_time_rolls_to_tomorrow(self):
        # 3am has already passed at NOW (13:00), so it means tomorrow 3am.
        got = autoresume.parse_reset_at(
            "Your limit will reset at 3am (America/Chicago).", NOW)
        self.assertEqual(got, datetime(2026, 7, 6, 3, 0, tzinfo=CHI))

    def test_prose_time_with_minutes_no_tz_uses_now_tz(self):
        got = autoresume.parse_reset_at("resets at 2:30pm", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 14, 30, tzinfo=CHI))

    def test_weekday_prose(self):
        # NOW is Sunday; "Thursday at 9am" = 2026-07-09 09:00.
        got = autoresume.parse_reset_at(
            "Weekly limit reached. Resets Thursday at 9am.", NOW)
        self.assertEqual(got, datetime(2026, 7, 9, 9, 0, tzinfo=CHI))

    def test_weekday_same_day_rolls_a_week(self):
        # "Sunday at 9am" when it's already Sunday 13:00 → next Sunday.
        got = autoresume.parse_reset_at("resets Sunday at 9am", NOW)
        self.assertEqual(got, datetime(2026, 7, 12, 9, 0, tzinfo=CHI))

    def test_unknown_tz_name_falls_back_to_now_tz(self):
        got = autoresume.parse_reset_at(
            "reset at 6pm (Made/Up_Zone)", NOW)
        self.assertEqual(got, datetime(2026, 7, 5, 18, 0, tzinfo=CHI))

    def test_garbage_returns_none(self):
        self.assertIsNone(autoresume.parse_reset_at("rate_limit", NOW))

    def test_small_numbers_are_not_epochs(self):
        # "429" and retry counts must not parse as timestamps.
        self.assertIsNone(autoresume.parse_reset_at(
            "429 rate_limit attempt 3 of 10 retry 5000ms", NOW))

    def test_result_is_tz_aware(self):
        got = autoresume.parse_reset_at("resets at 6pm", NOW)
        self.assertIsNotNone(got.tzinfo)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_parser -v`
Expected: ERRORs — `AttributeError: module 'autoresume' has no attribute 'parse_reset_at'`

- [ ] **Step 3: Implement the parser**

Add to `autoresume.py` (after the constants):

```python
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
        raw = m.group(0).replace(" ", "T").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed

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
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_parser -v`
Expected: `OK` (13 tests)

- [ ] **Step 5: Commit**

```bash
git add autoresume.py tests/test_parser.py
git commit -m "Parse reset times: epoch, ISO, prose time, weekday prose

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Locked state store

**Files:**
- Modify: `autoresume.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Consumes: `state_dir()` from Task 1.
- Produces: `locked_state()` context manager yielding a mutable `dict[str, dict]` (session_id → entry), persisted atomically on clean exit; `log(msg: str)` appending one timestamped line to `<state>/log`; `iso(dt: datetime) -> str`; `from_iso(s: str) -> datetime`.

- [ ] **Step 1: Write the failing tests**

`tests/test_state.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_state -v`
Expected: ERRORs — `AttributeError: ... no attribute 'locked_state'`

- [ ] **Step 3: Implement**

Add to `autoresume.py`:

```python
import fcntl
import time
from contextlib import contextmanager


def iso(dt):
    return dt.isoformat()


def from_iso(s):
    return datetime.fromisoformat(s)


def log(msg):
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(d / "log", "a") as f:
        f.write(f"{stamp} {msg}\n")


@contextmanager
def locked_state():
    """Read-modify-write the pending-sessions file under an exclusive lock.

    Several sessions can hit the limit in the same second, and the waiter
    mutates entries while hooks add them — every access goes through here.
    A corrupt file is renamed aside and treated as empty: a hook must never
    crash, and losing pending entries is recoverable (the next limit event
    re-adds them); crashing the hook chain is not.
    """
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "state.json"
    with open(d / "state.lock", "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            entries = {}
            if path.exists():
                try:
                    entries = json.loads(path.read_text())
                    if not isinstance(entries, dict):
                        raise ValueError("state root must be an object")
                except (ValueError, json.JSONDecodeError):
                    path.rename(d / f"state.json.corrupt-{int(time.time())}")
                    entries = {}
            yield entries
            tmp = d / "state.json.tmp"
            tmp.write_text(json.dumps(entries, indent=2))
            tmp.rename(path)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_state -v` → `OK` (6 tests)
Then the full suite: `python3 -m unittest discover -s tests -v` → `OK`

- [ ] **Step 5: Commit**

```bash
git add autoresume.py tests/test_state.py
git commit -m "Add flock-guarded state store with corruption quarantine

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: The two hooks

**Files:**
- Modify: `autoresume.py`
- Create: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `parse_reset_at`, `locked_state`, `log`, `iso`, `from_iso`.
- Produces:
  - `next_attempt_time(entry: dict, now: datetime) -> datetime` — shared scheduling rule, reused verbatim by the waiter in Task 7.
  - `cmd_hook_stop_failure(stdin_text: str, env=os.environ, now=None, spawn=None) -> int` — always returns 0.
  - `cmd_hook_stop(stdin_text: str) -> int` — always returns 0.
  - `spawn_waiter()` — detached `Popen` of `[sys.executable, autoresume.py, "wait"]`.
  - Entry schema (exact keys): `session_id, transcript_path, cwd, surface_id, workspace_id, detected_at, reset_at, attempts, next_attempt_at`.

- [ ] **Step 1: Write the failing tests**

`tests/test_hooks.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_hooks -v`
Expected: ERRORs — no attribute `cmd_hook_stop_failure`

- [ ] **Step 3: Implement**

Add to `autoresume.py`:

```python
import subprocess

RESET_BUFFER = timedelta(minutes=2)
RETRY_BACKOFF = timedelta(minutes=10)
LADDER = [timedelta(hours=1), timedelta(hours=3), timedelta(hours=5)]


def next_attempt_time(entry, now):
    """When to (re)try this entry.

    Known reset time: first attempt fires just after it; later attempts back
    off 10 minutes. Unknown reset time: a ladder anchored at detection —
    +1h/+3h/+5h covers the whole 5-hour window worst case.
    """
    if entry.get("reset_at"):
        if entry["attempts"] == 0:
            return from_iso(entry["reset_at"]) + RESET_BUFFER
        return now + RETRY_BACKOFF
    rung = min(entry["attempts"], len(LADDER) - 1)
    return from_iso(entry["detected_at"]) + LADDER[rung]


def spawn_waiter():
    """Fire-and-forget the waiter. It self-elects via flock (Task 7), so
    spawning duplicates is harmless — the losers exit immediately."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "log", "a") as out:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "wait"],
            stdout=out, stderr=out, stdin=subprocess.DEVNULL,
            start_new_session=True)


def cmd_hook_stop_failure(stdin_text, env=os.environ, now=None, spawn=None):
    """StopFailure(rate_limit) hook body. Must never raise, must exit 0."""
    try:
        now = now or datetime.now().astimezone()
        payload = json.loads(stdin_text)
        sid = payload["session_id"]

        # Archive the raw payload: these captures are how the parser's test
        # fixtures get refreshed when Anthropic changes the error format.
        cap = state_dir() / "captures"
        cap.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        (cap / f"{stamp}-{sid[:8]}.json").write_text(stdin_text)

        error_text = " ".join(
            str(payload.get(k, "")) for k in ("error", "error_details"))
        reset_at = parse_reset_at(error_text, now)

        with locked_state() as entries:
            prev = entries.get(sid, {})
            entry = {
                "session_id": sid,
                "transcript_path": payload.get("transcript_path"),
                "cwd": payload.get("cwd"),
                "surface_id": env.get("CMUX_SURFACE_ID"),
                "workspace_id": env.get("CMUX_WORKSPACE_ID"),
                "detected_at": prev.get("detected_at", iso(now)),
                "reset_at": iso(reset_at) if reset_at else None,
                # Preserved across re-upserts: a nudge that lands before the
                # limit actually reset re-fires this hook, and resetting the
                # counter would make that loop immortal.
                "attempts": prev.get("attempts", 0),
            }
            entry["next_attempt_at"] = iso(next_attempt_time(entry, now))
            entries[sid] = entry

        log(f"limit hit: session {sid} reset_at={entry['reset_at']} "
            f"next={entry['next_attempt_at']}")
        (spawn or spawn_waiter)()
    except Exception as exc:  # a hook must never break the Claude session
        try:
            log(f"hook error (stop-failure): {exc!r}")
        except Exception:
            pass
    return 0


def cmd_hook_stop(stdin_text):
    """Stop hook body: the session ended a turn normally, so any pending
    nudge for it is stale (finished, or resumed by hand). Drop it."""
    try:
        sid = json.loads(stdin_text).get("session_id")
        with locked_state() as entries:
            if entries.pop(sid, None) is not None:
                log(f"cleared pending nudge for {sid} (session progressed)")
    except Exception as exc:
        try:
            log(f"hook error (stop): {exc!r}")
        except Exception:
            pass
    return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_hooks -v` → `OK` (9 tests)
Full suite: `python3 -m unittest discover -s tests -v` → `OK`

- [ ] **Step 5: Commit**

```bash
git add autoresume.py tests/test_hooks.py
git commit -m "Record limit hits via StopFailure hook; clear them via Stop hook

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: cmux wrapper, pane classifier, transcript check, notify

**Files:**
- Modify: `autoresume.py`
- Create: `tests/test_pane.py`

**Interfaces:**
- Consumes: `make_fake_bin` from `tests/helpers.py`.
- Produces:
  - `cmux(*args, timeout=15) -> subprocess.CompletedProcess` (text mode, never raises on non-zero exit; raises only on timeout/missing binary — callers catch `Exception` and treat as failure).
  - `classify_pane(screen: str) -> str` — `"repl"` | `"shell"` | `"unknown"`.
  - `transcript_has_assistant_after(path: str, since: datetime) -> bool`.
  - `notify(title: str, body: str)` — best-effort `cmux notify` + `osascript`, never raises.
  - Module constant `REPL_MARKERS: tuple[str, ...]` — the strings that identify a live Claude REPL. **Phase 0 recon refines this tuple from real screen captures; keeping it a top-of-file constant is the point.**

- [ ] **Step 1: Write the failing tests**

`tests/test_pane.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_pane -v`
Expected: ERRORs — no attribute `classify_pane`

- [ ] **Step 3: Implement**

Add to `autoresume.py`:

```python
# Strings that mean "a live Claude REPL is on screen". The TUI's chrome is
# not a stable API — Phase 0 recon replaces these with strings from real
# `cmux read-screen` captures, and they'll need the odd touch-up after
# Claude Code updates. Low stakes: this only picks HOW we nudge (type into
# the REPL vs relaunch claude first); the transcript check judges success.
REPL_MARKERS = (
    "? for shortcuts",
    "esc to interrupt",
    "/help for help",
    "╭─",
)

_SHELL_PROMPT_RE = re.compile(r"[$%❯>]\s*$")


def classify_pane(screen):
    if any(marker in screen for marker in REPL_MARKERS):
        return "repl"
    lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
    if lines and _SHELL_PROMPT_RE.search(lines[-1]):
        return "shell"
    return "unknown"


def cmux(*args, timeout=15):
    """Run the cmux CLI. Non-zero exits come back in the result; only a
    missing binary or a hang raises — callers treat any Exception as a
    failed nudge attempt, which is the safe direction."""
    return subprocess.run(("cmux",) + args, capture_output=True,
                          text=True, timeout=timeout)


def notify(title, body):
    """Tell the human. Both channels are best-effort: notifications failing
    must never take down the waiter."""
    for cmd in (("cmux", "notify", "--title", title, body),
                ("osascript", "-e",
                 f'display notification {json.dumps(body)} '
                 f'with title {json.dumps(title)}')):
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass
    try:
        log(f"notify: {title}: {body}")
    except Exception:
        pass


def transcript_has_assistant_after(path, since):
    """True iff the session transcript has an assistant message newer than
    `since` — the only evidence we trust that a resume actually worked."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when > since:
            return True
    return False
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_pane -v` → `OK` (10 tests)
Full suite: `python3 -m unittest discover -s tests -v` → `OK`

- [ ] **Step 5: Commit**

```bash
git add autoresume.py tests/test_pane.py
git commit -m "Add cmux wrapper, pane classifier, transcript check, notify

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: The nudge

**Files:**
- Modify: `autoresume.py`
- Create: `tests/test_nudge.py`

**Interfaces:**
- Consumes: `cmux`, `classify_pane`, `transcript_has_assistant_after`, `MESSAGE`.
- Produces: `nudge_entry(entry: dict, sleep=time.sleep, verify_wait=30) -> str` returning one of `"resumed"`, `"retry"`, `"dead_pane"`, `"no_pane_id"`. Pure decision + side effects on the pane; the CALLER (waiter, Task 7) mutates state and sends notifications based on the outcome string.

- [ ] **Step 1: Write the failing tests**

The fake cmux for these tests reads canned responses from files so each test
scripts the pane's behaviour. Add this scenario builder at the top of
`tests/test_nudge.py`:

```python
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
```

Then the tests, same file:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m unittest tests.test_nudge -v`
Expected: ERRORs — no attribute `nudge_entry`

- [ ] **Step 3: Implement**

Add to `autoresume.py`:

```python
import shlex


def nudge_entry(entry, sleep=time.sleep, verify_wait=30):
    """Try to wake one interrupted session. Returns an outcome string; the
    waiter maps outcomes onto state changes and notifications.

    The screen tells us HOW to nudge (type into a live REPL, or relaunch
    claude first); only a fresh assistant message in the transcript tells
    us it WORKED. An unknown screen gets the optimistic path — worst case
    the verify step calls it a miss and we retry.
    """
    sid = entry["session_id"]
    surface = entry.get("surface_id")
    if not surface:
        return "no_pane_id"
    try:
        panels = cmux("list-panels")
        if panels.returncode != 0 or surface not in panels.stdout:
            return "dead_pane"

        screen = cmux("read-screen", "--surface", surface,
                      "--lines", "50").stdout
        if classify_pane(screen) == "shell":
            # claude exited on the limit (it does that sometimes — cmux
            # issue #2488). Relaunch it resumed, in the right directory.
            relaunch = (f"cd {shlex.quote(entry['cwd'])} && "
                        f"claude --resume {shlex.quote(sid)}")
            cmux("send", "--surface", surface, relaunch)
            cmux("send-key", "--surface", surface, "enter")
            for _ in range(12):          # up to ~60s for the REPL to draw
                sleep(5)
                screen = cmux("read-screen", "--surface", surface,
                              "--lines", "50").stdout
                if classify_pane(screen) == "repl":
                    break
            else:
                log(f"nudge {sid}: relaunch never reached a REPL")
                return "retry"

        since = datetime.now().astimezone()
        cmux("send", "--surface", surface, MESSAGE)
        cmux("send-key", "--surface", surface, "enter")
        sleep(verify_wait)
        if transcript_has_assistant_after(entry["transcript_path"], since):
            return "resumed"
        log(f"nudge {sid}: no assistant reply after {verify_wait}s")
        return "retry"
    except Exception as exc:
        log(f"nudge {sid}: {exc!r}")
        return "retry"
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m unittest tests.test_nudge -v` → `OK` (8 tests)
Full suite: `python3 -m unittest discover -s tests -v` → `OK`

- [ ] **Step 5: Commit**

```bash
git add autoresume.py tests/test_nudge.py
git commit -m "Nudge interrupted panes: type, relaunch if needed, verify by transcript

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---
