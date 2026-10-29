"""
Dry run: the whole pipeline against live data, placing nothing.

Resolves the watchlist, pulls 5-minute candles for a trading day, builds each
opening range, checks the entry rule, and for anything that triggered shows
the exit plan, the position size, the risk verdict, and the exact JSON that
WOULD be sent to POST /smart/order.

It never calls a write endpoint. The only network calls are the instruments
master and historical candles, both read-only, both outside the whitelist
requirement.

Run it after 09:30 IST for live behaviour, or on any past trading day to
replay. Market closed? It defaults to the previous trading day.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import risk as risk_mod  # noqa: E402
import session as sess  # noqa: E402
import strategy as strat  # noqa: E402
from bars import Bars, opening_range, scrip_code  # noqa: E402
from client import Client  # noqa: E402
from instruments import Instruments  # noqa: E402
from smartorder import SmartOrderError, build_for_instrument  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def load_watchlist(path: str) -> list[str]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line.upper())
    return out


def pick_day(arg: str | None, now: datetime) -> date:
    if arg:
        return date.fromisoformat(arg)
    today = now.date()
    if sess.is_trading_day(today) and now.time() >= sess.MARKET_OPEN:
        return today
    return sess.previous_trading_days(today + timedelta(days=1), 1)[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry run. Places nothing.")
    ap.add_argument("--date", help="trading day, YYYY-MM-DD (default: today or the last one)")
    ap.add_argument("--cash", default="100000", help="account cash for sizing")
    ap.add_argument("--at", help="evaluate as of this IST time, HH:MM (default: now, or the close)")
    ap.add_argument("--watchlist", default=os.path.join(HERE, "watchlist.txt"))
    ap.add_argument("--max-loss", default="500")
    args = ap.parse_args()

    now = sess.now()
    day = pick_day(args.date, now)
    session = sess.Session(day)

    if args.at:
        hh, mm = (int(x) for x in args.at.split(":"))
        as_of = sess.at(day, sess.time(hh, mm))
    elif day == now.date():
        as_of = now
    else:
        as_of = session.close

    limits = risk_mod.Limits(max_loss_per_trade=Decimal(args.max_loss))
    cash = Decimal(args.cash)

    print(f"Dry run for {day} as of {as_of:%H:%M} IST  ({session.phase(as_of)})")
    print(f"cash {cash}, max loss per trade {limits.max_loss_per_trade}")
    print("NOTHING IS PLACED. Read-only endpoints only.\n")

    if not sess.is_trading_day(day):
        print(f"{day} is not an NSE trading day.")
        return 0

    client = Client()
    master = Instruments(client=client, source="equity").load()
    symbols = load_watchlist(args.watchlist)

    resolved, missing = {}, []
    for s in symbols:
        try:
            resolved[s] = master.resolve(s)
        except KeyError:
            missing.append(s)
    if missing:
        print(f"not in the master, skipped: {', '.join(missing)}\n")

    bars = Bars(client, "5minute")
    codes = {scrip_code(i): s for s, i in resolved.items()}
    candles = bars.fetch(list(codes), session.open, min(as_of, session.close))

    triggered, waiting, no_data = [], [], []
    for code, cs in candles.items():
        symbol = codes[code]
        if not cs:
            no_data.append(symbol)
            continue
        sig = strat.check_entry(cs, session, as_of)
        (triggered if sig.triggered else waiting).append((symbol, sig))

    print(f"scanned {len(resolved)} symbols: {len(triggered)} triggered, "
          f"{len(waiting)} did not, {len(no_data)} no data")
    if no_data:
        # Not necessarily "did not trade": the endpoint returns an empty
        # array when throttled too, and these survived the retries.
        print(f"  no candles after retries: {', '.join(sorted(no_data))}")
        print("  treat these as UNKNOWN, not as 'no setup'.")
    print()

    for symbol, sig in sorted(waiting, key=lambda x: (x[1].distance is None,
                                                      x[1].distance or 0))[:8]:
        rng = (f"range {sig.range_low}-{sig.range_high}"
               if sig.range_high is not None else "no range")
        dist = f", {sig.distance} under" if sig.distance is not None else ""
        print(f"  {symbol:<12} {sig.state:<15} {rng}{dist}")

    if not triggered:
        print("\nNo setups. That is the normal outcome most mornings.")
        return 0

    print()
    for symbol, sig in triggered:
        inst = resolved[symbol]
        print("=" * 66)
        print(f"{symbol}  (id {inst.security_id}, tick {inst.tick_size})")
        print(f"  range {sig.range_low} - {sig.range_high}")
        print(f"  entry {sig.price} at {sig.at:%H:%M}  ({sig.reason})")

        try:
            plan = strat.plan_exit(sig.price, sig.range_high, sig.range_low,
                                   inst.tick_size)
        except strat.PlanError as exc:
            print(f"  NO PLAN: {exc}")
            continue

        sizing = risk_mod.size_position(plan, cash, limits)
        print(f"  stop {plan.stop}  target {plan.target}  "
              f"risk/share {plan.risk_per_share}  R:R {plan.reward_to_risk:.2f}")
        print(f"  size {sizing.qty} shares ({sizing.limit_hit}), "
              f"value {sizing.position_value}, loss at stop {sizing.loss_at_stop}, "
              f"costs {sizing.round_trip_cost}")

        verdict = risk_mod.check_entry_order(
            symbol=symbol, plan=plan, sizing=sizing, session=session,
            now=as_of, day=risk_mod.DayState(), limits=limits)
        print(f"  RISK: {verdict}")
        if not verdict.passed or not sizing.ok:
            continue

        try:
            order = build_for_instrument(
                inst, qty=sizing.qty, entry=plan.entry, stop=plan.stop,
                target=plan.target, remarks=f"orb-v1/{symbol}/{day}")
            print("  would send POST /smart/order:")
            for k, v in order.payload().items():
                print(f"      {k}: {v}")
        except SmartOrderError as exc:
            print(f"  ORDER REJECTED LOCALLY: {exc}")

    print("=" * 66)
    print("\nEnd of dry run. No order was placed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
