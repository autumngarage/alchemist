# Project State — Alchemist

> The fifth tool in the Autumn Garage family — the issue-driven transmuter.
> v0.0.1 ships **scan + doctor + banner only** so we can verify the cron + auth
> path on Railway before any code that mutates GitHub state goes live. v0.1
> ships the full `run-once` transmute loop.

## P0 — v0.0.1 bootstrap

CLI surface: `alchemist scan`, `alchemist doctor`, `alchemist banner`.
`alchemist run-once` is a placeholder that exits non-zero with a "ships in v0.1"
message. This is intentional — the deploy process needs a target before we
add load-bearing code that touches real repos.

## P1 — v0.1 transmute loop

`alchemist run-once`:
1. Scan org for labelled issues
2. For each issue (capped at `max_per_tick`):
   - Acquire lockfile on `<state_dir>/locks/<repo>-<issue>.lock`
   - Transition label `dispatch → working`
   - Clone or update the target repo, create branch `alchemist/issue-<N>-<slug>`
   - Render brief from versioned `templates/brief.md.j2`
   - `conductor exec --with <provider> --brief-file <path>` with `--timeout 600`
   - Invoke `<touchstone>/scripts/codex-review.sh` from the cloned repo's root
   - Push branch, `gh pr create` with body = issue link + review summary + cost log
   - Transition label `working → shipped`
   - Release lock
3. Errors flip label to `error` + structured comment, release lock, no retries

Dry-run mode skips the mutating steps and logs what would happen.

## P2 — Dockerfile + Railway

Dockerfile bundles gh, git, conductor (pipx-installed), touchstone (cloned at
pinned tag to /opt/touchstone), alchemist itself. ENTRYPOINT runs once and exits
so it's cron-friendly.

Railway: new project, one service, cron `*/5 * * * *`, persistent volume at
`/var/alchemist/state`, env vars for GITHUB_TOKEN, OPENROUTER_API_KEY,
ALCHEMIST_*.

## Dogfood gates (live cutover sequence)

Three manual transitions, never auto-graduated:

1. **A** — `dry_run=true`, label `alchemist-test`. Henry files a tiny test issue.
2. **B** — `dry_run=false`, label still `alchemist-test`. Real PR against a real
   trivial fix. Inspect, merge or reject.
3. **Live** — flip label to `alchemist-dispatch`. `max_per_tick=1` for one week,
   then lift the cap.

## Open questions

- After Dogfood B is clean, does Cortex T1.6 journal-write integration ship in
  v0.1 or wait for v0.2? (Plan says v0.2 — keep core thin first.)
- When the user volume on `alchemist-dispatch` justifies it, switch to GitHub
  App auth with installation tokens. v0.1 stays on fine-grained PAT.
