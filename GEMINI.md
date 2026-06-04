# alchemist — Gemini CLI Instructions

Gemini CLI should follow the same project contract as Claude and Codex.

Read `AGENTS.md` before coding. Follow its Authoring Guide for implementation work and its Review Guide when explicitly reviewing a PR or running the AI review hook. Claude-specific context may live in `CLAUDE.md`, but `AGENTS.md` is the shared source for agent workflow and review priorities.

## Agent Roles And Fallbacks

Gemini CLI is a **driving CLI** in this repo: it owns file edits, git state, tests, commits, PR creation, Conductor review invocation, and merge helper execution. Claude Code and Codex are equivalent fallback drivers because all three load the same managed principles and delivery workflow.

Conductor is the **worker/reviewer router**. The driving CLI may invoke Conductor for code review or bounded model work, and Conductor can fall back across configured providers such as Claude, Codex, Gemini, or local models. Conductor provider fallback does not replace the driving CLI's responsibility for the branch → PR → review → automerge workflow.

## Delivery Lifecycle

Drive this automatically unless the user asks for a different flow:

1. Pull/rebase the default branch.
2. Create a feature branch before editing tracked files.
3. Make the change, stage explicit file paths, and commit with a concise message.
4. From a clean worktree, run `CODEX_REVIEW_FORCE=1 bash scripts/codex-review.sh` so Conductor can review and safely auto-fix before merge.
5. If Conductor creates fix commits, let the loop finish. If it blocks, address findings, commit, and rerun until clean.
6. Ship with `bash scripts/open-pr.sh --auto-merge`; this creates the PR, runs the final read-only Conductor merge review, squash-merges, and syncs the default branch.
7. Clean up the feature branch if it still exists locally.

## Parallel Agent Work

File-writing subagents use isolated worktrees by default. Follow `principles/agent-swarms.md` for slice manifests, file ownership, concurrency caps, and parent orchestration. Use `scripts/spawn-worktree.sh` to create local branch/worktree slices and `scripts/cleanup-worktrees.sh` for dry-run-first teardown.

<!-- conductor:begin v0.10.33 -->
## Conductor delegation

This project has [conductor](https://github.com/autumngarage/conductor)
available for delegating tasks to other LLMs from inside an agent loop.
You can shell out to it instead of trying to do everything yourself.

Quick reference:

- Quick factual/background ask:
  `conductor ask --kind research --effort minimal --brief-file /tmp/brief.md`.
- Deeper synthesis/research:
  `conductor ask --kind research --effort medium --brief-file /tmp/brief.md`.
- Code explanation or small coding judgment:
  `conductor ask --kind code --effort low --brief-file /tmp/brief.md`.
- Repo-changing implementation/debugging:
  `conductor ask --kind code --effort high --brief-file /tmp/brief.md`.
- Merge/PR/diff review:
  `conductor ask --kind review --base <ref> --brief-file /tmp/review.md`.
- Architecture/product judgment needing multiple views:
  `conductor ask --kind council --effort medium --brief-file /tmp/brief.md`.
- `conductor list` — show configured providers and their tags.

Conductor does not inherit your conversation context. For delegation,
write a complete brief with goal, context, scope, constraints, expected
output, and validation; use `--brief-file` for nontrivial `exec` tasks.
Default to `conductor ask`; use provider-specific `call` / `exec` only
when the user explicitly asks for a provider or the semantic API does not
fit.

Providers commonly worth delegating to:

- `kimi` — long-context summarization, cheap second opinions.
- `gemini` — web search, multimodal.
- `claude` / `codex` — strongest reasoning / coding agent loops.
- `ollama` — local, offline, privacy-sensitive.
- `council` kind — OpenRouter-only multi-model deliberation and synthesis.

Full delegation guidance (when to delegate, when not to, error handling):

    ~/.conductor/delegation-guidance.md
<!-- conductor:end -->
