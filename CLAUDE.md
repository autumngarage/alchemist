# CLAUDE.md

Read `AGENTS.md` first. It is the source of truth for this repo's agent
workflow, architecture, testing, and review priorities.

Alchemist is now intentionally simple: it dispatches GitHub issues to Codex or
Devin and babysits the resulting PR. It does not run local model routers or
private merge gates.

Use normal GitHub PR workflow:

1. Branch off `main`.
2. Make the scoped change.
3. Run `bash scripts/validate.sh`.
4. Commit explicit paths.
5. Push and open/update a PR with `gh`.
6. Rely on GitHub Actions, branch protection, and PR review for merge readiness.
