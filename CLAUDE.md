# Alchemist — Claude Code Instructions

## What this repo is

Alchemist is the fifth tool in the Autumn Garage family — the **transmuter**.
It watches one GitHub org for issues labelled `alchemist-dispatch`, dispatches
each one to Conductor's agentic loop on a fresh feature branch, runs
Touchstone's review gate over the resulting diff, and opens a PR.

A human merges (or rejects). Alchemist never merges autonomously, never iterates
with the reviewer, and never speaks to an LLM directly — every model call goes
through Conductor, every quality judgment goes through Touchstone.

## Composition rules (Doctrine 0001 / 0003 / 0004)

Alchemist composes by file/CLI contract, never by code import. The Python code
in this repo is *only* orchestration. Everything load-bearing happens via
subprocess:

- `gh search issues / gh issue list / gh pr create / gh issue edit` — GitHub I/O
- `git clone / checkout / commit / push` — branch state
- `conductor exec --with <provider> --tools Read,Edit,Write,Bash --brief-file <path>` — agentic fix loop
- `<touchstone>/scripts/codex-review.sh` (invoked from the cloned repo's root) — review gate

If any external CLI is missing, alchemist fails fast with a structured error.
No fallbacks, no shims, no silent degradation.

## v0.0.x scope (what's here today)

- `alchemist scan` — list labelled issues across the configured org. No side effects.
- `alchemist doctor` — verify CLIs, auth, writable state. Exits non-zero if unhealthy.
- `alchemist banner` — print the brand surface (Doctrine 0007).
- `alchemist run-once` — placeholder. Ships in v0.1.

## v0.1 scope (next)

- `alchemist run-once` — full transmute flow with subprocess timeouts, lockfile
  state, brief rendering from versioned template, label transitions, error paths.
- Dockerfile bundling gh, git, conductor, touchstone, alchemist.
- Railway cron service + persistent volume + env vars.
- Three dogfood gates: dry-run + test label → live + test label → live + real label.

## Dogfood gates (the safety story)

The tool ships **disabled** by design. Three transitions, each manual:

1. `dry_run=true` + `dispatch_label=alchemist-test`. Cron runs, scans, even runs
   conductor on a scratch clone, but **skips** push/PR/label-mutation.
2. `dry_run=false` + `dispatch_label=alchemist-test`. Real PRs against
   intentional test issues only.
3. `dispatch_label=alchemist-dispatch`. Real users can trigger. `max_per_tick=1`
   for the first week, then lift the cap.

Never auto-graduate. Each transition is a deliberate config flip.

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
