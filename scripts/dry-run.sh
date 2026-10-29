#!/usr/bin/env bash
# The whole pipeline against live data. Places nothing.
#   sudo bash scripts/dry-run.sh                 # today, or the last trading day
#   sudo bash scripts/dry-run.sh --date 2026-10-29 --at 10:15
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
exec python3 "$(dirname "$0")/../indstocks/dryrun.py" "$@"
