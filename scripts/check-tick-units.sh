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

    # The docs name this exact instrument: security_id 565899 is a CRUDEOIL
    # FUTURES contract, lot size 100, tick size Rs 1. Options on crude tick
    # differently, so match the id rather than the symbol.
    anchor = None
    try:
        anchor = mcx.by_id("565899")
    except KeyError:
        futs = [r for r in mcx._rows
                if r.symbol_name.startswith("CRUDEOIL")
                and "FUT" in (r.segment + r.trading_symbol).upper()
                and r.lot_units == 100]
        anchor = futs[0] if futs else None

    if anchor is None:
        print("could not locate the CRUDEOIL futures anchor in the mcx master")
    else:
        print("anchor: %s (id=%s) -- docs say lot 100, tick Rs 1" %
              (anchor.trading_symbol, anchor.security_id))
        print("   master reports TICK_SIZE=%s  LOT_UNITS=%s" %
              (anchor.tick_size, anchor.lot_units))
        print()
        if anchor.tick_size == 100:
            print("VERDICT: PAISE. Divide TICK_SIZE by 100 to get rupees.")
        elif anchor.tick_size == 1:
            print("VERDICT: RUPEES. The equity values above are literal.")
        else:
            print("VERDICT: inconclusive -- reads %s, expected 1 or 100." % anchor.tick_size)
            print("Do not place an order until this is settled.")
except Exception as exc:
    print("mcx master unavailable (%s)" % exc)
    print("MCX may not be enabled on this account; that is fine.")
    print("Fallback: the equity distribution above decides it. A set of small")
    print("integers (1/5/10/25) means paise; values like 0.01/0.05 mean rupees.")
PY
