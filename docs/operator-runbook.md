# Operator runbook

How to deploy, dogfood, and run alchemist for one GitHub org.

Alchemist's normal operating mode is remote: a Railway cron service runs
`alchemist run-once --json` on schedule. Local commands are for setup,
diagnostics, and ad-hoc verification; they are not the production daemon.

This document is the canonical reference for an operator setting up alchemist for the first time, walking through the dogfood gates, going live, and handling the common failure modes. If you're a new operator: read top-to-bottom once, then keep this open while you work the dogfood ramp.

For the Autumn Garage internal deployment profile, including the watched repo
scope and deployment blocklist, see [`docs/internal-autumngarage.md`](internal-autumngarage.md).

## Architecture (in one paragraph)

Alchemist is a Railway cron service. Every 5 minutes it queries one GitHub org for issues labelled `alchemist-dispatch`, dispatches each found issue to [Conductor](https://github.com/autumngarage/conductor)'s agentic loop on a fresh feature branch, opens a PR, then hands the PR to [Touchstone](https://github.com/autumngarage/touchstone)'s `merge-pr.sh` — which runs the AI code-review gate and squash-merges on a CLEAN verdict. BLOCKED reviews leave the PR open with comments for human triage. Alchemist signs all commits as `Alchemist <alchemist@autumngarage.dev>` and prefixes PR titles with `[alchemist]` for audit visibility.

Alchemist composes by file/CLI contract — never by code import. It owns GitHub I/O, git plumbing, per-repo locking, and label state. It does NOT own LLM judgment (Conductor) or quality review (Touchstone).

## Prerequisites

- One GitHub org you control (or a personal account; treat the same).
- A Railway account on the hobby tier or higher.
- Local `gh` CLI authenticated (used during initial setup; not required at runtime).
- Local `railway` CLI authenticated.

## First-time setup

### 1. Create the GitHub credentials

Two paths. Pick one:

**Path A — fine-grained PAT (v0.1, simpler):**
Create a fine-grained personal access token at https://github.com/settings/tokens?type=beta with these scopes on the target org's repos:
- Issues: read-write
- Pull requests: read-write
- Contents: read-write
- Metadata: read

**Path B — GitHub App (v0.2, cleaner long-term):**
Use the existing `autumn-alchemist` App registration at https://github.com/apps/autumn-alchemist (or register your own clone). Install on the target org with these permissions: `issues:rw`, `pull_requests:rw`, `contents:rw`, `metadata:r`. Capture three values:

- App ID (numeric, e.g. `3628230`) — top of the App settings page.
- Installation ID (numeric, e.g. `130170611`) — visible in the URL after installing on your org: `https://github.com/organizations/<org>/settings/installations/<installation_id>`.
- Private key (PEM contents) — generated once on the App settings page; download and stash it. You'll paste the PEM body into a Railway env var.

Each Railway deployment scopes to one installation (one org). For multi-org operators, run multiple deployments.

The runtime mints a fresh installation access token every tick (~1-hour TTL,
but we don't cache across ticks). The Railway entrypoint runs
`alchemist run-once --json`; the CLI mints and exports the token internally
before it shells out to `gh` or `git`. When App env vars are unset, alchemist
falls back to Path A's PAT for backwards compatibility.

### 2. Get an OpenRouter API key

Conductor's agentic loop needs an LLM provider. The headless container can't use OAuth-CLI providers (claude, codex), so use OpenRouter (env-var keyed, tools-capable).

Create a key at https://openrouter.ai with at least $10 in credits (unlocks the 1000-req/day pay-as-you-go tier).

### 3. Provision the Railway service

```bash
cd ~/Repos/alchemist
railway init -n alchemist
railway add --service alchemist-cron

# --- Auth: pick Path A OR Path B, not both. ---
# Path A: fine-grained PAT
railway variable set GITHUB_TOKEN="$YOUR_PAT" --service alchemist-cron

# Path B: GitHub App (recommended for production). The private-key env var
# holds the PEM file's contents — Railway accepts multi-line values.
railway variable set ALCHEMIST_APP_ID="3628230" --service alchemist-cron
railway variable set ALCHEMIST_APP_INSTALLATION_ID="130170611" --service alchemist-cron
railway variable set ALCHEMIST_APP_PRIVATE_KEY="$(cat ~/.config/alchemist/autumn-alchemist.private-key.pem)" --service alchemist-cron
# ---

railway variable set OPENROUTER_API_KEY="$YOUR_OR_KEY" --service alchemist-cron
railway variable set ALCHEMIST_ORG="$YOUR_ORG" --service alchemist-cron

# Safety defaults — override during the dogfood ramp:
railway variable set ALCHEMIST_DRY_RUN=true --service alchemist-cron
railway variable set ALCHEMIST_CONDUCTOR_EFFORT=low --service alchemist-cron
railway variable set ALCHEMIST_LABEL=alchemist-test --service alchemist-cron
railway variable set ALCHEMIST_STATE_DIR=/var/alchemist/state --service alchemist-cron
railway variable set ALCHEMIST_MAX_ISSUES_PER_TICK=1 --service alchemist-cron
```

To verify auth before deploying, run `alchemist auth-token` locally with the same env set — it prints either the minted installation token (Path B) or the PAT (Path A), and exits 1 if neither is configured.

Optional but recommended:

```bash
# Repos to skip even if labelled — comma-separated, can be bare names
# (auto-qualified with the org) or fully-qualified "owner/name".
railway variable set ALCHEMIST_REPO_BLOCKLIST="local-only-repo,sensitive-repo" --service alchemist-cron
```

Provision a persistent volume at `/var/alchemist/state` via the Railway dashboard. Without it, the per-issue lockfile and cached clones don't survive between cron firings (alchemist still works, just less efficiently).

### 4. First deploy

```bash
cd ~/Repos/alchemist
railway up --service alchemist-cron --detach
```

Watch the build:
```bash
railway logs --service alchemist-cron --build
```

Once the first cron tick fires, the runtime logs:
```bash
railway logs --service alchemist-cron --deployment
```

The first tick should show the alchemist banner + all 7 doctor checks green. If anything is red, the alchemist `--json` output will name what failed.

## Dogfood ramp

The tool ships **disabled** by design. Three gates, removed manually one at a time, never auto-graduated.

### Dogfood A — dry-run, test label

**State:** `ALCHEMIST_DRY_RUN=true`, `ALCHEMIST_LABEL=alchemist-test`. Default after first deploy.

**What you do:** file 2-3 small test issues on watched repos labelled `alchemist-test`. Examples:
- "Fix typo on line 12 of README.md: tranmute → transmute"
- "Add missing trailing newline to docs/install.md"

**What alchemist does:** scans, finds the issues, attempts the full pipeline including conductor exec — but skips every mutation (no push, no PR, no label transition). Logs prefix `[DRY-RUN]`.

**Pass criteria:** for each test issue, you see in the logs:
- `Cloning into '/var/alchemist/state/work/<repo>-<issue>'`
- `Switched to a new branch 'alchemist/issue-<N>-<slug>'`
- `[DRY-RUN] <repo>#<num>: would commit, push branch ..., open PR, and call merge-pr.sh`

If conductor declines (issue not actionable as a code change), you'll see `error: "conductor produced no diff"` — that's CORRECT behavior, the agent was right to decline. The dry-run still exercised the pipeline up to the diff check.

**Move forward when:** at least one tick produced clean dry-run output for an actionable issue.

### Dogfood B — live, test label

**Switch:** `railway variable set ALCHEMIST_DRY_RUN=false --service alchemist-cron`

**What changes:** dry-run is off. Real PRs open against real repos when conductor produces a diff. But only the **test label** is honored, so only your hand-filed test issues trigger the loop.

**Pass criteria:** at least one test issue completes the full cycle. Opening a branch and PR alone does not count; a successful Dogfood B run must end in either a merged PR or a review-blocked PR with a clear Touchstone explanation.
1. label transitions `alchemist-test → alchemist-test-working`
2. PR opens, titled `[alchemist] fix: <issue title> (#<N>)`, signed `Alchemist <alchemist@autumngarage.dev>`
3. touchstone's `merge-pr.sh` runs the AI review
4. CLEAN review → squash-merge → label transitions to `-shipped`
   OR BLOCKED review → PR stays open, label stays `-working`, comment posted
5. issue auto-closes (via the PR's `Closes` reference)

**Move forward when:** at least one issue made it to `-shipped`. Inspect the merged commit; verify the diff is what you expected.

### Live — real label

**Switch:** `railway variable set ALCHEMIST_LABEL=alchemist-dispatch --service alchemist-cron`

Optional: pre-create the `alchemist-dispatch` label set on each watched repo (alchemist auto-creates on first scan, but pre-creating avoids any first-tick weirdness):

```bash
for repo in <your-org>/<repo1> <your-org>/<repo2> ...; do
  for suffix in "" -working -shipped -declined -error; do
    gh label create "alchemist-dispatch$suffix" --repo "$repo" \
      --color ffd787 --description "Alchemist state" --force
  done
done
```

**What changes:** real users on the org can label any issue `alchemist-dispatch` to trigger the loop.

**For the first week, keep:** `max_issues_per_tick=1`, `max_per_repo_per_tick=1`, and `max_concurrent_repos=1` (the defaults). Bounds blast radius if a brief-rendering bug or prompt-injection slips through.

After a week of clean operation, lift the global cap and concurrent repos together to enable cross-repo swarm:
```bash
railway variable set ALCHEMIST_MAX_ISSUES_PER_TICK=3 --service alchemist-cron
railway variable set ALCHEMIST_MAX_CONCURRENT_REPOS=3 --service alchemist-cron
```

## Common failure modes

### "alchemist: doctor failed; skipping tick"

The doctor pre-flight failed before the tick ran. The next log line names which check failed (gh / git / conductor / touchstone CLI; auth; state-dir; merge-pr.sh path). Fix the underlying issue and the next tick recovers automatically.

### Issue stuck in `-working`

A tick crashed mid-flow (Railway restart, OOM, network blip during conductor exec). The label stayed `-working` and the issue is invisible to future scans.

**Self-heal:** every tick runs a sweep that transitions `-working` issues older than 30 minutes back to `-error`. Wait one tick after 30 min has elapsed.

**Manual override:** if you need to retry sooner, re-label the issue back to the dispatch label. The next scan picks it up.

### `merge-pr.sh` timeout, but the PR actually merged

Real bug observed during Dogfood B (alchemist#22). The merge succeeded, but the timeout fired during cleanup, alchemist reported `merged=False`. Mitigated by a post-timeout state recheck (`gh pr view --json mergedAt`) — alchemist now correctly reports `merged=True` and transitions to `-shipped` even when the subprocess timed out post-merge.

If you see this in older deploys, the fix is in PR #26.

### LLM declines as "non-actionable"

The agent reviewed the issue, judged it can't be fixed as a code change (a question, an RFC, an ambiguous ask), and exited cleanly without edits. Alchemist labels the issue `<dispatch>-declined` and posts a comment explaining.

**To retry:** sharpen the issue (link a code path, narrow scope) and re-label `<dispatch>`. The next tick picks it up fresh.

### Budget exceeded

The agent loop spent more than `default_budget` (per-issue cap, summed from conductor's `cost_usd` usage events). Alchemist refuses to push the diff, labels the issue `<dispatch>-error`, and preserves the transcript + NDJSON log for inspection.

**Action:** read the transcript at `<state_dir>/transcripts/<repo>-<issue>.{log,ndjson}`. If the cost was legitimate (genuinely hard issue), bump the budget. If the agent looped, sharpen the issue text or pin a different provider (`ALCHEMIST_PROVIDER`) before retrying.

Set `ALCHEMIST_BUDGET=""` or `"$0"` to disable enforcement entirely (not recommended in production).

### Conductor timeout on substantive issues

The deployment default is intentionally bounded. For a one-off issue that is
larger than the normal dogfood path, add an explicit body annotation before
labelling it:

```text
alchemist-timeout: 25m
```

`alchemist-conductor-timeout: 1500s` is also accepted. Values may use seconds,
minutes, or hours (`s`, `m`, `h`) and must resolve to 60-3600 seconds. Invalid
annotations fail visibly before Conductor runs, so a typo does not silently fall
back to the default.

### Touchstone review BLOCKED

Touchstone's AI review found something it couldn't ship — could be a real bug in the agent's diff, or a project-convention violation, or a missing test. The PR stays open; touchstone posts review comments.

**Triage:** read the review comments, decide if the diff is salvageable (push fixes manually) or close the PR and re-label the issue.

### GITHUB_TOKEN appears in old Railway logs

Pre-PR #39 deploys printed the PAT in git URLs that landed in Railway's log buffer. Even after PR #39 fixes the leak going forward, historical logs may still contain the token.

**Action:** rotate the PAT. For PAT path:
```bash
gh auth refresh
railway variable set GITHUB_TOKEN="$(gh auth token)" --service alchemist-cron
```

For App path: nothing to rotate — installation tokens are minted fresh each tick from the App's private key. The only static secret is the private key itself; rotate it via the App settings page if it leaks, then update `ALCHEMIST_APP_PRIVATE_KEY` on Railway.

## Watching alchemist live

```bash
cd ~/Repos/alchemist
railway logs --service alchemist-cron --deployment
```

Streams Railway's deployment logs in follow mode. You'll see each cron tick fire; closing the pane stops the stream.

In Vesper, the `/watch` skill spawns this in a separate pane: invoke via `/watch` in any Claude Code session opened in this repo.

## Tuning knobs

All overridable per-deployment via `ALCHEMIST_*` env vars on Railway:

| Env var | Default | Purpose |
|---|---|---|
| `ALCHEMIST_ORG` | `autumngarage` | The single GitHub org this deployment scopes to |
| `ALCHEMIST_LABEL` | `alchemist-test` | Dispatch label trigger |
| `ALCHEMIST_DRY_RUN` | `true` | Skip mutations (no push, PR, label transitions) |
| `ALCHEMIST_PROVIDER` | `openrouter` | Conductor provider for the agent loop |
| `ALCHEMIST_CONDUCTOR_EFFORT` | `low` | Conductor effort for unattended issue work; raise deliberately for harder queues |
| `ALCHEMIST_BUDGET` | `$2` | Per-issue cost cap (USD); `$0` or empty disables |
| `ALCHEMIST_MAX_ISSUES_PER_TICK` | `1` | Global issues mutated per tick, including stuck sweeps |
| `ALCHEMIST_MAX_PER_REPO_PER_TICK` | `1` | Issues from one repo per tick |
| `ALCHEMIST_MAX_CONCURRENT_REPOS` | `1` | Repos processed in parallel (cross-repo swarm) |
| `ALCHEMIST_CONDUCTOR_TIMEOUT_SEC` | `600` | Default hard timeout on `conductor exec`; issue body can override one run with `alchemist-timeout: 25m` |
| `ALCHEMIST_REVIEW_TIMEOUT_SEC` | `900` | Hard timeout on `merge-pr.sh` |
| `ALCHEMIST_REPO_BLOCKLIST` | `""` | Comma-separated repos to skip even when labelled |
| `ALCHEMIST_ASSIGNEE` | `@me` | GitHub user to assign to claimed issues when using PAT auth; App-auth deployments skip assignment and rely on the claim comment |
| `ALCHEMIST_STATE_DIR` | `/var/alchemist/state` | Persistent state location (Railway volume) |

## Falsification (when to wind down)

The plan commits to two falsification thresholds:

1. **Below ~5 dispatched issues per month over 6 months** — alchemist is solving a problem that doesn't exist at this scale. Defer the always-on infrastructure; polling on a laptop ad-hoc would suffice.
2. **Less than 50% of dispatched issues result in a merged PR** — alchemist's quality is below the threshold to justify the loop. The fix would be in conductor's agent quality or in brief-rendering, not in adding more autonomy.

If either threshold is hit at 6 months, wind alchemist down. Don't sunk-cost it.

## References

- [autumngarage/alchemist](https://github.com/autumngarage/alchemist) — code + this runbook
- [autumngarage/conductor](https://github.com/autumngarage/conductor) — LLM router used for the agent loop
- [autumngarage/touchstone](https://github.com/autumngarage/touchstone) — review-and-merge gate
- [autumn-garage plan](https://github.com/autumngarage/autumn-garage/blob/main/.cortex/plans/alchemist.md) — original vision document
