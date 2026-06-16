#!/usr/bin/env bash
#
# setup.sh - one-command local setup for Alchemist.
set -euo pipefail

DEPS_ONLY=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --deps-only)
      DEPS_ONLY=true
      shift
      ;;
    -h | --help)
      cat <<'EOF'
Usage: bash setup.sh [--deps-only]

  --deps-only    Sync Python dependencies only.
EOF
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv is required. Install it from https://docs.astral.sh/uv/" >&2
  exit 2
fi

echo "==> Syncing dependencies"
uv sync --all-extras

if [ "$DEPS_ONLY" = true ]; then
  exit 0
fi

if command -v pre-commit >/dev/null 2>&1; then
  echo "==> Installing pre-commit hooks"
  pre-commit install --install-hooks
  pre-commit install --hook-type pre-push
else
  echo "==> pre-commit not installed; skipping hook install"
fi

echo "==> Validating checkout"
bash scripts/validate.sh

echo "==> Setup complete"
