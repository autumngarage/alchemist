# Alchemist

Issue dispatcher and PR babysitter for the
[Autumn Garage](https://github.com/autumngarage) family.

A user labels a GitHub issue `agent-ready`. On each Railway cron tick,
Alchemist scans that explicit queue, claims one bounded unit of work, triggers
an external coding agent, and then watches for the PR that references the issue.
It never clones target repositories, runs an LLM locally, commits code, or opens
the implementation PR itself.

Alchemist owns GitHub coordination only:

- intake labels and state labels
- one issue per repo locks
- Codex or Devin dispatch
- linked PR detection
- one-shot follow-up nudges for failed checks or requested changes
- optional GitHub auto-merge when a PR is ready

The external agent owns investigation, implementation, tests, commits, and PR
creation.

## Status

Alchemist runs as a remote Railway cron service, not as a long-lived local
daemon. The production path is Railway invoking `alchemist run-once --json` on
its cron schedule.

Use local `doctor` to check this checkout:

```bash
uv run alchemist doctor
```

Use Railway deployment logs to check the deployed runtime:

```bash
railway logs --service alchemist-cron --deployment
```

## Configure

A single TOML config file at `$ALCHEMIST_CONFIG` (defaults to
`~/.alchemist/config.toml`, or `/etc/alchemist/config.toml` for the Railway
deploy):

```toml
[alchemist]
org = "autumngarage"
intake_label = "agent-ready"
state_label_prefix = "alchemist"
agent_provider = "codex"          # codex or devin
dry_run = true
state_dir = "/var/alchemist/state"
max_issues_per_tick = 1
max_per_repo_per_tick = 1
max_concurrent_repos = 1
agent_stale_after_hours = 24
auto_merge = false
```

Environment variables override config:

- `ALCHEMIST_ORG`
- `ALCHEMIST_INTAKE_LABEL`
- `ALCHEMIST_STATE_LABEL_PREFIX`
- `ALCHEMIST_AGENT_PROVIDER` or legacy `ALCHEMIST_PROVIDER`
- `ALCHEMIST_DRY_RUN`
- `ALCHEMIST_STATE_DIR`
- `ALCHEMIST_MAX_ISSUES_PER_TICK`
- `ALCHEMIST_MAX_PER_REPO_PER_TICK`
- `ALCHEMIST_MAX_CONCURRENT_REPOS`
- `ALCHEMIST_AGENT_STALE_AFTER_HOURS`
- `ALCHEMIST_AUTO_MERGE`
- `ALCHEMIST_REPO_BLOCKLIST`

`ALCHEMIST_LABEL` is retained as a compatibility alias for
`ALCHEMIST_STATE_LABEL_PREFIX`. It does not define the intake queue.

Required externally:

- GitHub auth: either `GITHUB_TOKEN` with issue, PR, and metadata access, or
  GitHub App env vars (`ALCHEMIST_APP_ID`,
  `ALCHEMIST_APP_INSTALLATION_ID`, `ALCHEMIST_APP_PRIVATE_KEY`) so Alchemist can
  mint an installation token per tick.
- Codex: Codex cloud and GitHub integration enabled for the target repos.
  Alchemist triggers Codex by commenting `@codex` on the issue or PR.
- Devin: set `ALCHEMIST_AGENT_PROVIDER=devin`, `DEVIN_API_KEY`, and
  `ALCHEMIST_DEVIN_ORG_ID`. Alchemist creates Devin sessions through the Devin
  API and posts the session URL back to the issue.

## State Labels

For state prefix `alchemist`, Alchemist creates and uses:

- `agent-ready`: explicit intake queue
- `alchemist-dispatched`: external agent has been asked to work
- `alchemist-pr-open`: linked PR exists and is being watched
- `alchemist-blocked`: human triage needed
- `alchemist-shipped`: linked PR merged
- `alchemist-error`: coordinator failure
- `alchemist-skip`: opt out

## Lifecycle

1. Add `agent-ready` to an actionable issue.
2. Alchemist replaces it with `alchemist-dispatched` and triggers Codex or
   Devin.
3. The external agent investigates and opens a PR with `Closes #N` in the body.
4. Alchemist finds that PR, marks the issue `alchemist-pr-open`, and watches
   checks/reviews.
5. If checks fail or changes are requested, Alchemist nudges the same agent
   once.
6. If the PR merges, Alchemist marks the issue `alchemist-shipped`.
7. If no PR appears before `agent_stale_after_hours`, Alchemist marks the issue
   `alchemist-blocked`.

Set `auto_merge=true` only if GitHub branch protection is already the merge
gate you want. Alchemist then runs `gh pr merge --squash --auto` for PRs with
successful checks and no requested changes.

## Falsification

Keep this service only if it reduces coordination work. If the issue queue is
low, if most dispatched issues still need manual hand-holding, or if Codex/Devin
native automations cover the workflow directly, wind Alchemist down and keep the
state-label pattern as documentation.

## License

MIT - see `LICENSE`.
