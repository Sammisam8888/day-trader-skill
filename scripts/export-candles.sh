#!/usr/bin/env bash
# Write the trading list's 5-minute candles (and tick sizes) for the last N
# calendar days to stdout as gzipped JSON. Read-only market data; no account
# data. Needs today's Shoonya login.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
python3 - "${1:-100}" <<'PY' | gzip -c
import json, os, sys
from datetime import timedelta
import session as sess
from shoonya import Shoonya
b = Shoonya()
days = int(sys.argv[1])
end = sess.now(); start = end - timedelta(days=days)
syms = [s.strip().upper() for s in os.environ.get("TRADER_SYMBOLS", "NIFTYBEES").split(",") if s.strip()]
out = {"exported": end.isoformat(), "symbols": {}}
for s in syms:
    inst = b.instrument(s)
    rows, t0 = [], start
    while t0 < end:
        t1 = min(t0 + timedelta(days=7), end)
        for c in b.candles(inst.token, t0, t1, interval=5):
            rows.append([c.opens_at.isoformat(), str(c.open), str(c.high), str(c.low), str(c.close), c.volume])
        t0 = t1
    rows = sorted({r[0]: r for r in rows}.values())
    out["symbols"][s] = {"tick": str(inst.tick), "token": inst.token, "candles": rows}
    print(s, len(rows), file=sys.stderr)
print(json.dumps(out))
PY
