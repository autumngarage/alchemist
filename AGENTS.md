# AGENTS.md - AI Agent Instructions for Alchemist

Alchemist is a small GitHub issue dispatcher and PR babysitter. It scans issues
labelled for agent work, triggers an external coding agent, watches for a linked
pull request, nudges the agent when checks or reviews need attention, and marks
the issue shipped or blocked.

Alchemist does not run local coding agents, does not route models, does not clone
target repositories for implementation work, and does not own a private merge
gate. Codex, Devin, GitHub checks, branch protection, and human PR review own
those parts.

## Operating Model

- Intake label: `agent-ready`.
- State labels: `alchemist-dispatched`, `alchemist-pr-open`,
  `alchemist-shipped`, `alchemist-blocked`, `alchemist-error`.
- Opt-out label: `alchemist-skip`.
- Codex dispatch is a GitHub `@codex` issue or PR comment.
- Devin dispatch is a Devin API session plus an issue comment containing the
  session id and URL.
- GitHub is the source of truth for labels, comments, PR state, checks, and
  review decisions.

Keep the app narrow. Avoid adding local worker loops, model routers, private
review systems, or repo-specific implementation behavior.

## Development Workflow

1. Never commit directly on `main` or `master`.
2. Branch first with `git checkout -b <type>/<slug>`, where `<type>` is
   `feat`, `fix`, `chore`, `refactor`, or `docs`.
3. Make the smallest coherent change.
4. Run `bash scripts/validate.sh`.
5. Stage explicit file paths and commit with a concise message.
6. Push the branch and open or update a PR with `gh pr create` or `gh pr view`.
7. Let GitHub Actions, branch protection, and PR review decide merge readiness.

Do not use generated delivery scripts or external review gates in this repo.

## Testing

```bash
bash setup.sh --deps-only
bash scripts/validate.sh
```

`scripts/validate.sh` runs:

- `uv run ruff check .`
- `uv run pytest`

## Architecture

Alchemist is a Python CLI with a cron-friendly `run-once` path:

1. `scanner.py` finds open GitHub issues by label.
2. `runner.py` manages label lifecycle, dispatch comments/API calls, PR lookup,
   follow-up nudges, and optional GitHub auto-merge queueing.
3. `auth_token.py` mints GitHub App installation tokens for unattended Railway
   ticks; PAT auth remains a local/debug fallback.
4. `config.py` resolves TOML/env config and bounded runtime knobs.
5. `locks.py` stores recoverable per-repo lock state under the configured state
   dir.

## Key Files

| File | Purpose |
| --- | --- |
| `src/alchemist/runner.py` | Main dispatcher/babysitter loop. |
| `src/alchemist/auth_token.py` | GitHub App token minting and PAT fallback. |
| `src/alchemist/config.py` | TOML/env runtime config and safety caps. |
| `src/alchemist/scanner.py` | GitHub issue discovery by label. |
| `src/alchemist/locks.py` | Per-repo lock acquisition and stale lock behavior. |
| `Dockerfile` | Railway runtime image. |
| `scripts/railway-entrypoint.sh` | Cron entrypoint. |
| `docs/operator-runbook.md` | Deployment and operational failure modes. |
| `docs/internal-autumngarage.md` | Autumn Garage production profile. |

## Review Guide

When reviewing a PR for this repo, prioritize concrete bugs over style:

1. Credential safety: GitHub tokens, App keys, Devin keys, subprocess output, and
   GitHub comments must not leak secrets.
2. GitHub state integrity: label transitions, comments, PR lookup, and retry
   semantics must be idempotent and auditable.
3. Recoverable orchestration: locks, stale dispatches, cron exit codes, and
   state-dir behavior must leave operators a safe retry path.
4. External handoff contracts: `gh`, Codex comments, and Devin API calls need
   bounded timeouts, useful diagnostics, and narrow ownership.
5. Runtime/deployment drift: Dockerfile, Railway entrypoint, release workflow,
   and package metadata must match the tested code path.
6. Tests for changed failure modes: bug fixes need focused regression tests.

Do not flag formatting, import order, speculative refactors, or naming
preferences unless they hide a real bug.
