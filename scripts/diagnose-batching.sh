#!/usr/bin/env bash
# Batched vs single-code candle requests, same window, same day.
# If single works where batched does not, the bug is in how we batch --
# not in the symbols.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - "$@" <<'PY'
import json, sys, time
from datetime import date
import session as sess, instruments as inst_mod
from bars import Bars, scrip_code
from client import Client
from dryrun import load_watchlist

day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else sess.previous_trading_days(date.today(), 1)[0]
s = sess.Session(day)
c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()
bars = Bars(c, "5minute")

syms = [x for x in load_watchlist("watchlist.txt")][:10]
rows = {}
for sym in syms:
    try:
        rows[sym] = m.resolve(sym)
    except KeyError:
        pass

codes = [scrip_code(i) for i in rows.values()]
code_to_sym = {scrip_code(i): s2 for s2, i in rows.items()}

print(f"day {day}\n")
batched = bars.fetch(codes, s.open, s.close)

print(f"{'symbol':<13}{'code':<14}{'batched':<10}single")
print("-" * 48)
for code in codes:
    n_batch = len(batched.get(code, []))
    single = bars.fetch([code], s.open, s.close)
    n_single = len(single.get(code, []))
    flag = "  <-- batching loses it" if (n_single and not n_batch) else ""
    print(f"{code_to_sym[code]:<13}{code:<14}{n_batch:<10}{n_single}{flag}")
    time.sleep(0.3)

print("\nRaw response for one failing code, 5-code batch:")
probe = codes[:5]
raw = c.get("/market/historical/5minute", params={
    "scrip-codes": ",".join(probe),
    "start_time": sess.epoch_ms(s.open),
    "end_time": sess.epoch_ms(s.close)})
print("  requested:", probe)
print("  keys returned:", list((raw.get("data") or {}).keys()))
print("  success flag:", raw.get("success"))
for k, v in (raw.get("data") or {}).items():
    print(f"    {k}: {len(v.get('candles') or [])} candles")
PY
