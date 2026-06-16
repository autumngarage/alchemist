#!/usr/bin/env bash
#
# Run the deterministic checks for this Python package.
set -euo pipefail

echo "==> ruff check ."
uv run ruff check .

echo "==> pytest"
uv run pytest
