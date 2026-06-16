# AI Delivery Architecture

This document owns Touchstone's end-to-end delivery architecture for AI-authored changes. Other docs should link here instead of restating the workflow in detail.

## Target Flow

```text
Human user
  |
  | asks for a change
  v
Driver AI
  |
  | reads Touchstone steering
  | - checks repo state
  | - pulls main
  | - creates a feature branch before edits
  | - inspects the relevant code
  v
Implementation
  |
  | Driver AI edits files
  | may delegate bounded work through Conductor
  | may run focused local checks while developing
  v
Commit
  |
  | stage explicit paths
  | create focused commit(s)
  v
Open PR
  |
  | push feature branch
  | create GitHub PR
  | configured checks/reviews attach to the PR when enabled
  v
Agentic PR Review Loop
  |
  | Driver AI watches the PR
  | - check status and CI/check runs
  | - review PR comments and requested changes
  | - address actionable feedback with commits
  | - push updates that retrigger reviews/checks
  |
  v
Approval Gate
  |
  | Required reviews approved
  | Blocking comments resolved
  | Required checks green
  |
  v
Final Verification
  |
  | merge helper may run deterministic checks and
  | Conductor review/fix backstop for the final head
  v
Merge PR
  |
  | squash merge
  | sync local main
  | clean up branch/worktree
  v
Human user
  |
  | receives concise outcome
```

## Required Invariants

- Every change reaches `main` through a GitHub PR unless the documented emergency path is used.
- PR creation is not completion. The driver stays responsible until the PR is approved, merged, and synced locally.
- The exact commit merged has passed deterministic checks after its last mutation.
- The exact commit merged has no unresolved blocking review comments, requested changes, or failing required checks.
- Touchstone-managed LLM review uses Conductor as the only model access path. Driver CLIs do not call provider-specific review commands directly.
- PR creation is the review coordination surface. It should happen early enough for CI and any PR-visible agentic reviewers to work against visible PR state.
- Feature-branch push is not the expensive gate. It should preserve cheap local guardrails without running full test suites or LLM review by default.
- Merge is allowed only after PR-visible review and check approval. The local merge helper gates on requested-changes review decisions and unresolved review threads before and after Conductor review, then runs deterministic checks and Conductor review as a backstop.
- A deterministic check result may be reused only when the cache key includes the base ref, head commit, relevant config, and checker version/input boundary.

## Driver AI Responsibilities

The driver AI is Claude Code, Codex, Gemini CLI, or another AGENTS.md-native coding agent. The driver owns repo operations:

- branch before editing
- inspect and modify files
- run focused checks during implementation
- stage explicit file paths
- commit coherent changes
- open the PR
- watch PR comments, review decisions, and check status
- address actionable PR feedback with commits
- invoke final merge automation only after approval
- explain the outcome to the user

The driver may use Conductor for bounded implementation, research, or review work, but Conductor does not own the branch-to-merge lifecycle.

## Conductor Responsibilities

Conductor is the LLM router for review and delegated model work.

- Touchstone-managed LLM review runs through Conductor, whether invoked by PR automation, an advisory review command, or a final merge backstop.
- Conductor chooses the configured provider/model and handles provider fallback.
- Conductor may apply safe fixes only when the review mode and path policy allow it.
- Conductor findings should surface on the PR when possible. They are either fixed and committed on the PR branch, or block the merge.

Provider-specific commands such as direct Claude/Codex/Gemini review invocations are not part of the required review architecture.

## Agent Swarms And Worktrees

Parallel file-writing agents use worktrees by default.

```text
Driver AI
  |
  | decides work is parallelizable
  v
Agent swarm
  |
  | one worker per worktree
  | each worker gets an explicit file/module scope
  | workers commit only in their own worktree
  v
Driver AI integration
  |
  | integrates candidate changes into the primary PR branch
  | resolves conflicts
  | owns PR review loop, final checks, merge, and cleanup
```

Rules:

- Use worktrees for file-writing parallel agents.
- Give every worker a bounded task and explicit file ownership.
- Workers must not edit outside their assigned scope.
- Workers must not revert or overwrite another worker's work.
- Workers may produce candidate changes; only the driver integrates them into the PR that enters the review loop.
- No worker opens or merges the final PR unless the driver explicitly assigns that role.
- Clean up worktrees after merge or abandonment.

## Implementation Scope

The scripts now enforce the core merge-time parts of this architecture:

1. `open-pr.sh` creates or updates the PR and is the default driver entry point for shipping.
2. Creating or updating the PR should expose configured checks and, when enabled, PR-visible agentic reviewers.
3. The driver watches PR comments, review decisions, and checks after each push; actionable feedback becomes commits on the PR branch.
4. `merge-pr.sh` blocks draft PRs, active requested-changes decisions, unresolved review threads, and thread-state inspection failures before the final squash merge.
5. Review and preflight markers should key on base/head/config so repeated operations reuse valid results without hiding stale state.
6. Docs, templates, tests, and issue guidance should describe the PR-visible review loop consistently.
