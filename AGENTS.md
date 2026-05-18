# AGENTS.md — AI Agent Instructions for alchemist

This file steers Codex and other AGENTS.md-native coding agents. Claude Code reads `CLAUDE.md`; Gemini CLI reads `GEMINI.md`. Keep these files aligned when project-level workflow changes.

When coding, follow the authoring guide. When explicitly reviewing a PR or running the AI review hook, use the review guide.

## Authoring Guide

### Who You Are on This Project

Alchemist is the Autumn Garage issue transmuter. It scans GitHub issues labelled
for dispatch, claims one bounded unit of work, renders a brief, delegates the
code change to Conductor, opens a PR, and hands that PR to Touchstone's
`merge-pr.sh` review-and-merge gate.

Good work in this repo is conservative automation: preserve the audit trail,
avoid leaking credentials, keep label/lock/branch state recoverable, and let
Conductor and Touchstone own the decisions they are designed to own. Alchemist
should stay a narrow orchestrator around GitHub, git, Railway cron, and the
tool handoff contracts.

<!-- touchstone:steering:start -->

<!-- This block is generated from TOUCHSTONE.md. `touchstone update` refreshes it.
     Edit content OUTSIDE the markers; touchstone will not touch project-owned content. -->

## Touchstone — Shared Agent Steering

You are an AI agent (Claude Code, Codex, or another driving CLI) working in a Touchstone-bootstrapped project. This block is the universal contract: rules that apply on every turn, plus a routing table to deeper docs you should consult when specific triggers fire. Project-specific guidance lives outside this block in your driver's steering doc (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`).

## Agent Roles And Fallbacks

- **Driving CLI** — Claude Code, Codex, or Gemini CLI. Owns file edits, git state, tests, commits, PR creation, Conductor review invocation, and merge. Drivers are interchangeable; driver fallback is shared-contract fallback — if one is unavailable, another reads the same files and continues.
- **Conductor worker/reviewer router** — the model router used by the driving CLI for review and bounded model work. Conductor can route to Claude, Codex, Gemini, Kimi, Ollama, or other providers, and provider fallback runs across configured backends, but Conductor does not replace the driver's responsibility for the branch → PR → merge-gate review → automerge workflow.

## Engineering principles (always in mind)

Non-negotiable. Every code change is reviewed against them. Full rationale lives in `principles/engineering-principles.md`.

- **No band-aids** — fix the root cause; if patching a symptom, say so explicitly and name the root cause.
- **Keep interfaces narrow** — expose the smallest stable contract; don't leak storage shape, vendor SDKs, or workflow sequencing.
- **Derive limits from domain** — thresholds and sizes come from input/config/named constants; test at small, typical, and large scales.
- **Derive, don't persist** — compute from the source of truth; persist derived state only with documented invalidation + rebuild path.
- **No silent failures** — every exception is re-raised or logged with debug context. No `except: pass`, no swallowed errors.
- **Every fix gets a test** — bug fix includes a regression test that runs in CI and fails on the old code.
- **Think in invariants** — name and assert at least one invariant for nontrivial logic.
- **One code path** — share business logic across modes; confine mode-specific differences to adapters, config, or the I/O boundary.
- **Version your data boundaries** — when a model/algorithm/source change affects decisions, version the boundary; don't aggregate across.
- **Separate behavior changes from tidying** — never mix functional changes with broad renames, formatting sweeps, or unrelated refactors.
- **Make irreversible actions recoverable** — destructive operations need dry-run, backup, idempotency, rollback, or forward-fix plan before they run.
- **Preserve compatibility at boundaries** — public API/config/schema/CLI/hook/template changes need a compatibility or migration plan.
- **Audit weak-point classes** — find a structural bug → audit the class + add a guardrail. Use the `touchstone-audit-weak-points` skill (Claude) or read `principles/audit-weak-points.md` (other drivers).
- **Isolate file-writing subagents** — parallel workers use dedicated worktrees, slice manifests, and disjoint file ownership by default.
- **File issues for bugs** — open a GitHub issue when you find a bug, in this project or in an autumngarage tool. Don't silently work around it.
- **Escalate delivery friction upstream** — if Conductor or Touchstone causes workflow drag (excessive token burn, weak parallelization, unclear delegation ergonomics, brittle merge-gate behavior, or other agent-delivery inefficiency), file an actionable upstream issue with repro steps and impact instead of normalizing the pain.

## Never commit on the default branch

Before the first edit of a tracked file in a session, run `git branch --show-current`. If it reports the default branch (`main` or `master`), branch first with `git checkout -b <type>/<slug>` where `<type>` is `feat | fix | chore | refactor | docs`. Your unstaged changes carry over — there's no cost to switching now and a real cost to discovering at commit time. Recovery steps when it happens anyway live in `principles/git-workflow.md`.

## Required Delivery Workflow

Drive this lifecycle automatically; do not ask the user for permission at each step.

1. **Pull.** `git pull --rebase` on the default branch.
2. **Branch.** Before any edit that might become a commit.
3. **Claim issues before implementation.** If the work starts from a GitHub issue, claim it before editing or dispatching an agent: `bash scripts/claim-issue.sh <n>`. Claim every issue in a multi-issue bundle so two agents do not ship competing fixes.
4. **Change + commit.** Stage explicit file paths. Concise message. One concern per commit.
5. **Reconcile issues.** Before opening the PR, list every GitHub issue found, claimed, fixed, partially fixed, or made stale by the work. Fully fixed issues get closing trailers (`Closes-issue: #123` or `Closes #123`) so merge auto-closes them; partial/stale issues get a comment explaining the evidence or remaining gap. Do not leave fixed issues open silently.
6. **Open PR + ship through the merge gate.** `bash scripts/open-pr.sh --auto-merge` pushes, opens the PR, runs the merge-gate pipeline, squash-merges, and syncs the default branch. The required expensive gates happen at merge time: deterministic checks, Conductor LLM review/fix loop, then deterministic checks again only if Conductor changed the PR head.
7. **Clean up.** Delete the local branch if it persists.

Do not bypass the PR/review/merge path with a direct default-branch push except through the documented emergency path in `principles/git-workflow.md`.

## Memory hygiene

- Treat AI-agent memory as cached guidance, not canonical truth. Verify a remembered command, flag, path, or version against this repo before relying on it.
- Don't write memory for facts that are cheap to derive from `README.md`, the steering files, `VERSION`, `bin/touchstone --help`, or the scripts.
- If memory mentions a command, flag, file path, version, or workflow, include the date (`YYYY-MM-DD`) and the canonical source checked.
- If memory conflicts with the repo, follow the repo and propose updating the stale memory.

## Routing table — read these when the trigger fires

| When you're about to... | Read |
|---|---|
| commit, branch, open a PR, run review, merge, recover from `no-commit-to-branch`, work with stacked PRs, or fan out worktrees | `principles/git-workflow.md` |
| understand the AI-authored change lifecycle, merge-gate review architecture, or where Conductor fits | `principles/ai-delivery-architecture.md` |
| start a non-trivial code change | `principles/pre-implementation-checklist.md` |
| understand the *why* of a daily-reminder rule | `principles/engineering-principles.md` |
| edit, write, or audit documentation | `principles/documentation-ownership.md` |
| coordinate parallel agents (subagents, worktrees, conductor swarm) | `principles/agent-swarms.md` |
| audit a structural bug class after fixing one instance | `principles/audit-weak-points.md` |
| hit a bug in an upstream tool (don't silently work around it) | `principles/file-upstream-bugs.md` |
| write a `.cortex/` artifact or see a Tier-1 trigger fire | `.cortex/protocol.md` |
| delegate to Conductor — pick a provider, write a brief, choose `--kind` / `--effort` | `~/.conductor/delegation-guidance.md` |

Claude Code agents: the Touchstone-bundled user-scoped skills (`touchstone-git-workflow`, `touchstone-pre-impl`, `cortex-protocol`, `conductor-delegation`, `touchstone-audit-weak-points`, `touchstone-agent-swarms`, `memory-audit`) provide the same routing surface as this table, with descriptions in your session header. Trust whichever surface fires first.

## Orientation

If `.cortex/state.md` exists in the project, read it at session start for the current state of in-flight work.

<!-- touchstone:steering:end -->

### Git Workflow

Every change starts on a feature branch. Before editing tracked files, run `git branch --show-current`; if it reports the default branch (`main` or `master`), branch first with `git checkout -b <type>/<short-description>`.

Use the normal lifecycle unless the user asks for a different flow:

1. Pull/rebase the default branch.
2. Branch before editing.
3. Make the change, stage explicit file paths, and commit with a concise message.
4. From a clean worktree, run `CODEX_REVIEW_FORCE=1 bash scripts/codex-review.sh` so Conductor can review and safely auto-fix before merge. If Conductor creates fix commits, let the loop finish; if it blocks, address findings, commit, and rerun until clean.
5. Ship with `bash scripts/open-pr.sh --auto-merge`; it creates the PR, runs the final read-only Conductor merge review, squash-merges, and syncs the default branch.
6. Clean up the feature branch if it still exists locally.

File-writing subagents use isolated worktrees by default. Follow `principles/agent-swarms.md` for slice manifests, file ownership, concurrency caps, and cleanup; use `scripts/spawn-worktree.sh` and `scripts/cleanup-worktrees.sh` for local setup and teardown.

### Testing

```bash
# Reinstall dependencies without rerunning the full machine setup
bash setup.sh --deps-only

# Before any push — uses .touchstone-config profile defaults and command overrides
bash scripts/touchstone-run.sh validate
```

Fix failing tests before pushing.

### Release & Distribution

Alchemist ships two ways:

- **CLI release:** create a GitHub Release from `main` with
  `gh release create vX.Y.Z --generate-notes`. The `release.yml` workflow bumps
  `autumngarage/homebrew-alchemist` through the shared Autumn Garage Homebrew
  workflow. Verify with `brew update && brew upgrade alchemist` or by checking
  the tap commit and formula SHA.
- **Internal runtime:** deploy the Railway cron image from this repo with
  `railway up --service alchemist-cron --detach`. Verify deployed behavior via
  `railway logs --service alchemist-cron --deployment` (cron ticks, doctor output,
  run-once results). For local env-parity in a source checkout, use
  `railway run --service alchemist-cron --no-local -- uv run alchemist doctor --json`.

Rollback for the CLI is a new release or tap formula revert. Rollback for
Railway is redeploying a previous working commit/image and restoring the prior
Railway variables. Runtime changes that affect Conductor, Touchstone, or Cortex
pins must be verified in the built image, not only in the local checkout.

After merging release-affecting changes, verify the shipped artifact or deployed environment matches the pushed code.

### Architecture

Alchemist is a Python CLI with a cron-friendly `run-once` path:

1. `scanner.py` finds dispatch-labelled issues in one GitHub org.
2. `runner.py` enforces per-repo serialization, transitions labels, clones or
   updates the target repo, creates the branch, writes the brief/transcript,
   calls `conductor exec`, commits and pushes changes, opens the PR, then invokes
   Touchstone's `merge-pr.sh`.
3. `briefs.py` loads package templates and renders the Conductor brief plus PR
   body. Issue content is untrusted input and must remain fenced as such.
4. `auth_token.py` mints GitHub App installation tokens for unattended Railway
   ticks; PAT auth remains a local/debug fallback.
5. `config.py` resolves TOML/env config and supplies the bounded runtime knobs.
6. `locks.py` stores recoverable lock state under the configured state dir.

Alchemist never imports Conductor, Touchstone, or Cortex as libraries. The
contract is subprocess + files + exit codes. Mode-specific behavior belongs at
the I/O boundary (`dry_run`, provider selection, Railway entrypoint), not in
parallel business logic.

### Key Files

| File | Purpose |
|------|---------|
| `src/alchemist/runner.py` | Main transmute loop, label lifecycle, git/PR operations, Conductor and Touchstone handoffs. |
| `src/alchemist/auth_token.py` | GitHub App token minting and PAT fallback. Treat as security-sensitive. |
| `src/alchemist/config.py` | TOML/env runtime config and safety caps. |
| `src/alchemist/briefs.py` | Runtime template loading and prompt/PR body rendering. |
| `src/alchemist/scanner.py` | GitHub issue discovery for dispatch labels. |
| `src/alchemist/locks.py` | Per-repo/issue lock acquisition and stale lock behavior. |
| `src/alchemist/reporter.py` | Structured reporting of tool failures. |
| `Dockerfile` | Railway runtime image, pinned Conductor/Touchstone versions, bundled CLIs. |
| `scripts/railway-entrypoint.sh` | Cron entrypoint and runtime token export. |
| `scripts/open-pr.sh`, `scripts/merge-pr.sh`, `scripts/codex-review.sh` | Local PR/review/merge workflow copied from Touchstone. |
| `docs/operator-runbook.md` | Deployment, dogfood gates, and operational failure modes. |
| `docs/internal-autumngarage.md` | Autumn Garage production profile, watched repos, Railway variables. |

### State & Config

Config is loaded from `$ALCHEMIST_CONFIG`, `~/.alchemist/config.toml`, or
`/etc/alchemist/config.toml` in the Railway image, with `ALCHEMIST_*` env vars
overriding file values. Internal Railway defaults live in
`docs/internal-autumngarage.md`; the public template lives in `README.md` and
`docs/operator-runbook.md`.

Mutable runtime state lives under `ALCHEMIST_STATE_DIR` (Railway:
`/var/alchemist/state`) and includes cloned worktrees, lock files, rendered
briefs, Conductor transcripts, and NDJSON usage logs. That directory must be on
a Railway volume for efficient retries, but error comments must not depend on
operators having post-cron shell access to it.

Gitignored/generated artifacts include build outputs (`dist/`, `build/`),
virtualenv/cache directories, generated `_version.py`, and Cortex indexes under
`.cortex/.index*` / `.cortex/pending/`.

### Hard-Won Lessons

- Git credentials leaked through URL-based auth in git stdout/stderr and stored
  remotes. Root cause: embedding tokens in clone/push URLs. Guard: use
  `http.extraheader` auth and sanitize every subprocess error before logging or
  commenting.
- Railway marked handled issue failures as crashed deployments. Root cause:
  issue-level tool errors returned process exit 1 even after Alchemist had
  labelled/commented/reported the issue. Guard: distinguish run-level failures
  from per-issue handled failures in the CLI exit contract.
- Error comments pointed only at `/var/alchemist/state/transcripts/*.log`, which
  is not reachable after a one-shot cron exits. Root cause: diagnostics lived
  only on the Railway volume. Guard: include a bounded, sanitized transcript
  tail in issue error comments for logs under the transcript directory.
- Conductor fixes do not reach the Railway runtime until the Dockerfile pin is
  bumped and the service is redeployed. Root cause: assuming a source merge in a
  sibling repo updates the pinned image. Guard: verify runtime pins and deployed
  image when a fix depends on a sibling tool release.
- Wheel builds duplicated template package entries when templates were both
  package data and force-included. Root cause: redundant Hatch wheel config.
  Guard: check `unzip -Z1 dist/*.whl | sort | uniq -d` when packaging data paths
  change.

---

## Review Guide

You are reviewing pull requests for **alchemist**. Optimize your review for catching the things that bite this repo, not generic style polish.

### What to prioritize (in order)

1. **Credential safety.** GitHub tokens, App private keys, OpenRouter keys, git
   auth headers, subprocess output, Railway logs, and issue comments must not
   expose secrets.
2. **GitHub state integrity.** Label transitions, assignee/comment side effects,
   issue closure, PR creation, branch naming, force-with-lease behavior, and
   retry semantics must be idempotent and auditable.
3. **Recoverable orchestration.** Locks, stale sweeps, per-issue failure paths,
   cron exit codes, and Railway volume paths must leave operators a safe retry
   path and enough context to debug.
4. **Tool handoff contracts.** Conductor, Touchstone, Cortex, `gh`, and `git`
   subprocess calls must have explicit timeouts, captured diagnostics, stable
   working directories, and narrow ownership boundaries.
5. **Runtime/deployment drift.** Dockerfile pins, Railway entrypoint behavior,
   Homebrew release workflow, package data, and generated versions must match
   the code path being tested.
6. **Tests for changed failure modes.** Bug fixes need focused regression tests;
   broad orchestration changes need CLI/runner tests that cover dry-run and live
   mutation boundaries.

Style nits, formatting, and theoretical refactors are **out of scope** unless they hide a bug. Do not flag them.

---

### Specific review rules

#### High-scrutiny paths

Files and directories:

- `src/alchemist/runner.py`
- `src/alchemist/auth_token.py`
- `src/alchemist/config.py`
- `src/alchemist/scanner.py`
- `src/alchemist/locks.py`
- `src/alchemist/briefs.py`
- `Dockerfile`
- `scripts/railway-entrypoint.sh`
- `scripts/open-pr.sh`, `scripts/merge-pr.sh`, `scripts/codex-review.sh`
- `.github/workflows/release.yml`
- `pyproject.toml`
- `src/alchemist/templates/`
- `docs/operator-runbook.md`
- `docs/internal-autumngarage.md`

Flag any of the following:

- Token-bearing URLs, auth headers, env dumps, subprocess stderr/stdout, or
  transcript tails that can reach logs or GitHub comments without sanitization.
- New label transitions that can strand an issue in `-working`, skip
  `-error`/`-declined` visibility, or retry a shipped issue without an explicit
  operator action.
- Git pushes without force-with-lease when updating an existing Alchemist branch,
  or force pushes that do not first resolve the remote branch OID.
- New conductor/touchstone/cortex calls that bypass the existing subprocess
  wrappers, omit timeouts, change cwd incorrectly, or swallow non-zero exits.
- Cron entrypoint changes that make handled per-issue errors fail the Railway
  deployment, or make run-level auth/config failures look successful.
- Config/schema/env changes without README/runbook/internal profile updates.
- Template/package-data changes without built-wheel or template-loading
  verification.
- Changes to state-dir layout without migration or compatibility for existing
  Railway volumes.

#### Silent failures

Flag any of the following:

- New `except: pass`, `except Exception: pass`, or `except: ...` without logging.
- New `try / except` that catches a broad exception and continues without logging the exception object.
- Default values returned on error without a log line.
- Fallback behavior that masks broken state.

The rule: every exception is either re-raised or logged with enough context to debug from production logs alone.

#### Tests

- Bug fixes must include a test that reproduces the original failure mode.
- Tests should use relative values (percentages, ratios) not absolute values where applicable.
- Integration tests should hit real infrastructure for critical paths (mocks have masked real bugs in the past).

---

### What NOT to flag

- Formatting, whitespace, import order — pre-commit hooks handle these.
- Type annotations on existing untyped code.
- "You could refactor this for clarity" — only if the unclarity hides a bug.
- Missing docstrings on small private functions.
- Speculative future-proofing — don't suggest abstractions for hypothetical future requirements.
- Naming preferences absent a clear convention violation.

If you find yourself writing "consider" or "you might want to" without a concrete bug or risk attached, delete the comment.

---

### Output format

1. **Summary** — one paragraph: what this PR does and your overall verdict (approve / request changes / comment).
2. **Blocking issues** — bugs or risks that must be fixed before merge. Each item: file:line, what's wrong, why it matters, suggested fix.
3. **Non-blocking observations** — things worth noting but not blocking. Keep this section short.
4. **Tests** — does this PR add tests for the changed behavior? If not, is that OK?

If there are zero blocking issues, the review is just: "LGTM."
