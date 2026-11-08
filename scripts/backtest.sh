#!/usr/bin/env bash
# Replay the opening-range rules on real NSE 5-minute candles from Shoonya,
# breakout vs retest entry. Read-only. Needs today's Shoonya login.
# Usage: sudo bash scripts/backtest.sh [days] [HH:MM last entry] [SYMBOLS]
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
DAYS="${1:-40}"; CUT="${2:-12:30}"; SYMS="${3:-${TRADER_SYMBOLS:-NIFTYBEES}}"
python3 - "$DAYS" "$CUT" "$SYMS" <<'PY'
import sys
from datetime import time
import backtest
from shoonya import Shoonya
days = int(sys.argv[1]); hh, mm = sys.argv[2].split(":")
syms = [s.strip().upper() for s in sys.argv[3].split(",") if s.strip()]
print(backtest.report(backtest.run(Shoonya(), syms, days, time(int(hh), int(mm)))))
PY
