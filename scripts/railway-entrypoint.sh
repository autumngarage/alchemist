#!/usr/bin/env bash
# scripts/railway-entrypoint.sh - run one pinned-image Alchemist tick.
set -euo pipefail

echo "--- Starting Alchemist Tick ---"
# alchemist run-once handles its own GitHub App token minting internally.
exec alchemist run-once --json
