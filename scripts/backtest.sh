#!/usr/bin/env bash
# Replay the opening-range rules on real NSE 5-minute data, breakout vs
# retest entry. Read-only. Usage: sudo bash scripts/backtest.sh [days] [HH:MM]
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
DAYS="${1:-40}"; CUT="${2:-12:30}"
python3 - "$DAYS" "$CUT" <<'PY'
import sys
from datetime import time
import backtest, instruments as inst_mod
from client import Client
from dryrun import load_watchlist
days = int(sys.argv[1]); hh, mm = sys.argv[2].split(":")
c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()
out = backtest.run(c, m, load_watchlist("watchlist.txt"), days, time(int(hh), int(mm)))
print(backtest.report(out))
PY
