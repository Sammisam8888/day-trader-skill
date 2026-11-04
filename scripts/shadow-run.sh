#!/usr/bin/env bash
# One shadow run: decide as if trading, place nothing, write it down.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
exec python3 "$(dirname "$0")/../indstocks/shadow.py"
