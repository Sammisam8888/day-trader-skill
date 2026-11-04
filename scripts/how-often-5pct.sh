#!/usr/bin/env bash
# How often does a watchlist stock actually run 5-10% above its open?
#
# A target is only reachable as often as the move that reaches it. This
# measures (high - open) / open on a year of daily candles, which is the
# ceiling on what any intraday long could have captured that day -- a real
# entry comes later than the open and exits before the high, so the true
# hit rate is lower than anything printed here.
#
# Read-only. Places nothing.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - "$@" <<'PY'
import collections
from datetime import timedelta
from decimal import Decimal

import charges, instruments as inst_mod, session as sess
from bars import Bars, scrip_code
from client import Client
from dryrun import load_watchlist

BUCKETS = [Decimal(x) for x in ("1", "2", "3", "5", "10")]

c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()
bars = Bars(c, "1day")

end = sess.now()
start = end - timedelta(days=365)

rows, totals = [], collections.Counter()
days_total = 0

for sym in load_watchlist("watchlist.txt"):
    try:
        inst = m.resolve(sym)
    except KeyError:
        continue
    candles = bars.fetch_one(scrip_code(inst), start, end)
    if not candles:
        continue

    hits = collections.Counter()
    for k in candles:
        if k.open <= 0:
            continue
        up = (k.high - k.open) / k.open * 100
        for b in BUCKETS:
            if up >= b:
                hits[b] += 1
    n = len(candles)
    days_total += n
    for b in BUCKETS:
        totals[b] += hits[b]
    rows.append((sym, n, hits))

print(f"Days where the HIGH was this far above the OPEN ({days_total} stock-days)\n")
head = "symbol".ljust(13) + "days".rjust(6) + "".join(
    f">={b}%".rjust(10) for b in BUCKETS)
print(head)
print("-" * len(head))
for sym, n, hits in rows:
    line = sym.ljust(13) + str(n).rjust(6)
    for b in BUCKETS:
        pct = (Decimal(hits[b]) / n * 100) if n else Decimal(0)
        line += f"{hits[b]} ({pct:.0f}%)".rjust(10)
    print(line)

print("-" * len(head))
line = "ALL".ljust(13) + str(days_total).rjust(6)
for b in BUCKETS:
    pct = (Decimal(totals[b]) / days_total * 100) if days_total else Decimal(0)
    line += f"{totals[b]} ({pct:.1f}%)".rjust(10)
print(line)

print()
print("This is the CEILING, not a hit rate: it measures open to the day's")
print("high. A breakout entry starts above the open and a target fills below")
print("the high, so a tradeable 5% move is rarer than the 5% column suggests.")

entry = Decimal("465.50")
print()
print("Break-even move by buying power (what the gain must clear first):")
for value in (889, 5000, 25000, 50000):
    qty = int(Decimal(value) / entry)
    if qty < 1:
        qty = 1
    print(f"  Rs {value:>7}  ->  {charges.breakeven_pct(entry, qty)}%")
PY
