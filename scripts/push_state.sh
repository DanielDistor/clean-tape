#!/usr/bin/env bash
# Bootstrap/rebuild the `data` branch from a local state directory.
# Use when Yahoo blocks GitHub Actions IPs for the big backfill:
#   make bootstrap            # backfill into ./state on your laptop
#   make run                  # optional: one full run so the site/report exist
#   scripts/push_state.sh     # force-push ./state as a single commit on `data`
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f state/curated/observations.parquet ]; then
  echo "state/ is empty — run 'make bootstrap' first" >&2
  exit 1
fi
uv run cleantape state push --message "bootstrap from $(hostname -s) $(date -u +%Y-%m-%dT%H:%MZ)"
echo "done: origin/data now holds ./state. Next: run the 'daily' workflow (Actions tab → daily → Run workflow)."
