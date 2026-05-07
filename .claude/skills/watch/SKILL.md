---
name: watch
description: Stream live logs from the alchemist Railway cron service so the operator can see what alchemist is doing right now. Use when the user says "watch alchemist", "tail alchemist", "see what alchemist is doing", "monitor the cron", or "show me the live logs". Falls back from a split-pane Vesper window to the current shell when Vesper isn't available.
---

Stream Railway's deployment logs for `alchemist-cron` in follow mode so the operator can watch cron ticks in real time. Each tick fires every 5 minutes; logs include doctor checks, scan results, conductor exec progress, push/PR/merge events, and the JSON `RunResult` per dispatched issue.

## When to invoke

Direct triggers (user says any of these):

- "watch alchemist" / "tail alchemist" / "monitor alchemist"
- "show me the live logs"
- "what is alchemist doing right now"
- "see if the next tick fires"

Indirect triggers:

- User filed a labelled issue and wants to verify it's picked up at the next tick
- User just deployed a change and wants to watch the next container start

## When NOT to invoke

- Historical log inspection — use `railway logs --service alchemist-cron --lines N` directly, or the Railway dashboard.
- Build logs (different stream) — use `railway logs --service alchemist-cron --build`.
- One-off "did it run today?" — `railway service status` is faster.

## Behavior

The runtime command is:

```bash
railway logs --service alchemist-cron --deployment
```

Without `--lines` / `--since` / `--until`, the Railway CLI streams in follow mode. The stream stays open until the operator hits Ctrl-C.

### If Vesper is running

`$VESPER_SOCKET` is set AND a `vesper` CLI is reachable on `$PATH`:

```bash
vesper split --vertical --command "cd ~/Repos/alchemist && railway logs --service alchemist-cron --deployment"
```

This splits a vertical pane alongside the operator's current shell. They keep working in their original pane while the watcher streams in the new one. Closing the pane stops the stream.

### If Vesper isn't usable

`$VESPER_SOCKET` unset OR no `vesper` CLI on `$PATH`:

Print this guidance to the operator and let them paste it into a fresh terminal:

```
Open a new terminal pane/tab and run:

  cd ~/Repos/alchemist && railway logs --service alchemist-cron --deployment
```

Don't run the streaming command in the current shell — it would block the conversation. The operator launching it themselves keeps the agent loop free.

## Pre-flight checks

Before invoking, verify:

1. `railway whoami` succeeds (operator authenticated).
2. `railway status` shows a linked project. If not, the operator needs to `cd ~/Repos/alchemist && railway link` first — surface that error rather than blindly running the logs command.
3. The service `alchemist-cron` exists. If `railway service` lists no service or a different name, fall back to `railway service list` to show the operator their options.

## What the operator will see

A typical successful tick (synthesized from real Railway output):

```
Starting Container
         _      _                    _     _
    __ _| | ___| |__   ___ _ __ ___ (_)___| |_
   / _` | |/ __| '_ \ / _ \ '_ ` _ \| / __| __|
  ...
  ✓ gh, git, conductor, touchstone — all green
  org: autumngarage  ·  label: alchemist-test
  ✓ doctor passed
Cloning into '/var/alchemist/state/work/autumngarage-touchstone-N'...
Switched to branch 'alchemist/issue-N-...'
[branch hash] alchemist: <issue title>
remote: ...
[
  {
    "repo": "autumngarage/touchstone",
    "issue_number": N,
    "pr_url": "https://github.com/.../pull/M",
    "merged": true,
    "error": null,
    "elapsed_sec": 89.2,
    "dry_run": false
  }
]
```

A no-work tick is typically one line: `(no work this tick)`.

## Related

- `alchemist#30` — v0.2 enhancement: an `alchemist watch` subcommand that wraps this with friendlier per-tick formatting (color-coded states, collapsed JSON). When that ships, this skill should swap to `alchemist watch` instead of raw `railway logs`.
- `alchemist#8` — operator runbook covering the dogfood-A → dogfood-B → live progression.
