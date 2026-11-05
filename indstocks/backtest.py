"""
Opening-range backtest on real NSE 5-minute candles.

Replays the exact rules the autotrader uses, day by day, for every name on
the watchlist, under two entry variants:

  breakout   buy the first 5-min close above the range high (what runs now)
  retest     after that breakout, buy only if price pulls back to the range
             high within the same session (limit at the range high)

Both use the same stop (range midpoint), target (2R), last-entry cutoff,
flatten at 15:05, and real charges. Sizing is a fixed notional per trade so
the comparison is about entries, not account size.

Read-only. Places nothing. Run on the box:
    sudo bash scripts/backtest.sh [days] [last_entry HH:MM]
"""

from __future__ import annotations

import collections
import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges                                          # noqa: E402
import session as sess                                  # noqa: E402
import strategy as strat                                # noqa: E402
from bars import Bars, Candle, opening_range, scrip_code  # noqa: E402

NOTIONAL = Decimal(os.environ.get("BT_NOTIONAL", "12500"))
FLAT_AT = time(15, 5)


def by_day(candles: List[Candle]) -> Dict[date, List[Candle]]:
    out: Dict[date, List[Candle]] = collections.defaultdict(list)
    for c in candles:
        out[c.opens_at.date()].append(c)
    return out


def simulate_day(day_candles: List[Candle], session, tick: Decimal, *,
                 variant: str, last_entry: time) -> Optional[dict]:
    rng = opening_range(day_candles, session)
    if rng is None:
        return None
    high, low = rng
    try:
        plan = strat.plan_trigger_exit(high, low, tick)
    except strat.PlanError:
        return None
    stop, target = plan.stop, plan.target
    entry_px = plan.entry
    qty = int(NOTIONAL / entry_px)
    if qty < 1:
        return None
    cutoff = datetime.combine(session.day, last_entry, tzinfo=sess.IST)
    flat = datetime.combine(session.day, FLAT_AT, tzinfo=sess.IST)

    after_range = [c for c in day_candles if c.opens_at >= session.opening_range_end]
    broke_at = None
    filled_at = None
    fill_px = None
    for c in after_range:
        if c.opens_at >= cutoff:
            break
        if broke_at is None:
            # trigger order: fills the moment price trades through range high + tick
            if c.high >= entry_px:
                broke_at = c.opens_at
                if variant == "breakout":
                    filled_at, fill_px = c.opens_at, max(entry_px, c.open)
                    break
                # retest: a limit at the range high, armed from now
                continue
        else:
            if variant == "retest" and c.low <= high:
                filled_at, fill_px = c.opens_at, min(high, c.open)
                break
    if filled_at is None:
        return {"setup": broke_at is not None, "filled": False}

    # walk forward: stop first, then target, else flatten
    exit_px, how = None, None
    for c in day_candles:
        if c.opens_at <= filled_at:
            continue
        if c.opens_at >= flat:
            exit_px, how = c.open, "flatten"
            break
        if c.low <= stop:
            exit_px, how = stop, "stop"
            break
        if c.high >= target:
            exit_px, how = target, "target"
            break
    if exit_px is None:
        exit_px, how = day_candles[-1].close, "close"

    gross = (exit_px - fill_px) * qty
    cost = charges.round_trip(fill_px * qty, exit_px * qty).total
    return {"setup": True, "filled": True, "how": how, "gross": gross,
            "net": gross - cost, "fill_px": fill_px, "exit_px": exit_px, "qty": qty}


def run(client, master, symbols: List[str], days: int, last_entry: time) -> dict:
    bars = Bars(client, "5minute")
    end = sess.now()
    start = end - timedelta(days=int(days * 1.5) + 7)
    results = {"breakout": [], "retest": []}
    per_symbol = collections.defaultdict(lambda: {"breakout": Decimal(0), "retest": Decimal(0)})

    for sym in symbols:
        try:
            inst = master.resolve(sym)
        except KeyError:
            continue
        candles: List[Candle] = []
        t0 = start
        while t0 < end:
            t1 = min(t0 + timedelta(days=7), end)
            candles += bars.fetch_one(scrip_code(inst), t0, t1)
            t0 = t1
        for d, cs in sorted(by_day(candles).items())[-days:]:
            session = sess.session_for(datetime.combine(d, time(10, 0), tzinfo=sess.IST))
            if session is None:
                continue
            for variant in ("breakout", "retest"):
                r = simulate_day(cs, session, inst.tick_size, variant=variant, last_entry=last_entry)
                if r and r.get("filled"):
                    results[variant].append({**r, "symbol": sym, "day": d})
                    per_symbol[sym][variant] += r["net"]
    return {"results": results, "per_symbol": per_symbol}


def report(out: dict) -> str:
    lines = []
    for variant in ("breakout", "retest"):
        rs = out["results"][variant]
        if not rs:
            lines.append(f"{variant}: no trades")
            continue
        net = sum(r["net"] for r in rs)
        wins = [r for r in rs if r["net"] > 0]
        hows = collections.Counter(r["how"] for r in rs)
        lines.append(f"{variant:9s} trades {len(rs):4d}  win {len(wins) / len(rs) * 100:4.0f}%  "
                     f"net {net:+9.0f}  per trade {net / len(rs):+6.1f}  "
                     f"stops {hows['stop']} targets {hows['target']} flatten {hows['flatten']}")
    lines.append("")
    lines.append("per symbol (net, breakout / retest):")
    for sym, v in sorted(out["per_symbol"].items(), key=lambda kv: kv[1]["breakout"], reverse=True):
        lines.append(f"  {sym:12s} {v['breakout']:+8.0f} / {v['retest']:+8.0f}")
    return "\n".join(lines)
