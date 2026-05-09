#!/usr/bin/env bash
# scripts/railway-entrypoint.sh — auto-update tools and run alchemist tick.
set -euo pipefail

echo "--- Tool Refresh ---"

# Verify environment
if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "FATAL: GITHUB_TOKEN not set" >&2
  exit 1
fi

# 1. Update Touchstone
if [ -d "/opt/touchstone/.git" ]; then
  echo "Updating Touchstone (main)..."
  if ! git -C /opt/touchstone fetch origin main; then
    echo "  ! Touchstone fetch failed"
    # Report but don't exit; we can still run with the existing version
    gh issue create --repo autumngarage/alchemist --title "Update Failure: Touchstone fetch" --body "Touchstone fetch failed during railway-entrypoint.sh" --label "bug" || true
  else
    git -C /opt/touchstone reset --hard origin/main || {
      echo "FATAL: Touchstone reset failed" >&2
      exit 1
    }
  fi
fi

# 2. Update Conductor
if [ -d "/opt/conductor/.git" ]; then
  echo "Updating Conductor (main)..."
  if ! git -C /opt/conductor fetch origin main; then
    echo "  ! Conductor fetch failed"
    gh issue create --repo autumngarage/alchemist --title "Update Failure: Conductor fetch" --body "Conductor fetch failed during railway-entrypoint.sh" --label "bug" || true
  else
    git -C /opt/conductor reset --hard origin/main || {
      echo "FATAL: Conductor reset failed" >&2
      exit 1
    }
    # Reinstall from updated source to pick up dependency or entrypoint changes
    if ! pipx install --force /opt/conductor --quiet; then
      echo "FATAL: Conductor reinstall failed" >&2
      exit 1
    fi
    # Verify it works
    if ! conductor --version >/dev/null 2>&1; then
      echo "FATAL: Conductor broken after update" >&2
      exit 1
    fi
  fi
fi

# 3. Ensure uv is at a specific version (fast, keeps the resolver current)
pip install "uv==0.5.11" --quiet || {
  echo "FATAL: uv install failed" >&2
  exit 1
}

echo "--- Starting Alchemist Tick ---"
# alchemist run-once handles its own GitHub App token minting internally.
exec alchemist run-once --json
