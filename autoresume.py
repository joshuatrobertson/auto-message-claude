#!/usr/bin/env python3
"""claude-auto-resume: nudge usage-limited Claude Code sessions back to life.

One file on purpose. A Claude Code StopFailure hook records interrupted
sessions here; a detached waiter sleeps until the limit resets, then types a
continue message into the original cmux pane. See README.md for the how and
the why.
"""
import os
import subprocess
import sys
from pathlib import Path

MESSAGE = ("Usage limits have reset — continue where you left off. "
           "If the task is already complete, ignore this message.")
MAX_ATTEMPTS = 3

import fcntl
import json
import re
import shlex
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RESET_BUFFER = timedelta(minutes=2)
RETRY_BACKOFF = timedelta(minutes=10)
LADDER = [timedelta(hours=1), timedelta(hours=3), timedelta(hours=5)]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday"]

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


def cmd_wait(tick=60, sleep=time.sleep, now_fn=None, nudge=nudge_entry):
    """The waiter: a deliberately-not-a-daemon loop that exists only while
    entries are pending.

    Singleton by election: whoever wins the flock runs; everyone else exits
    on the spot. Hooks spawn waiters unconditionally, so losing this race
    is the normal case, not an error.
    """
    now_fn = now_fn or (lambda: datetime.now().astimezone())
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    lockf = open(d / "waiter.lock", "a")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    log("waiter: elected, watching for due sessions")
    try:
        while True:
            now = now_fn()
            with locked_state() as entries:
                if not entries:
                    log("waiter: nothing pending, exiting")
                    return 0
                due = [dict(e) for e in entries.values()
                       if from_iso(e["next_attempt_at"]) <= now]

            for snapshot in due:
                outcome = nudge(snapshot)
                sid = snapshot["session_id"]
                short = sid[:8]
                with locked_state() as entries:
                    if sid not in entries:
                        # Stop hook cleared it while we nudged: the session
                        # progressed some other way. Leave it cleared.
                        continue
                    if outcome == "resumed":
                        entries.pop(sid)
                        notify("claude-auto-resume",
                               f"Resumed session {short} — back to work.")
                    elif outcome in ("dead_pane", "no_pane_id"):
                        entries.pop(sid)
                        notify("claude-auto-resume",
                               f"Can't reach session {short} ({outcome}). "
                               f"Resume it by hand.")
                    else:  # retry
                        e = entries[sid]
                        e["attempts"] += 1
                        if e["attempts"] >= MAX_ATTEMPTS:
                            entries.pop(sid)
                            log(f"waiter: gave up on {short} after "
                                f"{MAX_ATTEMPTS} attempts")
                            notify("claude-auto-resume",
                                   f"Gave up on session {short} after "
                                   f"{MAX_ATTEMPTS} attempts. Resume it by hand.")
                        else:
                            e["next_attempt_at"] = iso(
                                next_attempt_time(e, now_fn()))
                            log(f"waiter: {short} attempt "
                                f"{e['attempts']}, next {e['next_attempt_at']}")

            sleep(tick)
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)
        lockf.close()


def settings_path():
    """Path to Claude Code's settings.json, honouring env CLAUDE_SETTINGS_PATH."""
    return Path(os.environ.get(
        "CLAUDE_SETTINGS_PATH",
        str(Path.home() / ".claude" / "settings.json")))


def _hook_commands():
    """The exact command strings we wire into Claude settings. Absolute
    paths: hooks run with no useful PATH or cwd guarantees."""
    me = Path(__file__).resolve()
    return {
        "StopFailure": f'"{sys.executable}" "{me}" hook-stop-failure',
        "Stop": f'"{sys.executable}" "{me}" hook-stop',
    }


def _existing_commands(groups):
    """Extract command strings from a list of hook groups."""
    return {h.get("command") for grp in groups for h in grp.get("hooks", [])}


def cmd_install():
    """Install the auto-resume hooks into Claude Code's settings.json."""
    sp = settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if sp.exists():
        backup = sp.with_name(f"settings.json.backup-{int(time.time())}")
        backup.write_text(sp.read_text())
        print(f"backed up settings to {backup}")
        data = json.loads(sp.read_text())  # corrupt settings should fail
                                           # loudly here, not get clobbered

    cmds = _hook_commands()
    hooks = data.setdefault("hooks", {})
    added = []

    sf = hooks.setdefault("StopFailure", [])
    if cmds["StopFailure"] not in _existing_commands(sf):
        sf.append({"matcher": "rate_limit", "hooks": [
            {"type": "command", "command": cmds["StopFailure"]}]})
        added.append(cmds["StopFailure"])

    st = hooks.setdefault("Stop", [])
    if cmds["Stop"] not in _existing_commands(st):
        st.append({"hooks": [
            {"type": "command", "command": cmds["Stop"]}]})
        added.append(cmds["Stop"])

    sp.write_text(json.dumps(data, indent=2) + "\n")

    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "install-manifest.json").write_text(json.dumps(
        {"settings_path": str(sp), "commands": list(cmds.values())},
        indent=2) + "\n")

    for c in added:
        print(f"added hook: {c}")
    if not added:
        print("hooks already installed, nothing to do")
    return 0


def cmd_uninstall():
    """Remove auto-resume hooks from Claude Code's settings.json."""
    mf = state_dir() / "install-manifest.json"
    if not mf.exists():
        print("no install manifest found — nothing to remove", file=sys.stderr)
        return 1
    manifest = json.loads(mf.read_text())
    ours = set(manifest["commands"])
    sp = Path(manifest["settings_path"])
    removed = 0
    if sp.exists():
        data = json.loads(sp.read_text())
        events = data.get("hooks", {})
        for event in list(events):
            groups = events[event]
            for grp in list(groups):
                kept = [h for h in grp.get("hooks", [])
                        if h.get("command") not in ours]
                removed += len(grp.get("hooks", [])) - len(kept)
                if kept:
                    grp["hooks"] = kept
                else:
                    groups.remove(grp)
            if not groups:
                del events[event]
        sp.write_text(json.dumps(data, indent=2) + "\n")
    mf.unlink()
    print(f"removed {removed} hook entr{'y' if removed == 1 else 'ies'}")
    return 0


USAGE = """\
claude-auto-resume — resume usage-limited Claude Code sessions in cmux

usage: claude-auto-resume <command>

  install       wire the hooks into Claude Code's settings.json
  uninstall     remove exactly what install added, nothing else
  status        show pending sessions and whether the waiter is alive
  nudge <sid>   try to resume one pending session right now
  wait          run the waiter loop in the foreground (debugging)

internal commands invoked by Claude Code hooks:
  hook-stop-failure, hook-stop
"""


def waiter_is_running():
    """A waiter holds an exclusive flock for its whole life, so 'can we
    take the lock' is the liveness check — no pidfiles to go stale."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        with open(d / "waiter.lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def cmd_status():
    print(f"waiter: {'running' if waiter_is_running() else 'not running'}")
    with locked_state() as entries:
        snapshot = list(entries.values())
    if not snapshot:
        print("nothing pending")
        return 0
    for e in snapshot:
        print(f"{e['session_id']}  attempts={e['attempts']}  "
              f"reset_at={e['reset_at'] or '?'}  "
              f"next={e['next_attempt_at']}  "
              f"pane={e['surface_id'] or '-'}")
    return 0


def cmd_nudge(sid):
    with locked_state() as entries:
        entry = entries.get(sid)
    if entry is None:
        print(f"no pending entry for {sid}", file=sys.stderr)
        return 1
    outcome = nudge_entry(entry)
    print(outcome)
    if outcome == "resumed":
        with locked_state() as entries:
            entries.pop(sid, None)
        return 0
    return 1


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "hook-stop-failure":
        return cmd_hook_stop_failure(sys.stdin.read())
    if cmd == "hook-stop":
        return cmd_hook_stop(sys.stdin.read())
    if cmd == "wait":
        return cmd_wait()
    if cmd == "status":
        return cmd_status()
    if cmd == "nudge" and len(rest) == 1:
        return cmd_nudge(rest[0])
    if cmd == "install":
        return cmd_install()
    if cmd == "uninstall":
        return cmd_uninstall()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
