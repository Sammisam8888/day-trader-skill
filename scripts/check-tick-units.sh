#!/usr/bin/env bash
# Settle what unit the instruments master publishes TICK_SIZE in.
#
# RELIANCE came back as TICK_SIZE=10. RELIANCE does not move in ten-rupee
# steps, so the column is not rupees -- but the order builder currently
# treats it as rupees, which would round a 358.32 stop to 360.
#
# The docs give a known anchor: a CRUDEOIL futures contract with "a tick size
# of Rs 1", rejected with "Limit Price should be multiple of 1.00". So:
#   CRUDEOIL prints 100  -> the column is paise, divide by 100
#   CRUDEOIL prints 1    -> the column is rupees and the equity values are real
#
# Read-only. Places nothing.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - <<'PY'
import collections
import client, instruments

c = client.Client()

eq = instruments.Instruments(client=c, source="equity").load()
counts = collections.Counter(r.tick_size for r in eq._rows)
print("equity TICK_SIZE distribution (%d instruments):" % len(eq))
for tick, n in sorted(counts.items()):
    print("   %-10s %6d instruments" % (tick, n))

print()
try:
    mcx = instruments.Instruments(client=c, source="mcx").load()
    hits = [r for r in mcx._rows if r.symbol_name.startswith("CRUDEOIL")][:3]
    if hits:
        print("CRUDEOIL rows (docs say this contract ticks at Rs 1):")
        for r in hits:
            print("   %-24s TICK_SIZE=%s  LOT_UNITS=%s" %
                  (r.trading_symbol, r.tick_size, r.lot_units))
        t = hits[0].tick_size
        print()
        if t == 100:
            print("VERDICT: paise. Divide TICK_SIZE by 100 before using it as a price.")
        elif t == 1:
            print("VERDICT: rupees. The equity values above are literal.")
        else:
            print("VERDICT: inconclusive -- CRUDEOIL reads %s, expected 1 or 100." % t)
            print("Do not place an order until this is settled.")
    else:
        print("no CRUDEOIL rows found in the mcx master")
except Exception as exc:
    print("mcx master unavailable (%s)" % exc)
    print("MCX may not be enabled on this account; that is fine.")
    print("Fallback: the equity distribution above decides it. A set of small")
    print("integers (1/5/10/25) means paise; values like 0.01/0.05 mean rupees.")
PY
