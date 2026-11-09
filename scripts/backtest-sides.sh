#!/usr/bin/env bash
# Opening-range triggers on real Shoonya 5-minute candles, three ways:
# long only, both sides, both sides with the live direction gate.
# Read-only. Needs today's Shoonya login.
# Usage: sudo bash scripts/backtest-sides.sh [days] [HH:MM last entry] [SYMBOLS]
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
print(backtest.report_sides(backtest.run_sides(Shoonya(), syms, days, time(int(hh), int(mm)))))
PY
