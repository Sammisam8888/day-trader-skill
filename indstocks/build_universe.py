"""Rank the F&O universe by realised intraday range. Places nothing."""
import argparse
import os
import sys
from datetime import timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges, instruments as inst_mod, session as sess, volatility as vol
from bars import Bars, scrip_code
from client import Client

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--threshold", default="3", help="rank by %% of days clearing this")
    ap.add_argument("--write", action="store_true", help="rewrite watchlist.txt")
    ap.add_argument("--limit", type=int, default=0, help="cap names scanned (testing)")
    args = ap.parse_args()
    threshold = Decimal(args.threshold)

    c = Client()
    print("loading masters...")
    equity = inst_mod.Instruments(client=c, source="equity").load()
    fno = inst_mod.Instruments(client=c, source="fno").load()

    names = vol.fno_underlyings(fno)
    resolved = {}
    for n in names:
        try:
            resolved[n] = equity.resolve(n)
        except KeyError:
            continue
    if args.limit:
        resolved = dict(list(resolved.items())[: args.limit])

    print(f"{len(resolved)} F&O underlyings resolved to cash instruments")
    print("fetching a year of daily candles (this takes a few minutes)...\n")

    bars = Bars(c, "1day")
    end = sess.now()
    start = end - timedelta(days=365)

    codes = {scrip_code(i): s for s, i in resolved.items()}
    got = bars.fetch(list(codes), start, end)

    profiles = []
    for code, candles in got.items():
        if candles:
            profiles.append(vol.profile(codes[code], candles))

    ranked = vol.rank(profiles, threshold=threshold)
    print(f"{len(profiles)} with data, {len(ranked)} passing liquidity and history\n")

    head = ("symbol".ljust(14) + "days".rjust(6) + "med up".rjust(9)
            + "med rng".rjust(9) + "p90 up".rjust(9)
            + "".join(f">={t}%".rjust(8) for t in vol.DEFAULT_THRESHOLDS))
    print(head)
    print("-" * len(head))
    for p in ranked[: args.top]:
        line = (p.symbol.ljust(14) + str(p.days).rjust(6)
                + f"{p.median_up}%".rjust(9) + f"{p.median_range}%".rjust(9)
                + f"{p.p90_up}%".rjust(9))
        for t in vol.DEFAULT_THRESHOLDS:
            line += f"{p.hits(t)}%".rjust(8)
        print(line)

    if ranked:
        best = ranked[0]
        print()
        print(f"Best name is {best.symbol}: a typical day runs "
              f"{best.median_up}% above the open, and {best.hits(Decimal('5'))}% "
              f"of days clear 5%.")
        print("That column is a CEILING -- open to the day's high. A breakout")
        print("entry is above the open and a target fills below the high, so")
        print("the capturable rate is lower.")

    print()
    print("Break-even by buying power, at a Rs 465 share price:")
    for value in (889, 25000, 50000):
        qty = max(int(Decimal(value) / Decimal("465.50")), 1)
        print(f"  Rs {value:>6} -> {charges.breakeven_pct(Decimal('465.50'), qty)}%")

    if args.write and ranked:
        path = os.path.join(HERE, "watchlist.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# Built by build-universe.sh, ranked by realised intraday\n")
            fh.write(f"# range over the last year. Threshold {threshold}%.\n")
            fh.write("# Rebuild periodically: volatility is not permanent.\n\n")
            for p in ranked[: args.top]:
                fh.write(f"{p.symbol}\n")
        print(f"\nwrote {min(args.top, len(ranked))} symbols to {path}")
        print("NOTE: this file is in git; commit it if you want it to survive")
        print("the next deploy, which does git reset --hard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
