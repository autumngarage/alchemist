# Operator Runbook

How to deploy, dogfood, and run Alchemist for one GitHub org.

Alchemist's normal operating mode is remote: a Railway cron service runs
`alchemist run-once --json` on schedule. Local commands are for setup,
diagnostics, and ad-hoc verification.

For the Autumn Garage internal deployment profile, see
[`docs/internal-autumngarage.md`](internal-autumngarage.md).

## Architecture

Alchemist is a Railway cron service that coordinates GitHub issue and PR state.
It scans issues labelled with an explicit intake label, triggers Codex or Devin,
then watches for a pull request that references the issue. It does not clone
target repositories, execute a local coding agent, commit changes, or open the
implementation PR itself.

The state machine is:

```text
agent-ready
  -> alchemist-dispatched
  -> alchemist-pr-open
  -> alchemist-shipped | alchemist-blocked | alchemist-error
```

## Prerequisites

- One GitHub org you control.
- A Railway account.
- Local `gh` CLI authenticated for setup.
- Local `railway` CLI authenticated.
- Codex cloud/GitHub integration enabled, or Devin API credentials.

## First-Time Setup

### 1. Create GitHub Credentials

Two paths are supported.

Path A: fine-grained PAT with:

- Issues: read-write
- Pull requests: read-write
- Metadata: read

Path B: GitHub App with:

- Issues: read-write
- Pull requests: read-write
- Metadata: read

Set either `GITHUB_TOKEN` for Path A or the App variables for Path B:

```bash
railway variable set ALCHEMIST_APP_ID="3628230" --service alchemist-cron
railway variable set ALCHEMIST_APP_INSTALLATION_ID="130170611" --service alchemist-cron
railway variable set ALCHEMIST_APP_PRIVATE_KEY="$(cat ~/.config/alchemist/app.private-key.pem)" --service alchemist-cron
```

### 2. Configure Agent Provider

Codex path:

```bash
railway variable set ALCHEMIST_AGENT_PROVIDER=codex --service alchemist-cron
```

Codex must be connected to the target repositories and able to respond to
`@codex` comments.

Devin path:

```bash
railway variable set ALCHEMIST_AGENT_PROVIDER=devin --service alchemist-cron
railway variable set DEVIN_API_KEY="$DEVIN_API_KEY" --service alchemist-cron
railway variable set ALCHEMIST_DEVIN_ORG_ID="$DEVIN_ORG_ID" --service alchemist-cron
```

Devin must also have GitHub repository access so it can open PRs.

### 3. Configure Runtime

```bash
railway variable set ALCHEMIST_ORG="$YOUR_ORG" --service alchemist-cron
railway variable set ALCHEMIST_INTAKE_LABEL=agent-ready --service alchemist-cron
railway variable set ALCHEMIST_STATE_LABEL_PREFIX=alchemist --service alchemist-cron
railway variable set ALCHEMIST_DRY_RUN=true --service alchemist-cron
railway variable set ALCHEMIST_STATE_DIR=/var/alchemist/state --service alchemist-cron
railway variable set ALCHEMIST_MAX_ISSUES_PER_TICK=1 --service alchemist-cron
railway variable set ALCHEMIST_MAX_PER_REPO_PER_TICK=1 --service alchemist-cron
railway variable set ALCHEMIST_MAX_CONCURRENT_REPOS=1 --service alchemist-cron
railway variable set ALCHEMIST_AGENT_STALE_AFTER_HOURS=24 --service alchemist-cron
railway variable set ALCHEMIST_AUTO_MERGE=false --service alchemist-cron
```

Optional blocklist:

```bash
railway variable set ALCHEMIST_REPO_BLOCKLIST="sensitive-repo,homebrew-tap" --service alchemist-cron
```

Provision a persistent volume at `/var/alchemist/state`. The state dir mainly
holds per-repo locks.

### 4. Deploy

```bash
railway up --service alchemist-cron --detach
railway logs --service alchemist-cron --deployment
```

Verify locally with Railway vars:

```bash
railway run --service alchemist-cron --no-local -- uv run alchemist doctor --json
```

## Dogfood Ramp

### Dogfood A - Dry Run

Keep `ALCHEMIST_DRY_RUN=true`.

Add `agent-ready` to one tiny issue. A healthy dry run logs that it would
dispatch the issue but does not mutate labels or comments.

### Dogfood B - Live Dispatch

Set:

```bash
railway variable set ALCHEMIST_DRY_RUN=false --service alchemist-cron
```

Pass criteria:

1. Issue label moves from `agent-ready` to `alchemist-dispatched`.
2. Alchemist posts a Codex trigger comment or a Devin session URL.
3. The external agent opens a PR with `Closes #N`.
4. Alchemist finds that PR and marks the issue `alchemist-pr-open`.
5. The PR either merges and the issue becomes `alchemist-shipped`, or stalls
   clearly as `alchemist-blocked`.

Keep all caps at `1` until the flow is boring.

## Common Failure Modes

### Doctor Failed

The tick stops before touching issues. The log line names the failed check:
GitHub auth, provider config, or state dir.

### Dispatched But No PR

If no linked PR appears before `ALCHEMIST_AGENT_STALE_AFTER_HOURS`, Alchemist
marks the issue `alchemist-blocked`. Verify that the external agent was enabled
for the repo and that the issue was actionable.

### PR Exists But Was Not Detected

Alchemist looks for PRs whose title or body references `#N` or the issue URL.
Make sure the agent PR body includes `Closes #N`.

### Failed Checks Or Requested Changes

Alchemist posts one follow-up nudge per reason:

- `checks-failed`
- `changes-requested`

It does not repeat the same nudge forever.

### Devin Ignores PR Comments

If Alchemist cannot find the original Devin session ID, it falls back to an
`@devin` PR comment. Devin organizations may ignore bot comments by default.
Prefer API-backed dispatch so Alchemist can send follow-ups to the session.

### Auto-Merge Did Not Merge

`auto_merge=true` queues GitHub auto-merge. Branch protection, missing checks,
draft PRs, or requested changes can still keep the PR open.

## Tuning Knobs

| Env var | Default | Purpose |
|---|---|---|
| `ALCHEMIST_ORG` | `autumngarage` | GitHub org to scan |
| `ALCHEMIST_INTAKE_LABEL` | `agent-ready` | Explicit issue intake queue |
| `ALCHEMIST_STATE_LABEL_PREFIX` | `alchemist` | Prefix for state labels |
| `ALCHEMIST_AGENT_PROVIDER` | `codex` | `codex` or `devin` |
| `ALCHEMIST_DRY_RUN` | `true` | Skip mutations |
| `ALCHEMIST_MAX_ISSUES_PER_TICK` | `1` | Global issues processed per tick |
| `ALCHEMIST_MAX_PER_REPO_PER_TICK` | `1` | Issues per repo per tick |
| `ALCHEMIST_MAX_CONCURRENT_REPOS` | `1` | Repos processed in parallel |
| `ALCHEMIST_AGENT_STALE_AFTER_HOURS` | `24` | Time before no-PR dispatch blocks |
| `ALCHEMIST_AUTO_MERGE` | `false` | Queue GitHub auto-merge for ready PRs |
| `ALCHEMIST_REPO_BLOCKLIST` | `""` | Comma-separated repos to skip |
| `ALCHEMIST_ASSIGNEE` | `@me` | Optional human assignee for PAT deployments |
| `ALCHEMIST_STATE_DIR` | `/var/alchemist/state` | Persistent lock directory |
| `DEVIN_API_KEY` | unset | Required for Devin dispatch |
| `ALCHEMIST_DEVIN_ORG_ID` | unset | Required for Devin dispatch |

## Wind-Down Rule

Alchemist should stay small. If native Codex/Devin automations cover the queue
directly, or if the dispatcher creates more triage than it removes, disable the
cron and keep only the label convention.
