"""
Stocks in play: opening-range breakout on the day's most active F&O stocks.

After Zarattini, Barbon & Aziz (2024), "A Profitable Day Trading Strategy for
the U.S. Equity Market": each day take the stocks whose first-bar volume is
the highest multiple of their own 14-day first-bar average (relative volume),
trade the break of the first bar in the direction it closed, stop a fraction
of the daily ATR away, and hold to the close unless stopped.

Adapted to NSE and to this account: MIS, flat by 14:50, Shoonya charges via
charges.py, a slippage tick on every fill, risk per trade and the daily kill
from the live settings, leverage capped at 5x.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from datetime import date, time
from decimal import Decimal as D
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import charges  # noqa: E402
import portfolio_sim as sim  # noqa: E402

EXCLUDE = {"NIFTYBEES", "BANKBEES"}


@dataclass(frozen=True)
class Cfg:
    name: str = "sip"
    capital: D = D("60600")
    leverage: D = D("5")
    top_n: int = 3
    min_relvol: D = D("1.0")
    min_atr_pct: D = D("0")         # daily ATR as % of price
    or_bars: int = 1                # opening range length in bars
    stop_atr: Optional[D] = D("0.10")   # stop distance as a fraction of daily ATR; None: other side of the range
    risk_rs: D = D("100")           # rupees lost at the stop, per trade
    daily_kill: D = D("300")        # day net at or under -this: close all, done
    exit_at: time = time(14, 50)
    entry_until: time = time(10, 15)    # a pending stop entry expires here
    slip_ticks: int = 1
    target_r: Optional[D] = None    # take profit at this many R (None: hold to the close)
    day_steps: tuple = ()           # ratchet day lock ((arm_rs, keep), ...)
    day_target: Optional[D] = None
    trail_atr: Optional[D] = None   # once +1R, trail the stop this fraction of ATR behind the best price
    direction: str = "both"         # both | long | short
    lookback: int = 14


def cost(side, entry, exit_, qty):
    buy, sell = (entry * qty, exit_ * qty) if side == "B" else (exit_ * qty, entry * qty)
    return charges.round_trip(buy, sell).total


def net(side, entry, px, qty):
    g = (px - entry) * qty * (1 if side == "B" else -1)
    return g - cost(side, entry, px, qty)


def daily_stats(data) -> Dict[str, Dict[date, dict]]:
    """Per symbol per day: first-bar volume, daily high/low/close."""
    out = {}
    for s, v in data.items():
        dd = {}
        for d, bars in v["days"].items():
            if len(bars) < 40:
                continue
            dd[d] = {"v0": bars[0].volume, "h": max(b.high for b in bars), "l": min(b.low for b in bars),
                     "c": bars[-1].close}
        out[s] = dd
    return out


def run(data, cfg: Cfg, start: date, end: date, stats=None, log: Optional[list] = None) -> dict:
    stats = stats or daily_stats(data)
    days = [d for d in sim.trading_days(data) if start <= d <= end]
    all_days = sim.trading_days(data)
    res = []
    for d in days:
        i = all_days.index(d)
        prev = all_days[max(0, i - cfg.lookback):i]
        if len(prev) < cfg.lookback:
            continue
        cands = []
        for s, v in data.items():
            if s in EXCLUDE or d not in stats[s] or d not in v["days"]:
                continue
            ps = [stats[s][p] for p in prev if p in stats[s]]
            if len(ps) < cfg.lookback - 2:
                continue
            avg_v0 = sum(p["v0"] for p in ps) / len(ps)
            if avg_v0 <= 0:
                continue
            trs, pc = [], None
            for p in ps:
                trs.append(max(p["h"] - p["l"], abs(p["h"] - pc) if pc else 0, abs(p["l"] - pc) if pc else 0))
                pc = p["c"]
            atr = sum(trs) / len(trs)
            bars = v["days"][d]
            orb = bars[:cfg.or_bars]
            v0 = sum(b.volume for b in orb) / cfg.or_bars
            rv = D(v0) / D(str(avg_v0))
            o, c = orb[0].open, orb[-1].close
            if c == o or atr <= 0:
                continue
            if rv < cfg.min_relvol or atr / o * 100 < cfg.min_atr_pct:
                continue
            side = "B" if c > o else "S"
            if cfg.direction == "long" and side == "S" or cfg.direction == "short" and side == "B":
                continue
            cands.append((rv, s, side, max(b.high for b in orb), min(b.low for b in orb), atr, v["tick"]))
        cands.sort(key=lambda x: -x[0])
        picks = cands[:cfg.top_n]
        # walk the day bar by bar across all picks
        pos = {}
        for rv, s, side, hi, lo, atr, tick in picks:
            trig = hi + tick if side == "B" else lo - tick
            pos[s] = {"side": side, "trig": trig, "hi": hi, "lo": lo, "atr": atr, "tick": tick,
                      "state": "pending", "qty": 0, "entry": None, "stop": None, "best": None, "pnl": D(0), "rv": rv}
        realised = D(0)
        day_best = None
        done = False
        n_bars = min(len(data[s]["days"][d]) for s in pos) if pos else 0
        for k in range(cfg.or_bars, n_bars):
            if done:
                break
            t = data[next(iter(pos))]["days"][d][k].opens_at.time()
            for s, p in pos.items():
                b = data[s]["days"][d][k]
                slip = p["tick"] * cfg.slip_ticks
                if p["state"] == "pending":
                    if t >= cfg.entry_until or t >= cfg.exit_at:
                        p["state"] = "expired"
                        continue
                    hit = b.high >= p["trig"] if p["side"] == "B" else b.low <= p["trig"]
                    if not hit:
                        continue
                    e = (max(p["trig"], b.open) + slip) if p["side"] == "B" else (min(p["trig"], b.open) - slip)
                    dist = (p["atr"] * cfg.stop_atr) if cfg.stop_atr is not None else (p["trig"] - p["lo"] if p["side"] == "B" else p["hi"] - p["trig"])
                    dist = max(dist, p["tick"] * 4)
                    stop = e - dist if p["side"] == "B" else e + dist
                    per_slot = cfg.capital * cfg.leverage / max(1, cfg.top_n)
                    qty = int(min(cfg.risk_rs / dist, per_slot / e))
                    if qty < 1:
                        p["state"] = "expired"
                        continue
                    p.update(state="open", entry=e, stop=stop, qty=qty, dist=dist, best=e)
                    # same-bar stop: assume the worst if the bar also reached the stop
                    if (p["side"] == "B" and b.low <= stop) or (p["side"] == "S" and b.high >= stop):
                        x = stop - slip if p["side"] == "B" else stop + slip
                        p["pnl"] = net(p["side"], e, x, qty); p["state"] = "closed"; realised += p["pnl"]
                    continue
                if p["state"] != "open":
                    continue
                if t >= cfg.exit_at:
                    x = b.open - slip if p["side"] == "B" else b.open + slip
                    p["pnl"] = net(p["side"], p["entry"], x, p["qty"]); p["state"] = "closed"; realised += p["pnl"]
                    continue
                if (p["side"] == "B" and b.low <= p["stop"]) or (p["side"] == "S" and b.high >= p["stop"]):
                    sx = min(p["stop"], b.open) if p["side"] == "B" else max(p["stop"], b.open)
                    x = sx - slip if p["side"] == "B" else sx + slip
                    p["pnl"] = net(p["side"], p["entry"], x, p["qty"]); p["state"] = "closed"; realised += p["pnl"]
                    continue
                if cfg.target_r is not None:
                    tgt = p["entry"] + p["dist"] * cfg.target_r if p["side"] == "B" else p["entry"] - p["dist"] * cfg.target_r
                    if (p["side"] == "B" and b.high >= tgt) or (p["side"] == "S" and b.low <= tgt):
                        p["pnl"] = net(p["side"], p["entry"], tgt, p["qty"]); p["state"] = "closed"; realised += p["pnl"]
                        continue
                if cfg.trail_atr is not None:
                    p["best"] = max(p["best"], b.high) if p["side"] == "B" else min(p["best"], b.low)
                    if abs(p["best"] - p["entry"]) >= p["dist"]:
                        ts = p["best"] - p["atr"] * cfg.trail_atr if p["side"] == "B" else p["best"] + p["atr"] * cfg.trail_atr
                        p["stop"] = max(p["stop"], ts) if p["side"] == "B" else min(p["stop"], ts)
            # day-level checks at this bar's close
            open_net = sum((net(p["side"], p["entry"], data[s]["days"][d][k].close, p["qty"])
                            for s, p in pos.items() if p["state"] == "open"), D(0))
            day_net = realised + open_net
            day_best = day_net if day_best is None else max(day_best, day_net)
            why = None
            if day_net <= -cfg.daily_kill:
                why = "kill"
            elif cfg.day_target is not None and day_net >= cfg.day_target:
                why = "target"
            elif cfg.day_steps:
                keep = None
                for a, kf in cfg.day_steps:
                    if day_best >= a:
                        keep = kf
                if keep is not None and day_net <= max(D(5), day_best * keep):
                    why = "lock"
            if why:
                for s, p in pos.items():
                    if p["state"] == "open":
                        b = data[s]["days"][d][k]
                        x = b.close - p["tick"] if p["side"] == "B" else b.close + p["tick"]
                        p["pnl"] = net(p["side"], p["entry"], x, p["qty"]); p["state"] = "closed"; realised += p["pnl"]
                    elif p["state"] == "pending":
                        p["state"] = "expired"
                done = True
        for s, p in pos.items():
            if p["state"] == "open":
                b = data[s]["days"][d][n_bars - 1]
                p["pnl"] = net(p["side"], p["entry"], b.close, p["qty"]); p["state"] = "closed"; realised += p["pnl"]
        trades = [(s, p["side"], float(p["pnl"])) for s, p in pos.items() if p["entry"] is not None]
        if log is not None:
            log.append((d, trades))
        res.append((d, realised, len(trades), sum(1 for x in trades if x[2] > 0)))
    nets = [r[1] for r in res]
    eq, peak, dd = D(0), D(0), D(0)
    for x in nets:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {"name": cfg.name, "net": round(float(sum(nets))), "days": len(res),
            "green": sum(1 for x in nets if x > 0), "red": sum(1 for x in nets if x < 0),
            "trades": sum(r[2] for r in res), "wins": sum(r[3] for r in res),
            "dd": round(float(dd)), "worst": round(float(min(nets))) if nets else 0,
            "best": round(float(max(nets))) if nets else 0, "daily": res}
