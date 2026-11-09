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
from market import Candle, opening_range  # noqa: E402

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


def run(broker, symbols: List[str], days: int, last_entry: time) -> dict:
    end = sess.now()
    start = end - timedelta(days=int(days * 1.5) + 7)
    results = {"breakout": [], "retest": []}
    per_symbol = collections.defaultdict(lambda: {"breakout": Decimal(0), "retest": Decimal(0)})

    for sym in symbols:
        try:
            inst = broker.instrument(sym)
        except KeyError:
            continue
        candles: List[Candle] = []
        t0 = start
        while t0 < end:
            t1 = min(t0 + timedelta(days=7), end)
            candles += broker.candles(inst.token, t0, t1, interval=5)
            t0 = t1
        for d, cs in sorted(by_day(candles).items())[-days:]:
            session = sess.session_for(datetime.combine(d, time(10, 0), tzinfo=sess.IST))
            if session is None or session.special:
                continue
            for variant in ("breakout", "retest"):
                r = simulate_day(cs, session, inst.tick, variant=variant, last_entry=last_entry)
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


# ------------------------------------------------------------------ both sides, direction gate

def _gate_at(t: datetime, day_by_sym: Dict[str, List[Candle]], hist_by_sym: Dict[str, List[Candle]],
             day: date, index: str, cache: dict) -> Optional[dict]:
    """The direction the live system would have measured just before bar `t`
    (only bars that had closed: no look-ahead)."""
    if t in cache:
        return cache[t]
    import regime
    trends = {}
    for sym, cs in day_by_sym.items():
        before = [c for c in cs if c.opens_at < t]
        hist = hist_by_sym.get(sym, []) + before
        bars15 = _bars15(before)
        trends[sym] = regime.stock_trend(hist, day, bars15)
    cache[t] = {"market": regime.market(trends, index), "stocks": trends}
    return cache[t]


def _bars15(cs: List[Candle]) -> List[Candle]:
    out: List[Candle] = []
    for c in cs:
        slot = c.opens_at.replace(minute=(c.opens_at.minute // 15) * 15)
        if out and out[-1].opens_at == slot:
            b = out[-1]
            out[-1] = Candle(b.opens_at, b.open, max(b.high, c.high), min(b.low, c.low), c.close, b.volume + c.volume, 15)
        else:
            out.append(Candle(slot, c.open, c.high, c.low, c.close, c.volume, 15))
    return out


def simulate_sides(sym: str, day_candles: List[Candle], session, tick: Decimal, *, sides: tuple,
                   gate=None, last_entry: time) -> Optional[dict]:
    """Resting triggers both ways at the opening range; the first to trade
    through fills and the other is cancelled. With `gate`, a side the
    measured direction forbids at that bar is not live (the system would
    have cancelled it)."""
    import regime
    rng = opening_range(day_candles, session)
    if rng is None:
        return None
    high, low = rng
    plans = {}
    try:
        if "B" in sides:
            plans["B"] = strat.plan_trigger_exit(high, low, tick)
        if "S" in sides:
            plans["S"] = strat.plan_breakdown(high, low, tick)
    except strat.PlanError:
        return None
    trig = {"B": high + tick, "S": low - tick}
    cutoff = datetime.combine(session.day, last_entry, tzinfo=sess.IST)
    flat = datetime.combine(session.day, FLAT_AT, tzinfo=sess.IST)
    side = fill_at = fill_px = None
    for c in day_candles:
        if c.opens_at < session.opening_range_end:
            continue
        if c.opens_at >= cutoff:
            break
        live = [s for s in plans if not gate or not regime.check("BUY" if s == "B" else "SELL", sym, gate(c.opens_at))]
        hit_b = "B" in live and c.high >= trig["B"]
        hit_s = "S" in live and c.low <= trig["S"]
        if hit_b and hit_s:
            return {"filled": False, "ambiguous": True}
        if hit_b or hit_s:
            side = "B" if hit_b else "S"
            limit = plans[side].entry
            fill_px = min(max(trig["B"], c.open), limit) if side == "B" else max(min(trig["S"], c.open), limit)
            fill_at = c.opens_at
            break
    if side is None:
        return {"filled": False}
    p = plans[side]
    qty = int(NOTIONAL / fill_px)
    if qty < 1:
        return None
    exit_px = how = None
    for c in day_candles:
        if c.opens_at <= fill_at:
            continue
        if c.opens_at >= flat:
            exit_px, how = c.open, "flatten"
            break
        if side == "B":
            if c.low <= p.stop:
                exit_px, how = p.stop, "stop"
                break
            if c.high >= p.target:
                exit_px, how = p.target, "target"
                break
        else:
            if c.high >= p.stop:
                exit_px, how = p.stop, "stop"
                break
            if c.low <= p.target:
                exit_px, how = p.target, "target"
                break
    if exit_px is None:
        exit_px, how = day_candles[-1].close, "close"
    gross = (exit_px - fill_px) * qty * (1 if side == "B" else -1)
    buy, sell = (fill_px, exit_px) if side == "B" else (exit_px, fill_px)
    cost = charges.round_trip(buy * qty, sell * qty).total
    return {"filled": True, "side": side, "how": how, "gross": gross, "net": gross - cost, "qty": qty}


VARIANTS = {"long only": (("B",), False), "both sides": (("B", "S"), False), "both + direction gate": (("B", "S"), True)}


def run_sides(broker, symbols: List[str], days: int, last_entry: time, index: str = "NIFTYBEES") -> dict:
    end = sess.now()
    start = end - timedelta(days=int(days * 1.5) + 10)
    data: Dict[str, Dict[date, List[Candle]]] = {}
    ticks: Dict[str, Decimal] = {}
    for sym in symbols:
        try:
            inst = broker.instrument(sym)
        except KeyError:
            continue
        cs: List[Candle] = []
        t0 = start
        while t0 < end:
            t1 = min(t0 + timedelta(days=7), end)
            cs += broker.candles(inst.token, t0, t1, interval=5)
            t0 = t1
        data[sym], ticks[sym] = by_day(cs), inst.tick
    all_days = sorted({d for v in data.values() for d in v})[-days:]
    results = {k: [] for k in VARIANTS}
    for d in all_days:
        session = sess.session_for(datetime.combine(d, time(10, 0), tzinfo=sess.IST))
        if session is None or session.special:
            continue
        day_by_sym = {s: v[d] for s, v in data.items() if d in v}
        hist_by_sym = {}
        for s, v in data.items():
            prev = [x for x in sorted(v) if x < d][-2:]
            hist_by_sym[s] = [c for x in prev for c in v[x]]
        cache: dict = {}
        gate = lambda t: _gate_at(t, day_by_sym, hist_by_sym, d, index, cache)  # noqa: E731
        for sym, cs in day_by_sym.items():
            for name, (sides, gated) in VARIANTS.items():
                r = simulate_sides(sym, cs, session, ticks[sym], sides=sides, gate=gate if gated else None,
                                   last_entry=last_entry)
                if r and r.get("filled"):
                    results[name].append({**r, "symbol": sym, "day": d})
    return {"results": results, "days": len(all_days)}


def report_sides(out: dict) -> str:
    lines = [f"{out['days']} sessions, fixed notional Rs {NOTIONAL} per trade, real charges", ""]
    for name, rs in out["results"].items():
        if not rs:
            lines.append(f"{name:22s} no trades")
            continue
        net = sum(r["net"] for r in rs)
        wins = [r for r in rs if r["net"] > 0]
        loss = -sum(r["net"] for r in rs if r["net"] < 0)
        gain = sum(r["net"] for r in wins)
        pf = (gain / loss) if loss else Decimal("999")
        longs = sum(1 for r in rs if r["side"] == "B")
        hows = collections.Counter(r["how"] for r in rs)
        lines.append(f"{name:22s} trades {len(rs):4d} ({longs} long / {len(rs) - longs} short)  "
                     f"win {len(wins) / len(rs) * 100:3.0f}%  net {net:+8.0f}  per trade {net / len(rs):+6.1f}  "
                     f"profit factor {pf:.2f}  stop/target/flat {hows['stop']}/{hows['target']}/{hows['flatten']}")
    return "\n".join(lines)
