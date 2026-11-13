"""
Replay recorded trading days through the real fill monitor, scan and flatten
code against a simulated exchange (tests/_exchange.py), checking safety
invariants every tick. Usage (local; candles.json.gz is not in git):

    python3 indstocks/research/replay.py [days] [--chaos 0.1] [--seed N]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "tests"))
sys.path.insert(0, HERE)

import _fake  # noqa: E402,F401  (sandbox env before anything imports common)
from _fake import reset  # noqa: E402

LIVE_LIKE = {
    "TRADER_DECIDER": "claude", "TRADER_STYLE": "aggressive", "TRADER_ALLOW_SHORT": "true",
    "TRADER_MAX_LEVERAGE": "5.0", "TRADER_MAX_CONCURRENT": "3", "TRADER_MIN_RR": "2.0",
    "TRADER_MAX_TRADES": "15", "TRADER_MAX_TRADES_HARD": "25", "TRADER_MAX_ENTRIES_PER_SYMBOL": "3",
    "TRADER_MAX_RESTING": "8", "TRADER_LAST_ENTRY": "14:45", "TRADER_MIN_POSITION_VALUE": "15000",
    "TRADER_MIN_NET_TARGET": "100", "TRADER_DIRECTION_GATE": "enforce", "TRADER_USE_BOOK": "false",
    "TRADER_SETUPS": "orb,failed_break,day_break", "TRADER_TARGET_R": "4",
    "TRADER_NO_ENTRY_WINDOWS": "12:00-13:00", "TRADER_STOP_AFTER_LOSSES": "3", "TRADER_FLIP": "true",
    "TRADER_MAX_LOSS": "600", "TRADER_DAILY_LOSS": "2500", "TRADER_MIN_ORDER_VALUE": "5000",
    "TRADER_RESTING_TTL_MIN": "45", "TRADER_STOP_BAND_PCT": "0.5", "TRADER_MARKETABLE_PCT": "0.3",
}


def load(path):
    """Candles by symbol and day (portfolio_sim.load, without its sandbox env)."""
    import collections
    import gzip
    import json
    from datetime import datetime
    from decimal import Decimal as D
    import session as sess
    from market import Candle
    d = json.load(gzip.open(path))
    out = {}
    for s, v in d["symbols"].items():
        byday = collections.defaultdict(list)
        for ts, o, h, l, c, vol in v["candles"]:
            t = datetime.fromisoformat(ts)
            t = t if t.tzinfo else t.replace(tzinfo=sess.IST)
            byday[t.date()].append(Candle(t, D(o), D(h), D(l), D(c), int(vol), 5))
        out[s] = {"tick": D(v["tick"]), "days": {k: sorted(x, key=lambda c: c.opens_at) for k, x in byday.items()}}
    return out


def main(argv):
    import common  # noqa: F401  (paths from the sandbox)
    from _exchange import run_day
    n = int(argv[0]) if argv and argv[0].isdigit() else 3
    chaos = float(argv[argv.index("--chaos") + 1]) if "--chaos" in argv else 0.0
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 1
    data = load(os.path.join(HERE, "candles.json.gz"))
    days = sorted(set.intersection(*[set(v["days"]) for v in data.values()]))[-n:]
    syms = sorted(data)
    tot_v = tot_e = 0
    for i, d in enumerate(days):
        reset(TRADER_SYMBOLS=",".join(syms), **LIVE_LIKE)
        t0 = time.time()
        r = run_day({s: data[s]["days"][d] for s in syms}, {s: data[s]["tick"] for s in syms},
                    seed=seed + i, chaos=chaos)
        tot_v += len(r["violations"]); tot_e += len(r["errors"])
        print(f"{r['day']}  orders {r['orders']:3d}  gross {r['gross']:+8.2f}  violations {len(r['violations'])}  "
              f"errors {len(r['errors'])}  rejects {len(r['rejects'])}  ({time.time() - t0:.0f}s)", flush=True)
        for v in r["violations"][:12]:
            print("   V", v)
        for e in r["errors"][:4]:
            print("   E", e[:900])
        seen = set()
        for x in r["rejects"]:
            k = x.split(": ", 1)[-1][:60]
            if k not in seen:
                seen.add(k)
                print("   R", x[:140])
    print(f"TOTAL violations {tot_v} errors {tot_e}")


if __name__ == "__main__":
    main(sys.argv[1:])
