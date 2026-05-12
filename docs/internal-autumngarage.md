# Internal Autumn Garage Setup

This is the narrow profile for running Alchemist inside the Autumn Garage org.
Use the general operator flow in `docs/operator-runbook.md`; this document owns
the repo scope and runtime defaults for our internal deployment.

## Repo Scope

Initial watched repos:

- `autumngarage/alchemist`
- `autumngarage/conductor`
- `autumngarage/cortex`
- `autumngarage/sentinel`
- `autumngarage/touchstone`
- `autumngarage/autumn-garage`

Excluded from the first internal rollout:

- Homebrew tap repos: release metadata is deterministic and usually should not
  be edited by an agent loop.
- `autumngarage/autumn-mail`: private app repo, not part of the core toolchain
  rollout.

Alchemist still scans the org, so keep the deployment blocklist set even though
the dispatch labels are only seeded on the watched repos.

## GitHub Labels

Seed or repair labels with:

```bash
bash scripts/setup-autumngarage-internal.sh --execute
```

The script is idempotent and dry-runs by default. It creates both the live
`alchemist-dispatch` state labels and the `alchemist-test` state labels.

Verify the live queue is idle before enabling the cron:

```bash
gh search issues --owner autumngarage --label alchemist-dispatch --state open --archived=false
gh search issues --owner autumngarage --label alchemist-working --state open --archived=false
```

## Railway Variables

After `railway login`, configure the `alchemist-cron` service with the internal
profile:

```bash
railway variable set ALCHEMIST_ORG=autumngarage --service alchemist-cron
railway variable set ALCHEMIST_LABEL=alchemist-dispatch --service alchemist-cron
railway variable set ALCHEMIST_DRY_RUN=false --service alchemist-cron
railway variable set ALCHEMIST_PROVIDER=openrouter --service alchemist-cron
railway variable set 'ALCHEMIST_BUDGET=$2' --service alchemist-cron
railway variable set ALCHEMIST_STATE_DIR=/var/alchemist/state --service alchemist-cron
railway variable set ALCHEMIST_MAX_ISSUES_PER_TICK=1 --service alchemist-cron
railway variable set ALCHEMIST_MAX_PER_REPO_PER_TICK=1 --service alchemist-cron
railway variable set ALCHEMIST_MAX_CONCURRENT_REPOS=1 --service alchemist-cron
railway variable set ALCHEMIST_REPO_BLOCKLIST=homebrew-touchstone,homebrew-conductor,homebrew-sentinel,homebrew-cortex,homebrew-alchemist,autumn-mail --service alchemist-cron
```

Secrets still come from the operator runbook:

- `OPENROUTER_API_KEY`
- GitHub App vars: `ALCHEMIST_APP_ID`, `ALCHEMIST_APP_INSTALLATION_ID`,
  `ALCHEMIST_APP_PRIVATE_KEY`

Use GitHub App auth for this deployment. PAT auth remains a fallback for local
debugging, but the unattended Railway cron should mint per-tick installation
tokens.

## Deploy And Verify

From this repo:

```bash
railway up --service alchemist-cron --detach
railway logs --service alchemist-cron --deployment
```

The first healthy tick should pass all doctor checks and then report no work
when no issue has the dispatch label.

For a local env-parity check after Railway variables are set:

```bash
railway run --service alchemist-cron -- alchemist doctor --json
```

Do not lift the caps until at least one internal issue has shipped through a
merged `[alchemist]` PR on the live `alchemist-dispatch` label. Keep
`max_issues_per_tick=1` and `max_concurrent_repos=1` for the first week of
internal use.
