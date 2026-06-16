# GEMINI.md

Read `AGENTS.md` first. It is the shared steering file for this repo.

Gemini CLI is a driving CLI here: it can edit files, run tests, commit, push,
and open PRs. Alchemist itself is only an issue dispatcher and PR babysitter for
Codex or Devin.

Do not invoke external model-routing or generated merge-gate tooling for this
repo. Use `bash scripts/validate.sh`, then normal GitHub PR workflow.
