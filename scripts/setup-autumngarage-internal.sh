#!/usr/bin/env bash
#
# Seed the GitHub label surface Alchemist needs for internal Autumn Garage use.
#
# Default mode is a dry-run. Pass --execute to mutate GitHub labels.
#
# Usage:
#   bash scripts/setup-autumngarage-internal.sh
#   bash scripts/setup-autumngarage-internal.sh --execute
#   bash scripts/setup-autumngarage-internal.sh --repos autumngarage/alchemist,autumngarage/cortex
#
set -euo pipefail

DRY_RUN=1
INTAKE_LABEL=agent-ready
STATE_PREFIX=alchemist

REPOS=(
  autumngarage/alchemist
  autumngarage/conductor
  autumngarage/cortex
  autumngarage/sentinel
  autumngarage/touchstone
  autumngarage/autumn-garage
)

usage() {
  sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute)
      DRY_RUN=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --repos)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --repos expects a comma-separated repo list" >&2
        exit 1
      fi
      IFS=',' read -r -a REPOS <<<"$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      exit 1
      ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh is required" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated" >&2
  exit 1
fi

print_cmd() {
  printf 'DRY-RUN:'
  printf ' %q' "$@"
  printf '\n'
}

run_or_print() {
  if [ "$DRY_RUN" -eq 1 ]; then
    print_cmd "$@"
    return 0
  fi
  "$@"
}

ensure_prefix() {
  local repo="$1"
  local prefix="$2"

  run_or_print gh label create "$INTAKE_LABEL" \
    --repo "$repo" \
    --color ffd787 \
    --description "Eligible for Alchemist agent dispatch" \
    --force

  run_or_print gh label create "$prefix-dispatched" \
    --repo "$repo" \
    --color fff5d7 \
    --description "Alchemist dispatched issue to external agent" \
    --force

  run_or_print gh label create "$prefix-pr-open" \
    --repo "$repo" \
    --color d7f0ff \
    --description "Alchemist found an agent PR and is watching it" \
    --force

  run_or_print gh label create "$prefix-shipped" \
    --repo "$repo" \
    --color d7ffd7 \
    --description "Alchemist saw the PR merge" \
    --force

  run_or_print gh label create "$prefix-blocked" \
    --repo "$repo" \
    --color cfd7ff \
    --description "Alchemist blocked for human triage" \
    --force

  run_or_print gh label create "$prefix-error" \
    --repo "$repo" \
    --color ffd7d7 \
    --description "Alchemist coordinator error" \
    --force
}

echo "==> Alchemist internal Autumn Garage label setup"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "==> Mode: dry-run. Pass --execute to mutate GitHub labels."
else
  echo "==> Mode: execute"
fi

for repo in "${REPOS[@]}"; do
  repo="${repo#"${repo%%[![:space:]]*}"}"
  repo="${repo%"${repo##*[![:space:]]}"}"
  [ -n "$repo" ] || continue

  echo "==> $repo"
  ensure_prefix "$repo" "$STATE_PREFIX"
done

echo "==> Done"
