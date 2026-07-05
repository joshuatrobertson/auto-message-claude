import fcntl
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import autoresume
from tests.helpers import TempStateMixin

T0 = datetime(2026, 7, 5, 13, 0, tzinfo=ZoneInfo("America/Chicago"))


class Clock:
    """Injected clock: sleep() just advances now()."""
    def __init__(self, start):
        self.t = start
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, secs):
        self.t += timedelta(seconds=secs)
        self.sleeps += 1
        if self.sleeps > 10000:
            raise AssertionError("waiter never exited")


def entry(sid="sess-1", reset_at=None, attempts=0, due=T0):
    return {
        "session_id": sid, "transcript_path": "/tmp/t.jsonl",
        "cwd": "/tmp", "surface_id": "surf-9", "workspace_id": "ws-2",
        "detected_at": autoresume.iso(T0 - timedelta(minutes=5)),
        "reset_at": autoresume.iso(reset_at) if reset_at else None,
        "attempts": attempts, "next_attempt_at": autoresume.iso(due),
    }


def put(*entries_):
    with autoresume.locked_state() as s:
        for e in entries_:
            s[e["session_id"]] = e


def run_waiter(clock, outcomes):
    """outcomes: list consumed one per nudge call; records what got nudged."""
    nudged = []

    def fake_nudge(e, **kw):
        nudged.append((e["session_id"], e["attempts"]))
        return outcomes.pop(0)

    rc = autoresume.cmd_wait(tick=60, sleep=clock.sleep,
                             now_fn=clock.now, nudge=fake_nudge)
    return rc, nudged


class TestWaiter(TempStateMixin, unittest.TestCase):
    def test_empty_state_exits_immediately(self):
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, [])
        self.assertEqual(rc, 0)
        self.assertEqual(nudged, [])

    def test_due_entry_resumed_and_removed(self):
        put(entry(due=T0))
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, ["resumed"])
        self.assertEqual(rc, 0)
        self.assertEqual(nudged, [("sess-1", 0)])
        with autoresume.locked_state() as s:
            self.assertEqual(s, {})
        self.assertIn("Resumed", (self.state / "log").read_text())

    def test_not_due_yet_waits_for_its_time(self):
        put(entry(due=T0 + timedelta(minutes=30)))
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, ["resumed"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(nudged), 1)
        # 30 minutes of 60s ticks had to pass first.
        self.assertGreaterEqual(clock.sleeps, 30)

    def test_retry_backs_off_then_gives_up_at_max(self):
        put(entry(reset_at=T0, due=T0, attempts=0))
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, ["retry", "retry", "retry"])
        self.assertEqual(rc, 0)
        # attempts seen by each nudge: 0, then 1, then 2 — then removal.
        self.assertEqual(nudged, [("sess-1", 0), ("sess-1", 1), ("sess-1", 2)])
        with autoresume.locked_state() as s:
            self.assertEqual(s, {})
        self.assertIn("gave up", (self.state / "log").read_text())

    def test_dead_pane_is_removed_with_notification(self):
        put(entry(due=T0))
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, ["dead_pane"])
        self.assertEqual(rc, 0)
        with autoresume.locked_state() as s:
            self.assertEqual(s, {})
        self.assertIn("by hand", (self.state / "log").read_text())

    def test_two_sessions_processed_independently(self):
        put(entry(sid="a", due=T0), entry(sid="b", due=T0))
        clock = Clock(T0)
        rc, nudged = run_waiter(clock, ["resumed", "resumed"])
        self.assertEqual(sorted(n[0] for n in nudged), ["a", "b"])

    def test_entry_cleared_mid_nudge_stays_cleared(self):
        # The Stop hook can remove an entry while a nudge is in flight
        # (user resumed by hand). The waiter must not resurrect it.
        put(entry(due=T0))
        clock = Clock(T0)

        def clearing_nudge(e, **kw):
            with autoresume.locked_state() as s:
                s.pop("sess-1", None)
            return "retry"

        rc = autoresume.cmd_wait(tick=60, sleep=clock.sleep,
                                 now_fn=clock.now, nudge=clearing_nudge)
        self.assertEqual(rc, 0)
        with autoresume.locked_state() as s:
            self.assertEqual(s, {})

    def test_singleton_second_waiter_exits_at_once(self):
        put(entry(due=T0 + timedelta(hours=1)))
        lockf = open(self.state / "waiter.lock", "w")
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            clock = Clock(T0)
            rc, nudged = run_waiter(clock, [])
            self.assertEqual(rc, 0)
            self.assertEqual(nudged, [])   # never even looked at state
            self.assertEqual(clock.sleeps, 0)
        finally:
            lockf.close()
