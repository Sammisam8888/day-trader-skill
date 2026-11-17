#!/usr/bin/env bash
# Write candles (and tick sizes) for the last N calendar days to stdout as
# gzipped JSON. Read-only market data; no account data. Needs today's login.
#   export-candles.sh DAYS [INTERVAL_MIN] [live|fno]
#   live: the trading list (TRADER_SYMBOLS); fno: scripts/fno-universe.txt
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
set -a; . /etc/indstocks.env; set +a
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../indstocks"
python3 - "${1:-100}" "${2:-5}" "${3:-live}" "$HERE/fno-universe.txt" <<'PY' | gzip -c
import json, os, sys
from datetime import timedelta
import session as sess
from shoonya import Shoonya
b = Shoonya()
days, interval, which, fno_file = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
if interval not in (1, 3, 5, 15):
    interval = 5
end = sess.now(); start = end - timedelta(days=days)
if which == "fno":
    syms = [l.strip().upper() for l in open(fno_file) if l.strip() and not l.startswith("#")]
else:
    syms = [s.strip().upper() for s in os.environ.get("TRADER_SYMBOLS", "NIFTYBEES").split(",") if s.strip()]
for s in ("NIFTYBEES", "BANKBEES"):
    if s not in syms:
        syms.append(s)
step = timedelta(days=7 if interval >= 5 else 3)
out = {"exported": end.isoformat(), "interval": interval, "symbols": {}}
for s in syms:
    try:
        inst = b.instrument(s)
    except Exception as e:                       # noqa: BLE001
        print("skip", s, e, file=sys.stderr)
        continue
    rows, t0 = [], start
    while t0 < end:
        t1 = min(t0 + step, end)
        try:
            for c in b.candles(inst.token, t0, t1, interval=interval):
                rows.append([c.opens_at.isoformat(), str(c.open), str(c.high), str(c.low), str(c.close), c.volume])
        except Exception as e:                   # noqa: BLE001
            print("gap", s, t0.date(), e, file=sys.stderr)
        t0 = t1
    rows = sorted({r[0]: r for r in rows}.values())
    out["symbols"][s] = {"tick": str(inst.tick), "token": inst.token, "candles": rows}
    print(s, len(rows), file=sys.stderr)
print(json.dumps(out, separators=(",", ":")))
PY
