# Alchemist — Claude Code Instructions

## What this repo is

Alchemist is the fifth tool in the Autumn Garage family — the **transmuter**.
A Railway cron polls one GitHub org every 5 minutes for any open issue alchemist
has not already touched, dispatches it to Conductor's agentic loop on a fresh
feature branch, opens a PR, and hands it to Touchstone's `merge-pr.sh` which
runs the AI code-review gate and squash-merges on a CLEAN verdict.

The full data flow:

```
cron tick (every 5 min)
  → alchemist scans the autumn-garage org for any open issue without
    one of its own state labels (-working/-blocked/-shipped/-declined/-error)
    or the manual opt-out label (alchemist-skip)
  → conductor agent evaluates scope (read code / web search / just go)
  → conductor agent does the work (edits in a per-issue working dir)
  → alchemist commits + pushes (signed as `Alchemist <alchemist@autumngarage.dev>`)
  → alchemist opens PR (title prefixed `[alchemist]` for audit visibility)
  → touchstone merge-pr.sh runs review + squash-merges if CLEAN
  → BLOCKED reviews leave the PR open; alchemist marks the issue
    `alchemist-blocked` for human triage
```

Alchemist never speaks to an LLM directly (every model call goes through
Conductor) and never makes a quality judgment (every review goes through
Touchstone's `merge-pr.sh`). It is purely the orchestrator of those two
peers plus the GitHub I/O around them.

## Composition rules (Doctrine 0001 / 0003 / 0004)

Alchemist composes by file/CLI contract, never by code import. The Python code
in this repo is *only* orchestration. Everything load-bearing happens via
subprocess:

- `gh search issues / gh issue edit / gh pr create / gh repo view` — GitHub I/O
- `git clone / checkout / commit / push` — branch state
- `conductor exec --with <provider> --tools Read,Edit,Write,Bash --brief-file <path>` — agentic fix loop
- `bash <touchstone>/scripts/merge-pr.sh <pr-number>` (from the cloned repo's root) — review-and-merge gate

Alchemist does NOT run `touchstone codex-review.sh` directly. `merge-pr.sh`
calls it internally as the merge gate; the result is a clean review-and-merge
in one step.

If any external CLI is missing, alchemist fails fast with a structured error.
No fallbacks, no shims, no silent degradation.

## Current runtime surface

- `alchemist scan` — list open issues across the configured org. No side effects.
- `alchemist doctor` — verify CLIs, auth, writable state, and runtime config.
  Exits non-zero when the tick should not run.
- `alchemist banner` — print the brand surface.
- `alchemist auth-token` — mint a GitHub App installation token for the cron
  entrypoint, or echo the PAT fallback when App vars are absent.
- `alchemist run-once` — full transmute flow with subprocess timeouts, lockfile
  state, brief rendering from versioned templates, label transitions, error
  comments, PR creation, and Touchstone merge-gate delegation.
- Dockerfile bundling gh, git, conductor, touchstone, alchemist.
- Railway cron service + persistent volume + env vars.

## State labels (the ownership signal)

Alchemist marks every issue it touches with one of five state labels. They
are alchemist's own bookkeeping — scanning filters out any issue that already
carries one, so alchemist never re-processes its own outcomes:

- `alchemist-working` — actively being worked this tick.
- `alchemist-blocked` — PR opened, Touchstone returned BLOCKED, awaiting
  human triage.
- `alchemist-shipped` — PR merged.
- `alchemist-declined` — agent reviewed and judged the issue non-actionable.
- `alchemist-error` — tooling failure mid-cycle.

To opt an issue out entirely, add the `alchemist-skip` label or close it.
To force a retry, remove the relevant state label. The `ALCHEMIST_LABEL`
env var is deprecated; alchemist no longer gates on a dispatch label.

## Project conventions

- Python 3.11+, hatch-vcs versioning, uv for env management, ruff for lint.
- Brew tap: `autumngarage/homebrew-alchemist`. Shared `homebrew-bump.yml` workflow
  in autumn-garage handles the formula bump on release.
- Independent release cadence (Doctrine 0001 — each tool ships on its own clock).

## Working in this repo

- Tests live under `tests/`. `uv run pytest -q` is the standard invocation.
- `uv run ruff check src tests` for lint.
- `uv run alchemist <cmd>` exercises the CLI from the local checkout.
- Don't introduce a runtime dep on `figlet` — the wordmark is embedded as a
  string literal in `banner.py`.

## Sister repositories

- `autumngarage/touchstone` — review gate provider.
- `autumngarage/conductor` — LLM router used for the agentic loop.
- `autumngarage/cortex` — optional journal author.
- `autumngarage/sentinel` — sister tool (proactive backlog worker; opposite
  posture from alchemist's reactive single-shot).
- `autumngarage/autumn-garage` — meta repo holding the shared workflow + plans.

<!-- conductor:begin v0.10.33 -->
@~/.conductor/delegation-guidance.md
<!-- conductor:end -->
