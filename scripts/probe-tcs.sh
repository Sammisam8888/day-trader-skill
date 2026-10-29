#!/usr/bin/env bash
# One failing symbol, every variation, to isolate which dimension is wrong:
# the id, the exchange prefix, the interval, the date, or historical coverage.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - "$@" <<'PY'
import json, sys
from datetime import date, timedelta
import session as sess, instruments as inst_mod
from client import Client

SYM = (sys.argv[1] if len(sys.argv) > 1 else "TCS").upper()
WORKS = "RELIANCE"
day = sess.previous_trading_days(date.today(), 1)[0]
s = sess.Session(day)
c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()

rows = [r for r in m._rows if SYM in (r.trading_symbol, r.symbol_name)]
good = m.resolve(WORKS)
print(f"{SYM}: {len(rows)} rows in the master")
for r in rows:
    print(f"   exch={r.exchange:<4} seg={r.segment:<4} series={r.series:<4} "
          f"id={r.security_id:<9} tsym={r.trading_symbol:<14} isin={r.isin}")
print(f"{WORKS} (works): exch={good.exchange} seg={good.segment} "
      f"series={good.series} id={good.security_id} isin={good.isin}\n")

def hist(code, interval="5minute", start=None, end=None):
    try:
        r = c.get(f"/market/historical/{interval}", params={
            "scrip-codes": code,
            "start_time": sess.epoch_ms(start or s.open),
            "end_time": sess.epoch_ms(end or s.close)})
        n = len((r.get("data", {}).get(code) or {}).get("candles") or [])
        return f"{n} candles"
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {str(exc)[:90]}"

print("historical, varying one thing at a time:")
for r in rows:
    code = f"{r.exchange}_{r.security_id}"
    for interval in ("5minute", "1minute", "day"):
        print(f"   {code:<14} {interval:<9} {hist(code, interval)}")

week = s.open - timedelta(days=7)
first = rows[0]
c0 = f"{first.exchange}_{first.security_id}"
print(f"   {c0:<14} {'5m/7day':<9} {hist(c0, '5minute', week, s.close)}")

print("\nlive quote (different endpoint, same id):")
for r in rows:
    code = f"{r.exchange}_{r.security_id}"
    try:
        q = c.get("/market/quotes/ltp", params={"scrip-codes": code})
        print(f"   {code:<14} {json.dumps(q)[:160]}")
    except Exception as exc:
        print(f"   {code:<14} ERROR {str(exc)[:110]}")
PY
