#!/usr/bin/env bash
#
# scripts/merge-pr.sh — squash-merge a PR and clean up.
#
# Usage:
#   bash scripts/merge-pr.sh <pr-number>
#   bash scripts/merge-pr.sh <pr-number> --bypass-with-disclosure="<reason>"
#
# What this does:
#   1. Verifies the PR is open and mergeable.
#   2. Runs AI code review as a merge gate.
#   3. Squash-merges and deletes the remote branch.
#   4. Checks out/syncs the default branch where the local topology permits.
#   5. Deletes the verified-merged local feature branch when safe.
#
# Exit codes:
#   0 — merged cleanly
#   1 — merge failed (PR not mergeable, conflicts, etc.)
#   2 — usage / environment error
#
set -euo pipefail

PR_NUMBER=""
BYPASS_REASON=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REVIEW_SCRIPT="$SCRIPT_DIR/codex-review.sh"
PREFLIGHT_SCRIPT="$SCRIPT_DIR/../lib/preflight.sh"
REVIEW_COMMENT_SCRIPT="$SCRIPT_DIR/../lib/review-comment.sh"
if [ -f "$SCRIPT_DIR/../lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  source "$SCRIPT_DIR/../lib/events.sh"
else
  touchstone_emit_event() { :; }
fi
if [ -f "$PREFLIGHT_SCRIPT" ]; then
  # shellcheck source=../lib/preflight.sh
  source "$PREFLIGHT_SCRIPT"
fi
if [ -f "$REVIEW_COMMENT_SCRIPT" ]; then
  # shellcheck source=../lib/review-comment.sh
  source "$REVIEW_COMMENT_SCRIPT"
fi
REVIEWED_HEAD_OID=""
PR_HEAD_BRANCH=""
BYPASS_REVIEW=false
TOUCHSTONE_MERGE_FAILURE_REASON="nonzero-exit"
PREFLIGHT_REQUIRED=true
COMMENT_ON_CLEAN=true
COMMENT_FINDINGS_HISTORY=true
REVIEW_SUMMARY_FILE=""

on_merge_exit() {
  local rc="$?"
  if [ "$rc" -ne 0 ]; then
    touchstone_emit_event failed phase=merge reason="$TOUCHSTONE_MERGE_FAILURE_REASON" pr_number="$PR_NUMBER"
  fi
  return "$rc"
}

trap on_merge_exit EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bypass-with-disclosure=*)
      BYPASS_REVIEW=true
      BYPASS_REASON="${1#*=}"
      shift
      ;;
    --bypass-with-disclosure)
      echo "ERROR: --bypass-with-disclosure requires a non-empty reason." >&2
      exit 2
      ;;
    --*)
      echo "ERROR: Unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [ -n "$PR_NUMBER" ]; then
        echo "ERROR: Unexpected extra argument: $1" >&2
        exit 2
      fi
      PR_NUMBER="$1"
      shift
      ;;
  esac
done

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

BYPASS_REASON="$(trim "$(printf '%s' "$BYPASS_REASON" | tr '\r\n\t' '   ')")"

if [ -z "$PR_NUMBER" ] || ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash scripts/merge-pr.sh <pr-number> [--bypass-with-disclosure=\"<reason>\"]" >&2
  exit 2
fi
if [ "$BYPASS_REVIEW" = true ] && [ -z "$BYPASS_REASON" ]; then
  echo "ERROR: --bypass-with-disclosure requires a non-empty reason." >&2
  exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' is not installed." >&2
  exit 2
fi

# Resolve the default branch.
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || echo main)"

truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_bool() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) printf 'true' ;;
    false | 0 | no | off) printf 'false' ;;
    *) printf '%s' "$1" ;;
  esac
}

load_merge_review_config() {
  local config_file
  config_file="$(git rev-parse --show-toplevel 2>/dev/null)/.codex-review.toml"
  [ -f "$config_file" ] || return 0
  [ -f "$SCRIPT_DIR/../lib/toml.sh" ] || return 0

  # shellcheck source=../lib/toml.sh
  source "$SCRIPT_DIR/../lib/toml.sh"

  merge_pr_toml_callback() {
    local section="$1"
    local key="$2"
    local value="$3"

    if [ "$section" = "review" ] && [ "$key" = "preflight_required" ]; then
      PREFLIGHT_REQUIRED="$(normalize_bool "$value")"
    elif [ "$section" = "review" ] && [ "$key" = "comment_on_clean" ]; then
      COMMENT_ON_CLEAN="$(normalize_bool "$value")"
    elif [ "$section" = "review" ] && [ "$key" = "comment_findings_history" ]; then
      COMMENT_FINDINGS_HISTORY="$(normalize_bool "$value")"
    fi
  }

  toml_parse "$config_file" merge_pr_toml_callback
}

review_clean_marker_key() {
  local branch="$1"
  printf '%s' "$branch" | sed 's/[^A-Za-z0-9._-]/_/g'
}

review_clean_marker_file() {
  local branch="$1"
  printf '%s/%s.clean' \
    "$(git rev-parse --git-path touchstone/reviewer-clean)" \
    "$(review_clean_marker_key "$branch")"
}

review_findings_history_file() {
  local branch="$1"
  printf '%s/%s.jsonl' \
    "$(git rev-parse --git-path touchstone/reviewer-findings-history)" \
    "$(review_clean_marker_key "$branch")"
}

marker_field() {
  local field="$1"
  local marker="$2"
  awk -F= -v key="$field" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$marker"
}

worktree_path_for_branch() {
  local branch="$1"
  local current_path=""
  local current_branch=""
  local line key value

  git worktree list --porcelain | while IFS= read -r line || [ -n "$line" ]; do
    if [ -z "$line" ]; then
      if [ "$current_branch" = "refs/heads/$branch" ]; then
        printf '%s\n' "$current_path"
        exit 0
      fi
      current_path=""
      current_branch=""
      continue
    fi

    key="${line%% *}"
    value="${line#* }"
    case "$key" in
      worktree) current_path="$value" ;;
      branch) current_branch="$value" ;;
    esac
  done

  if [ "$current_branch" = "refs/heads/$branch" ]; then
    printf '%s\n' "$current_path"
  fi
  return 0
}

branch_has_clean_review_marker() {
  local branch="$1"
  local head_oid="$2"
  local merge_base="$3"
  local marker marker_branch marker_head marker_merge_base live_branch_head
  marker="$(review_clean_marker_file "$branch")"
  [ -f "$marker" ] || return 1
  grep -q '^result=CODEX_REVIEW_CLEAN$' "$marker" || return 1
  marker_branch="$(marker_field branch "$marker")"
  marker_head="$(marker_field head "$marker")"
  marker_merge_base="$(marker_field merge_base "$marker")"

  if ! live_branch_head="$(git rev-parse "$branch" 2>/dev/null)"; then
    live_branch_head="$(git rev-parse HEAD 2>/dev/null || echo "")"
  fi

  # Invariant: A clean-review marker is valid only when its `head` field equals the current branch HEAD.
  [ "$marker_branch" = "$branch" ] \
    && [ -n "$live_branch_head" ] \
    && [ "$live_branch_head" = "$head_oid" ] \
    && [ "$marker_head" = "$live_branch_head" ] \
    && [ "$marker_merge_base" = "$merge_base" ]
}

sync_default_branch_after_merge() {
  local current_branch current_worktree default_worktree

  echo "==> Merged. Updating local $DEFAULT_BRANCH ..."
  current_branch="$(git rev-parse --abbrev-ref HEAD)"

  if [ "$current_branch" = "$DEFAULT_BRANCH" ]; then
    if ! git pull --rebase; then
      echo "WARNING: PR #$PR_NUMBER merged remotely, but local $DEFAULT_BRANCH could not pull --rebase." >&2
      echo "WARNING: Run this when convenient: git pull --rebase" >&2
    fi
    return 0
  fi

  current_worktree="$(git rev-parse --show-toplevel)"
  default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
  if [ -n "$default_worktree" ] && [ "$default_worktree" != "$current_worktree" ]; then
    if [ ! -d "$default_worktree" ]; then
      echo "WARNING: $DEFAULT_BRANCH is recorded as checked out in a missing worktree: $default_worktree" >&2
      echo "WARNING: This is stale git worktree metadata, usually from deleting the directory directly." >&2
      echo "WARNING: Run 'git worktree prune' from a remaining checkout, then rerun local sync if needed." >&2
      return 0
    fi
    echo "==> $DEFAULT_BRANCH is checked out in sibling worktree: $default_worktree"
    echo "==> Fast-forwarding that worktree after remote merge ..."
    if git -C "$default_worktree" pull --ff-only; then
      return 0
    fi
    echo "WARNING: PR #$PR_NUMBER merged remotely, but sibling worktree '$default_worktree' could not fast-forward." >&2
    echo "WARNING: Run this when convenient: git -C '$default_worktree' pull --ff-only" >&2
    return 0
  fi

  if ! git checkout "$DEFAULT_BRANCH"; then
    echo "WARNING: PR #$PR_NUMBER merged remotely, but this worktree could not check out $DEFAULT_BRANCH." >&2
    echo "WARNING: Run this when convenient: git checkout '$DEFAULT_BRANCH' && git pull --rebase" >&2
    return 0
  fi
  if ! git pull --rebase; then
    echo "WARNING: PR #$PR_NUMBER merged remotely, but local $DEFAULT_BRANCH could not pull --rebase." >&2
    echo "WARNING: Run this when convenient: git pull --rebase" >&2
  fi
}

checkout_default_ref_for_cleanup() {
  local branch="$1"
  local reviewed_head="$2"
  local current_branch current_head current_worktree default_worktree

  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  current_head="$(git rev-parse HEAD 2>/dev/null || echo "")"
  if [ "$current_branch" != "$branch" ]; then
    if [ "$current_branch" != "HEAD" ] || [ "$current_head" != "$reviewed_head" ]; then
      return 0
    fi
  fi

  current_worktree="$(git rev-parse --show-toplevel)"
  default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
  if [ -n "$default_worktree" ] && [ "$default_worktree" != "$current_worktree" ]; then
    echo "==> $DEFAULT_BRANCH is checked out elsewhere; detaching this worktree at $DEFAULT_BRANCH before local branch cleanup ..."
    if git checkout --detach "$DEFAULT_BRANCH"; then
      return 0
    fi
    echo "WARNING: Could not detach this worktree at $DEFAULT_BRANCH; leaving local branch '$branch' intact." >&2
    return 1
  fi

  if git checkout "$DEFAULT_BRANCH"; then
    return 0
  fi
  if git checkout --detach "$DEFAULT_BRANCH"; then
    echo "==> Detached this worktree at $DEFAULT_BRANCH before local branch cleanup."
    return 0
  fi
  echo "WARNING: Could not move off local branch '$branch'; leaving it intact." >&2
  return 1
}

cleanup_local_pr_branch_after_merge() {
  local branch="$PR_HEAD_BRANCH"
  local reviewed_head="$REVIEWED_HEAD_OID"
  local local_head pr_state

  if [ -z "$branch" ] || [ -z "$reviewed_head" ]; then
    echo "WARNING: Missing reviewed PR head metadata; skipping local branch cleanup." >&2
    return 0
  fi
  if [ "$branch" = "$DEFAULT_BRANCH" ] || [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
    echo "WARNING: Refusing to delete protected branch '$branch' after PR #$PR_NUMBER." >&2
    return 0
  fi
  if ! git show-ref --verify --quiet "refs/heads/$branch"; then
    echo "==> Local branch '$branch' is already absent."
    return 0
  fi
  if ! local_head="$(git rev-parse "$branch" 2>/dev/null)"; then
    echo "WARNING: Could not resolve local branch '$branch'; leaving it intact." >&2
    return 0
  fi
  if [ "$local_head" != "$reviewed_head" ]; then
    echo "WARNING: Local branch '$branch' is at $local_head, not reviewed PR head $reviewed_head; leaving it intact." >&2
    return 0
  fi
  pr_state="$(gh pr view "$PR_NUMBER" --json state --jq '.state' 2>/dev/null || echo "")"
  if [ "$pr_state" != "MERGED" ]; then
    echo "WARNING: PR #$PR_NUMBER is not confirmed MERGED (state: ${pr_state:-unknown}); leaving local branch '$branch' intact." >&2
    return 0
  fi

  if ! checkout_default_ref_for_cleanup "$branch" "$reviewed_head"; then
    return 0
  fi

  echo "==> Deleting local branch '$branch' after verified squash merge of $reviewed_head ..."
  if git branch -D "$branch"; then
    echo "==> Local branch '$branch' deleted."
  else
    echo "WARNING: Could not delete local branch '$branch' after verified merge." >&2
    echo "WARNING: Run this when convenient after moving off the branch: git branch -D '$branch'" >&2
  fi
}

print_bypass_banner() {
  cat <<EOF

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! BYPASSING REVIEWER GATE
!! reason: $BYPASS_REASON
!! This bypass is recorded on the PR and squash commit.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

EOF
}

# Append a squash-merge record to .git/touchstone/squash-map.jsonl so
# scripts/cleanup-branches.sh can recognize a branch as squash-merged even
# after $DEFAULT_BRANCH evolves past it (later commits on the same files
# break the tree-equivalence heuristic).
#
# The record carries:
#   - branch       : the head ref name of the merged PR
#   - pr           : PR number
#   - branch_oid   : tip of the branch at merge time (so cleanup can detect
#                    "branch picked up new commits after the squash" and
#                    fall through to the existing tree check)
#   - squash_commit: the squash commit on the default branch (best effort —
#                    empty string if gh cannot resolve it yet)
#   - ts           : UTC ISO timestamp
#
# I/O is best-effort. A failure to write must not fail the merge: the merge
# already succeeded server-side, and the squash-map is an optimization for
# later cleanup, not a correctness boundary. Any failure is logged to stderr.
record_squash_merge() {
  local branch="$1"
  local pr="$2"
  local branch_oid="$3"
  local squash_commit="${4:-}"
  local map_path map_dir ts

  if [ -z "$branch" ] || [ -z "$pr" ] || [ -z "$branch_oid" ]; then
    echo "WARNING: record_squash_merge: missing branch/pr/oid, skipping squash-map write." >&2
    return 0
  fi

  if ! map_path="$(git rev-parse --git-path touchstone/squash-map.jsonl 2>/dev/null)" \
    || [ -z "$map_path" ]; then
    echo "WARNING: record_squash_merge: could not resolve squash-map path; skipping." >&2
    return 0
  fi
  map_dir="$(dirname "$map_path")"
  if ! mkdir -p "$map_dir" 2>/dev/null; then
    echo "WARNING: record_squash_merge: could not create $map_dir; skipping squash-map write." >&2
    return 0
  fi

  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"

  # JSON-encode each string field. Bash can produce safe JSON for these
  # tightly constrained values (branch names exclude " and \, OIDs are hex,
  # ts is ISO-8601) by simply quoting — no escaping needed in practice.
  # Defense in depth: refuse to record if any field contains a quote or
  # backslash, rather than emit malformed JSON.
  local field
  for field in "$branch" "$pr" "$branch_oid" "$squash_commit" "$ts"; do
    case "$field" in
      *\"* | *\\*)
        echo "WARNING: record_squash_merge: field contains quote/backslash, skipping squash-map write." >&2
        return 0
        ;;
    esac
  done

  local line
  line="{\"branch\":\"$branch\",\"pr\":\"$pr\",\"branch_oid\":\"$branch_oid\",\"squash_commit\":\"$squash_commit\",\"ts\":\"$ts\"}"
  if ! printf '%s\n' "$line" >>"$map_path" 2>/dev/null; then
    echo "WARNING: record_squash_merge: could not append to $map_path; skipping squash-map write." >&2
    return 0
  fi
  echo "==> Recorded squash-merge metadata for '$branch' -> $map_path"
}

record_bypass_comment() {
  gh pr comment "$PR_NUMBER" --body "Reviewer bypassed via \`--bypass-with-disclosure\`. Reason: $BYPASS_REASON"
}

post_clean_review_comment() {
  local summary_file="$1"
  local summary_json comment

  if ! truthy "$COMMENT_ON_CLEAN"; then
    echo "==> Clean-review PR comment disabled by [review].comment_on_clean=false."
    return 0
  fi
  if [ "$BYPASS_REVIEW" = true ]; then
    return 0
  fi
  if ! declare -F format_clean_review_comment >/dev/null 2>&1 \
    || ! declare -F post_pr_review_comment >/dev/null 2>&1; then
    echo "WARNING: review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping clean-review comment." >&2
    return 0
  fi
  if [ -z "$summary_file" ] || [ ! -f "$summary_file" ]; then
    echo "WARNING: clean review summary file missing; skipping clean-review comment." >&2
    return 0
  fi

  summary_json="$(tail -n 1 "$summary_file" 2>/dev/null || true)"
  if [ -z "$summary_json" ]; then
    echo "WARNING: clean review summary file is empty; skipping clean-review comment." >&2
    return 0
  fi

  comment="$(format_clean_review_comment "$summary_json")"
  if post_pr_review_comment "$PR_NUMBER" "$comment"; then
    echo "==> Posted clean-review PR comment."
    return 0
  fi

  echo "WARNING: failed to post clean-review PR comment for PR #$PR_NUMBER." >&2
  return 0
}

post_findings_history_comment() {
  local branch="$1"
  local history_file comment

  if ! truthy "$COMMENT_FINDINGS_HISTORY"; then
    echo "==> Findings-history PR comment disabled by [review].comment_findings_history=false."
    return 0
  fi
  if [ "$BYPASS_REVIEW" = true ]; then
    return 0
  fi
  if ! declare -F format_findings_history_comment >/dev/null 2>&1 \
    || ! declare -F post_pr_review_comment >/dev/null 2>&1; then
    echo "WARNING: review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping findings-history comment." >&2
    return 0
  fi
  if [ -z "$branch" ]; then
    echo "WARNING: PR head branch missing; skipping findings-history comment." >&2
    return 0
  fi

  history_file="$(review_findings_history_file "$branch")"
  if ! comment="$(format_findings_history_comment "$history_file")"; then
    echo "==> No actionable review findings history to comment."
    return 0
  fi

  if post_pr_review_comment "$PR_NUMBER" "$comment"; then
    echo "==> Posted findings-history PR comment."
    return 0
  fi

  echo "WARNING: failed to post findings-history PR comment for PR #$PR_NUMBER." >&2
  return 0
}

failed_checks() {
  gh pr checks "$PR_NUMBER" \
    --json name,bucket,state,link \
    --template '{{range .}}{{if eq .bucket "fail"}}{{.name}}{{"\t"}}{{.state}}{{"\t"}}{{.link}}{{"\n"}}{{end}}{{end}}' \
    2>/dev/null || true
}

print_failed_checks_and_exit() {
  local failed_checks="$1"
  local name state link

  [ -n "$failed_checks" ] || return 1

  echo "ERROR: PR #$PR_NUMBER has failed check(s); stopping automerge." >&2
  while IFS="$(printf '\t')" read -r name state link || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    if [ -n "$link" ]; then
      echo "       - $name (${state:-failed}): $link" >&2
    else
      echo "       - $name (${state:-failed})" >&2
    fi
  done <<<"$failed_checks"
  TOUCHSTONE_MERGE_FAILURE_REASON="check-failed"
  exit 1
}

review_output_has_concrete_findings() {
  local output_file="$1"

  [ -f "$output_file" ] || return 1
  grep -Eq 'CODEX_REVIEW_BLOCKED|Conductor review found|blocking advisory finding|blocking finding|findings[" ]*[:=][" ]*[1-9]' "$output_file"
}

run_preflight_gate() {
  local base_ref="$1"

  if ! truthy "$PREFLIGHT_REQUIRED"; then
    echo "==> Preflight disabled by [review].preflight_required=false."
    return 0
  fi
  if truthy "${TOUCHSTONE_NO_PREFLIGHT:-false}"; then
    echo "==> Skipping preflight because TOUCHSTONE_NO_PREFLIGHT=1."
    return 0
  fi
  if ! declare -F touchstone_preflight_main >/dev/null 2>&1; then
    echo "==> Preflight helper not found at $PREFLIGHT_SCRIPT — skipping preflight."
    return 0
  fi

  echo "==> Running deterministic preflight before merge review (diff vs $base_ref) ..."
  touchstone_emit_event preflight_started pr_number="$PR_NUMBER" mode=merge
  if touchstone_preflight_main_sanitized --diff "$base_ref" "$(git rev-parse --show-toplevel)"; then
    touchstone_emit_event preflight_clean pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
    return 0
  fi

  echo "ERROR: Deterministic preflight failed; refusing to spend provider tokens on review." >&2
  echo "       Fix the preflight failure or set TOUCHSTONE_NO_PREFLIGHT=1 for an emergency bypass." >&2
  touchstone_emit_event preflight_blocked pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
  TOUCHSTONE_MERGE_FAILURE_REASON="preflight-blocked"
  return 1
}

run_merge_review() {
  local current_branch default_base_ref local_head pr_head_branch pr_head_oid

  if ! pr_head_branch="$(gh pr view "$PR_NUMBER" --json headRefName --jq '.headRefName' 2>/dev/null)"; then
    echo "ERROR: Failed to resolve PR #$PR_NUMBER head branch." >&2
    exit 1
  fi
  if ! pr_head_oid="$(gh pr view "$PR_NUMBER" --json headRefOid --jq '.headRefOid' 2>/dev/null)"; then
    echo "ERROR: Failed to resolve PR #$PR_NUMBER head commit." >&2
    exit 1
  fi
  if [ -z "$pr_head_branch" ]; then
    echo "ERROR: PR #$PR_NUMBER head branch is empty." >&2
    exit 1
  fi
  if [ -z "$pr_head_oid" ]; then
    echo "ERROR: PR #$PR_NUMBER head commit is empty." >&2
    exit 1
  fi

  PR_HEAD_BRANCH="$pr_head_branch"
  REVIEWED_HEAD_OID="$pr_head_oid"
  default_base_ref="origin/$DEFAULT_BRANCH"

  if [ "$BYPASS_REVIEW" = true ]; then
    echo "==> Refreshing $default_base_ref before reviewer bypass validation ..."
    if ! git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"; then
      echo "ERROR: Failed to refresh $default_base_ref before reviewer bypass validation." >&2
      exit 1
    fi
    if ! git rev-parse --verify --quiet "$default_base_ref^{commit}" >/dev/null; then
      echo "ERROR: Could not verify $default_base_ref before reviewer bypass validation." >&2
      exit 1
    fi
    if ! git cat-file -e "$pr_head_oid^{commit}" 2>/dev/null; then
      echo "==> Checking out PR #$PR_NUMBER head ($pr_head_branch) for reviewer bypass validation ..."
      gh pr checkout "$PR_NUMBER" --detach
    fi
    local current_merge_base
    if ! current_merge_base="$(git merge-base "$default_base_ref" "$pr_head_oid" 2>/dev/null)"; then
      echo "ERROR: Could not compute merge base for PR #$PR_NUMBER head against $default_base_ref." >&2
      exit 1
    fi
    if ! branch_has_clean_review_marker "$pr_head_branch" "$pr_head_oid" "$current_merge_base"; then
      echo "ERROR: Refusing reviewer bypass for PR #$PR_NUMBER." >&2
      echo "       No prior clean review marker matches branch '$pr_head_branch' at head '$pr_head_oid' and merge base '$current_merge_base'." >&2
      echo "       Run the reviewer cleanly once before using --bypass-with-disclosure." >&2
      exit 1
    fi
    print_bypass_banner
    record_bypass_comment
    return 0
  fi

  if truthy "${SKIP_REVIEW:-${SKIP_CODEX_REVIEW:-false}}"; then
    echo "==> Skipping merge review because SKIP_REVIEW is set."
    return 0
  fi

  if [ ! -f "$REVIEW_SCRIPT" ]; then
    echo "==> Review script not found at $REVIEW_SCRIPT — skipping review."
    return 0
  fi

  echo "==> Refreshing $default_base_ref for merge review ..."
  if ! git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"; then
    echo "ERROR: Failed to refresh $default_base_ref before merge review." >&2
    exit 1
  fi
  if ! git rev-parse --verify --quiet "$default_base_ref^{commit}" >/dev/null; then
    echo "ERROR: Could not verify $default_base_ref before merge review." >&2
    exit 1
  fi

  # The reviewer reads the committed diff against the default base; uncommitted
  # changes in unrelated paths do not affect that view. Only refuse when at
  # least one dirty path overlaps the PR's diff against the default base, which
  # is the actual ambiguous-tree case. Refusing on any dirty path false-positives
  # whenever the operator has unrelated WIP they aren't ready to commit.
  local dirty_status diff_paths dirty_paths overlap
  dirty_status="$(git status --porcelain)"
  if [ -n "$dirty_status" ]; then
    if ! diff_paths="$(git diff --name-only "$default_base_ref"...HEAD 2>/dev/null | sort -u)"; then
      echo "ERROR: Could not compute diff against $default_base_ref to evaluate dirty-tree overlap." >&2
      exit 1
    fi
    # Parse `git status --porcelain` robustly: rename entries have the form
    # `R<space|index> old -> new`, others are `XY path`. We want the path that
    # actually exists in the working tree, which is the post-rename path.
    dirty_paths="$(printf '%s\n' "$dirty_status" \
      | awk '{
          line = substr($0, 4)
          idx = index(line, " -> ")
          if (idx > 0) {
            print substr(line, idx + 4)
          } else {
            print line
          }
        }' \
      | sort -u)"
    if [ -n "$diff_paths" ] && [ -n "$dirty_paths" ]; then
      overlap="$(comm -12 <(printf '%s\n' "$diff_paths") <(printf '%s\n' "$dirty_paths"))"
    else
      overlap=""
    fi
    if [ -n "$overlap" ]; then
      echo "ERROR: Working tree has uncommitted changes that overlap PR #$PR_NUMBER's diff vs $default_base_ref;" >&2
      echo "       refusing to run review against an ambiguous tree. Overlapping paths:" >&2
      printf '%s\n' "$overlap" | sed 's/^/         /' >&2
      exit 1
    fi
    if [ -n "$dirty_paths" ]; then
      echo "==> Working tree has uncommitted changes outside PR #$PR_NUMBER's diff vs $default_base_ref; proceeding."
    fi
  fi

  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  local_head="$(git rev-parse HEAD)"
  if [ "$current_branch" != "$pr_head_branch" ] || [ "$local_head" != "$pr_head_oid" ]; then
    echo "==> Checking out PR #$PR_NUMBER head ($pr_head_branch) for merge review ..."
    gh pr checkout "$PR_NUMBER" --detach
    local_head="$(git rev-parse HEAD)"
  fi

  if [ "$local_head" != "$pr_head_oid" ]; then
    echo "ERROR: Local review checkout does not match PR #$PR_NUMBER head commit." >&2
    echo "       expected: $pr_head_oid" >&2
    echo "       actual:   $local_head" >&2
    exit 1
  fi

  run_preflight_gate "$default_base_ref" || return $?

  echo "==> Running merge review ..."
  local review_rc=0
  local review_output_file
  review_output_file="$(mktemp -t touchstone-merge-review.XXXXXX.txt)"
  REVIEW_SUMMARY_FILE="$(git rev-parse --git-path "touchstone/review-summary-pr-${PR_NUMBER}.json" 2>/dev/null || echo "")"
  if [ -n "$REVIEW_SUMMARY_FILE" ]; then
    mkdir -p "$(dirname "$REVIEW_SUMMARY_FILE")" 2>/dev/null || true
    rm -f "$REVIEW_SUMMARY_FILE" 2>/dev/null || true
  fi
  touchstone_emit_event review_started pr_number="$PR_NUMBER" mode=review-only
  set +e
  CODEX_REVIEW_BASE="$default_base_ref" \
    CODEX_REVIEW_BRANCH_NAME="$pr_head_branch" \
    CODEX_REVIEW_FORCE=1 \
    CODEX_REVIEW_MODE=review-only \
    TOUCHSTONE_PREFLIGHT_ALREADY_RAN=1 \
    CODEX_REVIEW_SUMMARY_FILE="$REVIEW_SUMMARY_FILE" \
    bash "$REVIEW_SCRIPT" 2>&1 | tee "$review_output_file"
  review_rc="${PIPESTATUS[0]}"
  set -e

  if [ "$review_rc" -eq 0 ]; then
    rm -f "$review_output_file"
    touchstone_emit_event review_clean pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
    return 0
  fi

  # Issue #182: when the merge-pr review fails (typically a routing-induced
  # timeout — Ollama wedging on review-only on small diffs, etc.) AND the
  # exact same HEAD already has a recorded clean review marker from a
  # prior run, auto-promote to bypass-with-disclosure rather than refusing
  # the merge. The marker proves the diff was reviewed cleanly; a failed
  # second iteration is a stalled reviewer gate, exactly the case that
  # principles/git-workflow.md documents `--bypass-with-disclosure` for.
  # Without this auto-promotion, every operator hit by the routing bug
  # has to invoke the bypass manually with a synthesized reason.
  local current_merge_base
  if current_merge_base="$(git merge-base "$default_base_ref" "$pr_head_oid" 2>/dev/null)" \
    && branch_has_clean_review_marker "$pr_head_branch" "$pr_head_oid" "$current_merge_base"; then
    if review_output_has_concrete_findings "$review_output_file"; then
      echo "" >&2
      echo "ERROR: Merge review returned concrete findings; refusing to auto-bypass with a prior clean marker." >&2
      echo "       Fix the findings or use an explicit manual bypass with disclosure." >&2
      rm -f "$review_output_file"
      touchstone_emit_event review_blocked pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
      TOUCHSTONE_MERGE_FAILURE_REASON="review-blocked"
      return "$review_rc"
    fi
    echo "" >&2
    echo "==> Merge review exited $review_rc, but a prior clean review marker is recorded for HEAD $pr_head_oid." >&2
    echo "==> Auto-promoting to reviewer bypass with disclosure." >&2
    BYPASS_REVIEW=true
    BYPASS_REASON="merge-pr.sh final review iteration exited ${review_rc} (typically a routing-induced timeout); a prior clean review marker is recorded for HEAD ${pr_head_oid}, so this auto-bypass preserves the safety guarantee that the diff was reviewed cleanly at least once."
    touchstone_emit_event review_bypass pr_number="$PR_NUMBER" head_sha="$pr_head_oid" reason="$BYPASS_REASON"
    print_bypass_banner
    record_bypass_comment
    rm -f "$review_output_file"
    return 0
  fi

  rm -f "$review_output_file"
  touchstone_emit_event review_blocked pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
  TOUCHSTONE_MERGE_FAILURE_REASON="review-blocked"
  return "$review_rc"
}

load_merge_review_config

# 1. Sanity check the PR exists and is open.
if ! PR_STATE="$(gh pr view "$PR_NUMBER" --json state --jq '.state')"; then
  echo "ERROR: Failed to inspect PR #$PR_NUMBER state with gh." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="pr-state"
  exit 1
fi
if [ "$PR_STATE" != "OPEN" ]; then
  echo "ERROR: PR #$PR_NUMBER is not open (state: $PR_STATE)." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="pr-not-open"
  exit 1
fi

# 2. Check mergeability with retries (GitHub's status can lag after a push).
echo "==> Checking merge state for PR #$PR_NUMBER ..."
STATE=""
MERGEABLE=""
MERGE_STATE_RETRY_DELAYS=(1 2 5 10 30 30 30 30 30)
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  MERGE_STATE="$(gh pr view "$PR_NUMBER" --json mergeStateStatus,mergeable --template '{{.mergeStateStatus}} {{.mergeable}}' 2>/dev/null || echo '')"
  STATE="${MERGE_STATE%% *}"
  MERGEABLE="${MERGE_STATE#* }"
  [ -n "$STATE" ] || STATE="UNKNOWN"
  [ -n "$MERGEABLE" ] || MERGEABLE="UNKNOWN"
  echo "    attempt $attempt: mergeStateStatus=$STATE mergeable=$MERGEABLE"
  if [ "$STATE" = "CLEAN" ] && [ "$MERGEABLE" = "MERGEABLE" ]; then
    break
  fi
  FAILED_CHECKS="$(failed_checks)"
  if [ -n "$FAILED_CHECKS" ]; then
    print_failed_checks_and_exit "$FAILED_CHECKS"
  fi
  if [ "$MERGEABLE" = "CONFLICTING" ] || [ "$STATE" = "DIRTY" ] || [ "$STATE" = "BEHIND" ] || [ "$STATE" = "CONFLICTING" ]; then
    echo "ERROR: PR #$PR_NUMBER is $STATE — has conflicts or is out of date with base." >&2
    echo "       Final merge state: mergeStateStatus=$STATE mergeable=$MERGEABLE." >&2
    echo "       Rebase or resolve conflicts on the PR branch before merging." >&2
    TOUCHSTONE_MERGE_FAILURE_REASON="not-mergeable"
    exit 1
  fi
  if [ "$attempt" -lt 10 ]; then
    sleep_seconds="${MERGE_STATE_RETRY_DELAYS[$((attempt - 1))]}"
    # Tests may set MERGE_PR_SLEEP_OVERRIDE=0 to exercise retry behavior
    # without waiting for the production backoff schedule.
    if [ -n "${MERGE_PR_SLEEP_OVERRIDE+x}" ]; then
      sleep_seconds="$MERGE_PR_SLEEP_OVERRIDE"
    fi
    sleep "$sleep_seconds"
  fi
done

if [ "$STATE" != "CLEAN" ] || [ "$MERGEABLE" != "MERGEABLE" ]; then
  echo "ERROR: PR #$PR_NUMBER is not cleanly mergeable (state=$STATE mergeable=$MERGEABLE)." >&2
  echo "       Inspect manually: gh pr view $PR_NUMBER --web" >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="not-mergeable"
  exit 1
fi

# 3. Run AI review as the merge gate.
run_merge_review

# 4. Squash-merge and delete the branch.
echo "==> Squash-merging PR #$PR_NUMBER ..."
if [ -z "$REVIEWED_HEAD_OID" ]; then
  echo "ERROR: Cannot merge PR #$PR_NUMBER because no reviewed head commit was recorded." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="missing-reviewed-head"
  exit 1
fi
gh_merge_exit=0
if [ "$BYPASS_REVIEW" = true ]; then
  gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_OID" \
    --body "Reviewer-bypass: $BYPASS_REASON" || gh_merge_exit=$?
else
  gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_OID" \
    || gh_merge_exit=$?
fi

# `gh pr merge --delete-branch` does the squash AND tries to delete the
# local feature branch. The local-delete fails when the branch is checked
# out in the current worktree (the common case for parallel-worktree work).
# When that happens, the remote merge succeeded server-side — only the
# local cleanup didn't. Verify by asking the API; if MERGED, treat as
# success with a warning so the script doesn't claim the PR failed.
if [ "$gh_merge_exit" -ne 0 ]; then
  pr_state="$(gh pr view "$PR_NUMBER" --json state --jq '.state' 2>/dev/null || echo "")"
  if [ "$pr_state" = "MERGED" ]; then
    echo "WARNING: gh pr merge exited $gh_merge_exit, but PR #$PR_NUMBER is MERGED on GitHub."
    echo "         Likely cause: local feature branch is checked out in a worktree,"
    echo "         or stale worktree metadata still records it there. Remote branch is gone."
    echo "         Use 'git worktree remove <path>' or 'bash scripts/cleanup-worktrees.sh --execute' for normal cleanup."
    echo "         If the directory was deleted directly, run 'git worktree prune' from a remaining checkout."
  else
    echo "ERROR: gh pr merge exited $gh_merge_exit and PR #$PR_NUMBER is not MERGED." >&2
    TOUCHSTONE_MERGE_FAILURE_REASON="gh-pr-merge"
    exit "$gh_merge_exit"
  fi
fi

MERGED_AT="$(gh pr view "$PR_NUMBER" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || echo "")"
touchstone_emit_event merged pr_number="$PR_NUMBER" merged_at="$MERGED_AT" head_sha="$REVIEWED_HEAD_OID"
post_clean_review_comment "$REVIEW_SUMMARY_FILE"
post_findings_history_comment "$PR_HEAD_BRANCH"

# Record squash-merge metadata for cleanup-branches.sh. The merge has
# succeeded on GitHub; this is best-effort persistence for later cleanup.
SQUASH_COMMIT_OID="$(gh pr view "$PR_NUMBER" --json mergeCommit --jq '.mergeCommit.oid' 2>/dev/null || echo "")"
record_squash_merge "$PR_HEAD_BRANCH" "$PR_NUMBER" "$REVIEWED_HEAD_OID" "$SQUASH_COMMIT_OID"

# 5. Sync local default branch.
sync_default_branch_after_merge

# 6. Cortex post-merge hook (T1.9). Fires only when the project meets the
# activation criteria documented in scripts/cortex-pr-merged-hook.sh.
# Activation is the hook's job — we always invoke and let it self-gate.
# The hook may produce a follow-up journal branch/PR; the journal commit
# is created with --no-verify so it doesn't recurse through this script's
# review gates. Failures inside the hook surface as visible stderr; we
# don't fail the overall merge over a journal-write hiccup.
CORTEX_HOOK_SCRIPT=""
for candidate_hook in \
  "$SCRIPT_DIR/cortex-pr-merged-hook.sh" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/scripts/cortex-pr-merged-hook.sh"; do
  if [ -n "$candidate_hook" ] && [ -f "$candidate_hook" ]; then
    CORTEX_HOOK_SCRIPT="$candidate_hook"
    break
  fi
done

if [ -n "$CORTEX_HOOK_SCRIPT" ]; then
  hook_status=0
  TOUCHSTONE_MERGED_PR="$PR_NUMBER" bash "$CORTEX_HOOK_SCRIPT" || hook_status=$?
  if [ "$hook_status" -ne 0 ]; then
    echo "WARNING: cortex-pr-merged-hook exited $hook_status (see above)." >&2
    echo "         The PR merged cleanly; only the auto-draft journal step had a problem." >&2
  fi
fi

cleanup_local_pr_branch_after_merge

echo "==> Done."
