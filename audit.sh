#!/usr/bin/env bash
# Run the whole audit: sweep the corpus, write a snapshot + an HTML report.
#
# Designed to be run unattended by a Moonlighter cron job:
#   - no interactive prompts, ever
#   - exits non-zero on real failure (no corpus, sweep crash)
#   - a single unparseable transcript line never kills the run
#
# Usage:
#   ./audit.sh                              # audit, deltas vs newest prior snapshot
#   ./audit.sh --out-dir /path/to/snapshots
#   ./audit.sh --previous baselines/snapshot-2026-07-17.json
#   ./audit.sh --compare old.json new.json  # diff two snapshots and exit

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "audit.sh: no python3 on PATH" >&2
  exit 127
fi

# Default deltas to the stored baseline when no prior snapshot exists yet.
args=("$@")
if [[ ! " ${args[*]} " =~ " --previous " && ! " ${args[*]} " =~ " --compare " ]]; then
  shopt -s nullglob
  existing=(out/snapshot-*.json)
  shopt -u nullglob
  if (( ${#existing[@]} == 0 )) && [[ -f baselines/snapshot-2026-07-17.json ]]; then
    args+=(--previous baselines/snapshot-2026-07-17.json)
  fi
fi

exec "$PY" -m friction_audit.cli "${args[@]}"
