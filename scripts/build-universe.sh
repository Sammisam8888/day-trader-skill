#!/usr/bin/env bash
# Rank the F&O universe by how far it actually moves intraday, and write the
# top names to watchlist.txt.
#
#   sudo bash scripts/build-universe.sh            # report only
#   sudo bash scripts/build-universe.sh --write    # also rewrite watchlist
#
# Read-only against the broker. Places nothing.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
exec python3 build_universe.py "$@"
