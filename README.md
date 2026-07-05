# auto-message-claude

Long Claude Code jobs die when your usage limit runs out, and then they sit
there until you come back and type "continue". If the limit reset at 6pm and
you got back at 9, you lost three hours for no reason.

This tool closes that gap. When a session inside [cmux](https://cmux.com)
hits a usage limit, a Claude Code hook records which pane it was in and when
the limit resets. A small background process waits out the reset, then types
into that pane:

> Usage limits have reset — continue where you left off. If the task is
> already complete, ignore this message.

If the session picks the message up and starts working, done. If not, it
retries — at most three attempts, ten minutes apart — then sends you a
notification and stops. It never loops forever and it never touches a
session that finished on its own (a second hook clears those).

## What you need

- macOS, with sessions running inside [cmux](https://github.com/manaflow-ai/cmux)
- Claude Code recent enough to have the `StopFailure` hook event
- Python 3.9+ (the system one is fine; there are no dependencies)

## Install

```
git clone https://github.com/joshuatrobertson/auto-message-claude
cd auto-message-claude
./bin/claude-auto-resume install
```

`install` adds two hooks to `~/.claude/settings.json` (it backs the file up
first and prints exactly what it added). `./bin/claude-auto-resume uninstall`
removes exactly those entries and nothing else.

## Commands

```
claude-auto-resume status       what's pending, and is the waiter alive
claude-auto-resume nudge <sid>  push one session right now, see the outcome
claude-auto-resume wait         run the waiter in the foreground to watch it
```

State, logs and captured hook payloads live in
`~/.local/state/claude-auto-resume/`. The log is plain text; read it when
anything surprises you.

## Verifying it on your machine

The two things this tool leans on that Anthropic and cmux don't guarantee:

1. **The hook payload format.** Every rate-limit event's raw payload is
   archived to `~/.local/state/claude-auto-resume/captures/`. After your
   first real limit hit, look at the capture. If `status` shows
   `reset_at=?`, the parser didn't understand the new format — file an
   issue with the (redacted) capture, or fix `parse_reset_at` and add the
   capture to `tests/`.
2. **cmux socket access from a detached process.** The waiter inherits its
   pane's environment, which is what lets it talk to cmux hours later. If
   `status` shows the waiter running but nudges land nowhere (check the
   log), launch cmux with `CMUX_SOCKET_MODE=allowAll` and see the cmux docs
   on socket access modes.

Also worth knowing: `REPL_MARKERS` at the top of `autoresume.py` is the list
of strings that identify a live Claude prompt on screen. Claude Code's TUI
chrome changes now and then; if the tool starts relaunching sessions that
were alive, that list needs a refresh from a real `cmux read-screen` capture.

## Caveats, honestly

- Your Mac has to be awake at reset time. This tool doesn't fight power
  management — run `caffeinate` (or equivalent) for overnight jobs.
- Sessions outside cmux get a notification instead of a resume. Typing into
  arbitrary terminals isn't something I'm willing to guess at, and headless
  `claude -p --resume` can stall silently on permission prompts.
- A job that legitimately needs four usage windows gets resumed four times.
  That's the point — but you'll get a notification each time, so a runaway
  job won't burn windows quietly.
- The reset-time formats and TUI markers are reverse-engineered, not
  documented. Expect the odd touch-up after Claude Code updates; the
  captures directory exists to make those touch-ups five-minute jobs.

## How it works, in one paragraph

`StopFailure` hook (matcher `rate_limit`) → entry in a flock-guarded JSON
state file + raw payload archived → detached waiter (flock-elected
singleton, exits when idle) sleeps in 60s ticks → at reset time it reads
the pane over the cmux socket, types the message (relaunching
`claude --resume <id>` first if the process exited), then confirms success
by watching for a new assistant message in the session's transcript JSONL —
the only evidence that actually proves the session is working again.
