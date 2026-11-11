#!/usr/bin/env bash
# The playbook, setup by setup and side by side, on real Shoonya 5-minute
# candles. Read-only. Needs today's Shoonya login.
# Usage: sudo bash scripts/backtest-setups.sh [days] [HH:MM last entry] [notional]
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
DAYS="${1:-40}"; CUT="${2:-${TRADER_LAST_ENTRY:-14:45}}"; NOTIONAL="${3:-26000}"
python3 - "$DAYS" "$CUT" "$NOTIONAL" <<'PY'
import sys, os
from datetime import time
from decimal import Decimal
import backtest
from shoonya import Shoonya
days = int(sys.argv[1]); hh, mm = sys.argv[2].split(":")
syms = [s.strip().upper() for s in os.environ.get("TRADER_SYMBOLS", "NIFTYBEES").split(",") if s.strip()]
print(backtest.report_setups(backtest.run_setups(Shoonya(), syms, days, time(int(hh), int(mm)), Decimal(sys.argv[3]))))
PY
