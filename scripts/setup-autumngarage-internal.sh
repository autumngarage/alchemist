#!/usr/bin/env bash
#
# Seed the GitHub label surface Alchemist needs for internal Autumn Garage use.
#
# Default mode is a dry-run. Pass --execute to mutate GitHub labels.
#
# Usage:
#   bash scripts/setup-autumngarage-internal.sh
#   bash scripts/setup-autumngarage-internal.sh --execute
#   bash scripts/setup-autumngarage-internal.sh --execute --dispatch-only
#   bash scripts/setup-autumngarage-internal.sh --repos autumngarage/alchemist,autumngarage/cortex
#
set -euo pipefail

DRY_RUN=1
INCLUDE_TEST_LABELS=1

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
    --dispatch-only)
      INCLUDE_TEST_LABELS=0
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

transition_label() {
  local prefix="$1"
  local state="$2"

  case "$state" in
    dispatch)
      printf '%s' "$prefix"
      ;;
    working)
      printf '%s' "${prefix/-dispatch/-working}" | sed 's/-test$/-test-working/'
      ;;
    shipped)
      printf '%s' "${prefix/-dispatch/-shipped}" | sed 's/-test$/-test-shipped/'
      ;;
    declined)
      printf '%s' "${prefix/-dispatch/-declined}" | sed 's/-test$/-test-declined/'
      ;;
    error)
      printf '%s' "${prefix/-dispatch/-error}" | sed 's/-test$/-test-error/'
      ;;
    *)
      echo "ERROR: unknown label state '$state'" >&2
      return 1
      ;;
  esac
}

ensure_prefix() {
  local repo="$1"
  local prefix="$2"
  local name

  name="$(transition_label "$prefix" dispatch)"
  run_or_print gh label create "$name" \
    --repo "$repo" \
    --color ffd787 \
    --description "Dispatched to Alchemist for transmutation" \
    --force

  name="$(transition_label "$prefix" working)"
  run_or_print gh label create "$name" \
    --repo "$repo" \
    --color fff5d7 \
    --description "Alchemist actively working" \
    --force

  name="$(transition_label "$prefix" shipped)"
  run_or_print gh label create "$name" \
    --repo "$repo" \
    --color d7ffd7 \
    --description "Alchemist shipped a PR" \
    --force

  name="$(transition_label "$prefix" declined)"
  run_or_print gh label create "$name" \
    --repo "$repo" \
    --color d7d7ff \
    --description "Alchemist reviewed and declined to make changes" \
    --force

  name="$(transition_label "$prefix" error)"
  run_or_print gh label create "$name" \
    --repo "$repo" \
    --color ffd7d7 \
    --description "Alchemist hit an error" \
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
  ensure_prefix "$repo" alchemist-dispatch
  if [ "$INCLUDE_TEST_LABELS" -eq 1 ]; then
    ensure_prefix "$repo" alchemist-test
  fi
done

echo "==> Done"
