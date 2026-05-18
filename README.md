# Alchemist

Issue-driven transmuter for the [Autumn Garage](https://github.com/autumngarage) family.

A user files a GitHub issue and labels it `alchemist-dispatch`. Within ~5 minutes, Alchemist
sees the label, dispatches the issue to [Conductor](https://github.com/autumngarage/conductor)'s
agentic loop on a fresh feature branch, opens a PR, and hands it to
[Touchstone](https://github.com/autumngarage/touchstone)'s `merge-pr.sh` which runs the AI
code-review gate and squash-merges on a CLEAN verdict. BLOCKED reviews leave the PR open
with comments for human triage.

Alchemist never speaks to an LLM directly (every model call goes through Conductor) and
never makes a quality judgment (Touchstone owns review-and-merge). It is purely the
orchestrator: GitHub I/O, git plumbing, and per-repo locking around its two peers.

Every commit and PR is signed `Alchemist <alchemist@autumngarage.dev>` with a `[alchemist]`
title prefix so the audit trail in `git log` and PR lists is unambiguous.

## Status

Alchemist runs as a remote Railway cron service, not as a long-lived local
daemon. The local CLI is for setup, diagnostics, and one-off dry runs; the
production path is Railway invoking `alchemist run-once --json` on its cron
schedule.

Use local `doctor` to check this checkout:

```bash
uv run alchemist doctor
```

Use Railway to check the deployed runtime:

```bash
railway run --service alchemist-cron -- alchemist doctor --json
railway logs --service alchemist-cron --deployment
```

For deployment and dogfood guidance, use the operator runbook:
[`docs/operator-runbook.md`](docs/operator-runbook.md).

## How it composes

| Tool | Role |
|---|---|
| **Touchstone** | review gate (invoked headless via `scripts/merge-pr.sh`) |
| **Cortex** | optional journal author for each transmute cycle (target repo's `.cortex/journal/`) |
| **Conductor** | LLM router for the agentic fix loop (`conductor exec --tools ...`) |
| **Alchemist** | the orchestrator — GitHub I/O, branch state, lockfile, label transitions |

Alchemist composes by file/CLI contract, never by code import (Doctrine 0001/0003/0004).

## Install

```bash
brew install autumngarage/alchemist/alchemist
```

Or from source:

```bash
git clone https://github.com/autumngarage/alchemist.git
cd alchemist
uv sync
uv run alchemist doctor
```

## Configure

A single TOML config file at `$ALCHEMIST_CONFIG` (defaults to `~/.alchemist/config.toml`,
or `/etc/alchemist/config.toml` for the Railway deploy):

```toml
[alchemist]
org = "autumngarage"
poll_interval_minutes = 5
default_budget = "$2"
default_provider = "openrouter"
conductor_effort = "medium"       # production default; override per deployment as needed
dispatch_label = "alchemist-test"  # flip to "alchemist-dispatch" after dogfood
dry_run = true                     # flip to false after dogfood A
state_dir = "/var/alchemist/state"
max_issues_per_tick = 1            # global blast-radius cap for the first week
max_per_repo_per_tick = 1
max_concurrent_repos = 1
```

`default_budget` caps conductor spend per dispatched issue. After conductor
exec finishes, alchemist sums `cost_usd` from the structured NDJSON event log
and bails (`error: "budget-exceeded: $X spent vs $Y budgeted"`) if the run
exceeded it — the issue lands in `-error` for human triage. Set to `"$0"` or
empty to disable enforcement.

For unusually substantive issues, operators can opt that one issue into a
longer Conductor run by adding an annotation to the issue body:

```text
alchemist-timeout: 25m
```

`alchemist-conductor-timeout: 1500s` is accepted as an equivalent explicit
form. Overrides are bounded to 60-3600 seconds so a single issue cannot silently
turn into an unbounded provider spend.

Env vars override config: `ALCHEMIST_ORG`, `ALCHEMIST_LABEL`, `ALCHEMIST_DRY_RUN`,
`ALCHEMIST_PROVIDER`, `ALCHEMIST_BUDGET`, `ALCHEMIST_STATE_DIR`,
`ALCHEMIST_MAX_ISSUES_PER_TICK`, `ALCHEMIST_MAX_PER_REPO_PER_TICK`,
`ALCHEMIST_MAX_CONCURRENT_REPOS`, `ALCHEMIST_CONDUCTOR_EFFORT`.

Required externally:
- GitHub auth — either `GITHUB_TOKEN` with `issues:rw`, `pull_requests:rw`,
  `contents:rw`, or GitHub App env vars (`ALCHEMIST_APP_ID`,
  `ALCHEMIST_APP_INSTALLATION_ID`, `ALCHEMIST_APP_PRIVATE_KEY`) so Alchemist can
  mint an installation token per tick.
- `OPENROUTER_API_KEY` — for Conductor's default `openrouter` provider when
  running headless.

## Going live (the dogfood gates)

Three transitions, each manual. The tool never auto-graduates.

1. **Dogfood A** — `dry_run=true`, label `alchemist-test`. File a tiny test issue.
   Verify cron tick + log output. No side effects.
2. **Dogfood B** — `dry_run=false`, label still `alchemist-test`. File one tiny
   test issue. Real PR opens. Inspect and merge or reject.
3. **Live** — flip `dispatch_label` to `alchemist-dispatch`. Keep
   `max_issues_per_tick=1` and `max_concurrent_repos=1` for one week, then lift
   them deliberately to enable cross-repo swarm.

Full operator walkthrough including Railway provisioning, common failure modes,
and tuning knobs lives at [`docs/operator-runbook.md`](docs/operator-runbook.md).

## Falsification

If, six months after v0.1 ships, fewer than half of dispatched issues result in a
merged Alchemist PR, OR if dispatch volume stays under ~5 issues per month across
the watched repos, Alchemist failed to be useful and should be wound down. The fix
would be in Conductor's agentic loop or in brief-rendering, not in adding more
autonomy to Alchemist.

## License

MIT — see `LICENSE`.
