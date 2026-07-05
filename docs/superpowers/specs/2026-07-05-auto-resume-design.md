# claude-auto-resume — design

**Date:** 2026-07-05
**Status:** awaiting user approval

## The problem

Long unattended Claude Code jobs die when the 5-hour usage window runs out.
Nobody is at the keyboard, so the session sits idle until a human comes back
and types "continue" — often hours after the limit actually reset. The wasted
time is the whole gap between reset and return.

The fix: notice the limit-hit the moment it happens, wait for the reset, and
type the continue message into the interrupted session automatically.

## Requirements (from brainstorming with Josh, 2026-07-05)

- Runs against **cmux** (manaflow-ai/cmux, the Ghostty-based macOS terminal) —
  that is where all long jobs live.
- **Every active session, automatically.** No per-job arming, no wrapper the
  user has to remember to launch through.
- **Fire once at the known reset time; up to 3 attempts total** as backup.
  After 3 failures, stop and notify.
- The nudge message must tell Claude to ignore it if the work is already done.
  Default: *"Usage limits have reset — continue where you left off. If the
  task is already complete, ignore this message."*
- Clean, well-documented repo. README and comments written plainly, in a
  human voice.

## Research findings this design rests on

Verified 2026-07-05 by research agents (sources in brackets):

1. Claude Code has **no built-in auto-resume** on limit reset; it is an open
   feature request. [github.com/anthropics/claude-code/issues/36320]
2. Claude Code fires a **`StopFailure` hook** when a turn ends on an API
   error, with matcher value `rate_limit`. Payload includes `session_id`,
   `transcript_path`, `error`, `error_details`.
   [code.claude.com/docs/en/hooks]
   ⚠ Must be re-verified on the installed CLI version before building on it
   (Phase 0 below).
3. cmux has **no limit handling** of its own (its "auto-resume" is app-relaunch
   session restore; an orchestration loop is only proposed in issue #7361),
   but exposes first-class control primitives: `cmux send`, `cmux send-key`,
   `cmux read-screen`, `cmux list-panels`, `cmux notify` over a Unix socket
   (`/tmp/cmux.sock`). [cmux.com/docs/api]
4. Processes inside cmux panes get `CMUX_WORKSPACE_ID` / `CMUX_SURFACE_ID`
   env vars, and default socket access is "cmux processes only" — our hook
   runs inside the pane's process tree, so it and anything it spawns inherit
   access. [cmux.com/docs/api]
5. cmux's Claude integration writes `~/.cmuxterm/claude-hook-sessions.json`
   mapping Claude session IDs to workspace/surface/cwd/PID.
   [cmux.com/docs/session-restore] — useful as a cross-check, not a primary
   data source (our own hook captures everything we need).
6. On a rate-limit the claude TUI process may either stay alive at the REPL
   or exit (cmux issue #2488 mentions "rate-limit exit"). The nudge must
   handle both.
7. The reset time appears in the error **text**, not a documented structured
   field. Known historical shapes: a bare epoch (`...limit reached|1719878400`)
   and prose ("Your limit will reset at 3am (America/Chicago)"). Parser must
   try several formats and must be built against real captured payloads.

## Approach chosen

**Hook-driven scheduler with cmux pane injection** (chosen over a
screen-polling daemon — brittle regex-scraping, constant wakeups — and over a
wrapper launcher, which only covers jobs it launched and so fails the
"every session automatically" requirement).

Selected while Josh was away, per his standing answers; flagged for his
confirmation at spec review.

## Architecture

Three cooperating pieces, one Python file, zero dependencies beyond the
Python 3 stdlib and the `cmux`/`claude` CLIs already on the machine.

```
                     limit hits
 claude (in cmux pane) ──► StopFailure hook ──► state.json entry
                                   │                  │
                                   └─ spawns (if not running)
                                                      ▼
                                                the waiter
                                       sleeps in 60s ticks, re-reads state
                                                      │  entry due
                                                      ▼
                                                 the nudge
                                    cmux read-screen → send text → verify
                                    via transcript growth → clear entry
                                    (or retry ≤3, then notify + give up)
```

### 1. Hooks (registered in `~/.claude/settings.json`)

**`StopFailure`, matcher `rate_limit`** — invoked with JSON on stdin. Does
three things, fast, then exits:

- Parse reset time from `error` / `error_details` text (see Parser below).
- Append an entry to the state file, under an exclusive `fcntl.flock` lock
  (several sessions can hit the limit in the same second):

  ```json
  {
    "session_id":      "…",
    "transcript_path": "…",
    "cwd":             "…",
    "surface_id":      "$CMUX_SURFACE_ID or null",
    "workspace_id":    "$CMUX_WORKSPACE_ID or null",
    "detected_at":     "ISO-8601",
    "reset_at":        "ISO-8601 or null (parse failed)",
    "attempts":        0,
    "next_attempt_at": "ISO-8601"
  }
  ```

- Spawn the waiter, detached (double-fork / `start_new_session=True`), unless
  the pidfile points at a live process. The waiter inherits the pane's env,
  which is what grants it cmux socket access.

**`Stop`** (no matcher) — a normal turn ended for some session. If a pending
entry exists for that `session_id`, the session evidently got resumed some
other way (or finished); delete the entry. This is what makes stale nudges
impossible.

### 2. The waiter

A detached process, deliberately **not** a daemon: it exists only while
entries are pending, then exits. No launchd, no cron.

Loop: sleep 60s → re-read state under lock → for each entry with
`next_attempt_at <= now`, run the nudge → if state is empty, exit. Re-reading
every tick means limit-hits that arrive mid-wait are picked up without any
signalling machinery.

Scheduling per entry:

- `reset_at` known → `next_attempt_at = reset_at + 2min` buffer.
- `reset_at` unknown (parse failure) → fallback ladder: attempts at
  detection + 1h, + 3h, + 5h (the 5-hour window guarantees the reset lands
  in that range). The 3-attempt cap and the ladder are the same mechanism.

### 3. The nudge

For one due entry:

1. `cmux list-panels` — pane gone? → mark failed, notify, drop entry.
2. `cmux read-screen --surface <id>` to decide the pane's state:
   - Claude REPL alive → `cmux send` the message, `cmux send-key enter`.
   - Shell prompt (claude exited) → send `claude --resume <session_id>`,
     wait for the REPL to appear (re-read screen, ≤60s), then send message.
3. **Verify by transcript, not by screen:** wait ~30s, then check the
   session's transcript JSONL has grown (size/mtime) since before the nudge.
   Growth = Claude is working again → delete entry, `cmux notify` success.
4. No growth, or the limit banner is back → `attempts += 1`,
   `next_attempt_at = now + 10min`. At `attempts == 3` → delete entry,
   `cmux notify` + `osascript` macOS notification: gave up, resume manually.

Screen content chooses *how* to nudge; only the transcript judges *whether it
worked*. That keeps the fragile part (TUI text matching) low-stakes.

### Parser

`parse_reset_at(error_text) -> datetime | None`. Tries, in order:

1. Unix epoch after a `|` or standing alone (10-digit int in a sane range).
2. ISO-8601 timestamp anywhere in the text.
3. Prose: `reset(s)? at <h>(:<mm>)?\s*(am|pm) (\(<tz>\))?` — resolved to the
   next future occurrence in the given (or local) timezone.

Returns `None` rather than guessing; `None` engages the fallback ladder.
Built against captured fixtures (Phase 0), which live in `tests/fixtures/`.

### CLI

One executable, `bin/claude-auto-resume`, subcommands:

| command | purpose |
|---|---|
| `hook-stop-failure` | stdin JSON → state entry + ensure waiter (wired into settings.json) |
| `hook-stop` | stdin JSON → clear entry for session |
| `wait` | the waiter loop (spawned internally; runnable by hand for debugging) |
| `status` | human-readable dump of pending entries and waiter liveness |
| `nudge <session-id>` | force a nudge now (manual override / testing) |
| `install` | merge hooks into `~/.claude/settings.json` (backup first, print diff, record the exact hook entries it added in a manifest next to the state file) |
| `uninstall` | remove exactly the entries listed in the install manifest, nothing else |

### State & logs

- State: `~/.local/state/claude-auto-resume/state.json` (+ `waiter.pid`,
  `log`). All writes under `fcntl.flock`; a corrupt state file is renamed
  aside (`state.json.corrupt-<ts>`) and replaced fresh — never crash the hook.
- Log: append-only plain text, one line per event. `status` shows the tail.

## Error handling summary

| failure | behaviour |
|---|---|
| reset time unparseable | fallback ladder +1h/+3h/+5h |
| nudge lands but limit not actually reset | transcript doesn't grow → retry ≤3 |
| pane closed / cmux quit | notify, drop entry |
| session not in cmux (`surface_id` null) | notify only — headless `claude -p --resume` can stall silently on permission prompts, worse than telling the user (v1 scope fence) |
| waiter killed (reboot/logout) | next limit-hit's hook respawns it; `status`/`nudge` allow manual recovery |
| Mac asleep | out of scope — README tells the user to `caffeinate` |
| hook crashes | hooks are wrapped in a top-level try/except that logs and exits 0 — never break the Claude session itself |

## Testing

Stdlib `unittest`, no test deps. The seams:

- **Parser**: pure function, table-driven tests over captured fixtures plus
  synthetic variants.
- **State ops**: temp-dir state file; add/clear/corruption-recovery/locking.
- **Nudge decisions**: `cmux` and `claude` are invoked via `subprocess` —
  tests prepend a directory with fake `cmux`/`osascript` scripts to `PATH`
  that record their argv and play back canned screens; transcript growth
  faked with temp files.
- **Waiter loop**: injectable clock/sleep so due-time logic runs instantly.

## Implementation phases

- **Phase 0 — recon (blocking):** verify `StopFailure` exists and fires on
  the installed Claude Code version; install a log-everything hook, capture a
  real rate-limit payload, commit it (redacted) as a fixture. The parser and
  entry schema get locked to reality here.
- **Phase 1:** parser + state ops (TDD).
- **Phase 2:** hooks + install/uninstall.
- **Phase 3:** waiter + nudge + verification.
- **Phase 4:** README, polish, end-to-end dry run with a fake cmux.

## Out of scope (v1)

- Non-cmux sessions (plain terminals, IDE panes) — notify only.
- Headless `claude -p` wrapper mode.
- Power management.
- Anything cross-platform: this is macOS + cmux, deliberately.
