#!/usr/bin/env bash
# Why did 12 of 19 symbols return no candles?
# Prints what each watchlist symbol resolved to, and whether candles came back.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - "$@" <<'PY'
import sys
from datetime import date
import session as sess, instruments as inst_mod
from bars import Bars, scrip_code
from client import Client
from dryrun import load_watchlist

day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else sess.previous_trading_days(date.today(), 1)[0]
s = sess.Session(day)
c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()

print(f"resolving against {len(m)} instruments, candles for {day}\n")
print(f"{'symbol':<13}{'exch':<6}{'series':<8}{'sec_id':<10}{'code':<14}candles")
print("-" * 62)

rows = {}
for sym in load_watchlist("watchlist.txt"):
    try:
        i = m.resolve(sym)
    except KeyError:
        print(f"{sym:<13}{'-':<6}{'-':<8}{'NOT IN MASTER':<24}-")
        continue
    rows[sym] = i

bars = Bars(c, "5minute")
codes = {scrip_code(i): sym for sym, i in rows.items()}
got = bars.fetch(list(codes), s.open, s.close)

bad = []
for sym, i in rows.items():
    code = scrip_code(i)
    n = len(got.get(code, []))
    flag = "" if n else "   <-- NO DATA"
    print(f"{sym:<13}{i.exchange:<6}{i.series:<8}{i.security_id:<10}{code:<14}{n}{flag}")
    if not n:
        bad.append((sym, i))

if bad:
    print("\nFor each symbol with no data, every row the master holds:")
    for sym, _ in bad:
        print(f"\n  {sym}:")
        for r in m._rows:
            if sym in (r.trading_symbol, r.symbol_name):
                print(f"    exch={r.exchange:<5} series={r.series:<5} id={r.security_id:<10} "
                      f"tsym={r.trading_symbol:<16} tick={r.tick_size}")
PY
