"""
Portfolio replay of the trading system on real 5-minute Shoonya candles.

Uses the live code for everything it can: setups.detect (the playbook),
regime.stock_trend (trend scores), backtest.simulate_setups + strategy_book
(the nightly book, rebuilt each day from the 20 sessions before it only),
strategy.make_plan, risk.size_position / check_entry_order (the owner's
limits), charges.round_trip (every rupee of charges).

Claude's live judgement cannot be replayed; in its place a fixed policy takes
the active setups, best record first, as Claude is instructed to.

Capital compounds: each day starts with the previous day's closing balance.
"""

from __future__ import annotations

import collections
import gzip
import json
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
from typing import Dict, List, Optional

REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "indstocks")
SANDBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
os.environ["TRADER_STATE_DIR"] = SANDBOX
os.environ["TRADER_ENV_FILE"] = os.path.join(SANDBOX, "env")
os.makedirs(SANDBOX, exist_ok=True)
sys.path.insert(0, REPO)

import backtest                                         # noqa: E402
import charges                                          # noqa: E402
import regime                                           # noqa: E402
import risk as risk_mod                                 # noqa: E402
import session as sess                                  # noqa: E402
import setups as playbook                               # noqa: E402
import strategy as strat                                # noqa: E402
import strategy_book                                    # noqa: E402
from market import Candle                               # noqa: E402

IST = sess.IST


@dataclass
class Config:
    name: str = "V1"
    capital: D = D("26000")
    leverage: D = D("4.0")
    max_concurrent: int = 4
    max_trades: int = 15
    max_trades_hard: int = 25
    giveback_pct: D = D("50")
    max_entries_per_symbol: int = 3
    max_resting: int = 8
    max_loss_per_trade: D = D("600")
    daily_loss: D = D("2500")
    min_rr: D = D("2.0")
    min_reward_to_cost: D = D("2")
    min_position_value: D = D("15000")
    min_net_target: D = D("100")
    last_entry: time = time(14, 45)
    flat_by: time = time(15, 5)
    valid_bars: int = 6                 # Claude's valid_minutes, 30 min
    book_window: int = 20
    use_book: bool = True
    # --- knobs the critique may change (V1 = live behaviour) ---
    trail: Optional[str] = None         # None | "be1r" (breakeven at +1R, trail 1R from +1.5R)
    allowed_setups: Optional[set] = None   # restrict to these "setup side" keys
    no_entry_before: time = time(9, 30)
    skip_first_bars_after_or: int = 0
    direction_gate: bool = False        # only with the market direction
    cooldown_bars_after_stop: int = 0   # per symbol
    stop_day_after_losses: int = 0      # halt new entries after N losing trades in a day (0 = off)
    target_r: Optional[D] = None        # override the setup's 2R target
    book_min_pf: D = D("1.15")
    slippage_ticks: int = 0             # extra ticks against us on stop fills and entries
    lunch_block: Optional[tuple] = None  # (time, time): no new entries inside


def load(path: str) -> Dict[str, dict]:
    d = json.load(gzip.open(path))
    out = {}
    for s, v in d["symbols"].items():
        tick = D(v["tick"])
        byday = collections.defaultdict(list)
        for ts, o, h, l, c, vol in v["candles"]:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=IST)
            byday[t.date()].append(Candle(t, D(o), D(h), D(l), D(c), int(vol), 5))
        out[s] = {"tick": tick, "days": {k: sorted(x, key=lambda c: c.opens_at) for k, x in byday.items()}}
    return out


def trading_days(data) -> List[date]:
    days = sorted({d for v in data.values() for d in v["days"]})
    return [d for d in days if d.weekday() < 5 and len(data["NIFTYBEES"]["days"].get(d, [])) >= 60]


# ------------------------------------------------------------------ caches

_setup_cache: Dict[tuple, list] = {}


def setup_results(data, sym, d, prev_days, cfg) -> list:
    """simulate_setups for one symbol-day (the nightly book's raw material)."""
    key = (sym, d, cfg.last_entry, cfg.valid_bars)
    if key not in _setup_cache:
        session = sess.Session(d)
        hist = [c for x in prev_days[-2:] for c in data[sym]["days"].get(x, [])]
        _setup_cache[key] = backtest.simulate_setups(
            data[sym]["days"][d], hist, session, data[sym]["tick"], last_entry=cfg.last_entry,
            notional=D("26000"), valid_bars=cfg.valid_bars)
    return _setup_cache[key]


def book_for(data, days, i, cfg) -> Optional[dict]:
    """The book as the 15:40 job would have built it the evening before day i."""
    if i < cfg.book_window:
        return None
    window = days[i - cfg.book_window:i]
    rows = []
    for j, d in enumerate(window):
        prev = days[max(0, i - cfg.book_window + j - 2):i - cfg.book_window + j]
        for sym in data:
            if d in data[sym]["days"]:
                rows += setup_results(data, sym, d, prev, cfg)
    old = strategy_book.MIN_PF
    strategy_book.MIN_PF = cfg.book_min_pf
    try:
        return strategy_book.build(rows, len(window))
    finally:
        strategy_book.MIN_PF = old


# ------------------------------------------------------------------ one day

@dataclass
class Pos:
    sym: str
    side: str            # B / S
    qty: int
    entry: D
    stop: D
    target: D
    r: D
    setup: str
    opened: datetime
    tick: D
    best: D = D(0)


def run_day(data, d, prev_days, book, cash: D, cfg: Config, log: list) -> dict:
    session = sess.Session(d)
    os.environ["TRADER_LAST_ENTRY"] = cfg.last_entry.strftime("%H:%M")
    limits = risk_mod.Limits(max_loss_per_trade=cfg.max_loss_per_trade, max_leverage=cfg.leverage,
                             daily_loss_limit=cfg.daily_loss, max_trades_per_day=cfg.max_trades,
                             min_reward_to_risk=cfg.min_rr, min_reward_to_cost=cfg.min_reward_to_cost,
                             max_concurrent_positions=cfg.max_concurrent, cash_buffer_pct=D("5"),
                             min_circuit_distance_pct=D("0"))
    syms = [s for s in data if d in data[s]["days"]]
    bars = {s: data[s]["days"][d] for s in syms}
    hist = {s: [c for x in prev_days[-2:] for c in data[s]["days"].get(x, [])] for s in syms}
    nbars = min(len(b) for b in bars.values())
    open_pos: Dict[str, Pos] = {}
    resting: List[dict] = []
    trades: List[dict] = []
    entries = collections.Counter()
    fills = 0
    realised_gross = D(0)
    realised_net = D(0)
    halted = None
    cooldown: Dict[str, int] = {}
    losses_today = 0
    flat_at = datetime.combine(d, cfg.flat_by, tzinfo=IST)

    def close(p: Pos, px: D, how: str, t: datetime):
        nonlocal realised_gross, realised_net, losses_today
        gross = (px - p.entry) * p.qty * (1 if p.side == "B" else -1)
        buy, sell = (p.entry, px) if p.side == "B" else (px, p.entry)
        cost = charges.round_trip(buy * p.qty, sell * p.qty).total
        realised_gross += gross
        realised_net += gross - cost
        if gross - cost < 0:
            losses_today += 1
        trades.append({"day": d.isoformat(), "sym": p.sym, "side": p.side, "setup": p.setup, "qty": p.qty,
                       "entry": float(p.entry), "exit": float(px), "how": how, "gross": float(gross),
                       "charges": float(cost), "net": float(gross - cost), "value": float(p.entry * p.qty),
                       "opened": p.opened.strftime("%H:%M"), "closed": t.strftime("%H:%M"),
                       "risk": float(p.r * p.qty)})
        if how == "stop":
            cooldown[p.sym] = cfg.cooldown_bars_after_stop

    for k in range(nbars):
        t = bars[syms[0]][k].opens_at
        # ---- flatten
        if t >= flat_at:
            for s, p in list(open_pos.items()):
                close(p, bars[s][k].open, "flatten", t)
                del open_pos[s]
            resting.clear()
            break
        # ---- exits on this bar (exchange stop first: conservative)
        for s, p in list(open_pos.items()):
            c = bars[s][k]
            slip = p.tick * cfg.slippage_ticks
            if p.side == "B":
                if c.low <= p.stop:
                    close(p, min(p.stop, c.open) - slip, "stop", t)
                    del open_pos[s]
                    continue
                if c.high >= p.target:
                    close(p, max(p.target, c.open) if c.open >= p.target else p.target, "target", t)
                    del open_pos[s]
                    continue
            else:
                if c.high >= p.stop:
                    close(p, max(p.stop, c.open) + slip, "stop", t)
                    del open_pos[s]
                    continue
                if c.low <= p.target:
                    close(p, min(p.target, c.open) if c.open <= p.target else p.target, "target", t)
                    del open_pos[s]
                    continue
            # trailing (V2+), applied at the bar close
            if cfg.trail == "be1r":
                gain = (c.close - p.entry) if p.side == "B" else (p.entry - c.close)
                if p.r > 0 and gain >= p.r:
                    if p.side == "B":
                        cand = p.entry if gain < p.r * D("1.5") else max(p.entry, c.close - p.r)
                        p.stop = max(p.stop, cand)
                    else:
                        cand = p.entry if gain < p.r * D("1.5") else min(p.entry, c.close + p.r)
                        p.stop = min(p.stop, cand)
        # ---- resting entries: fill or expire
        still = []
        for o in resting:
            s = o["sym"]
            c = bars[s][k]
            if s in open_pos:
                continue                                # the fill monitor cancels the other side
            if k > o["until"]:
                continue
            long_ = o["side"] == "B"
            trig = o["entry"]
            up = o["trigger_side"] == "B"
            hit = c.high >= trig if up else c.low <= trig
            dead = (c.low <= o["stop"]) if long_ else (c.high >= o["stop"])
            if hit:
                if o["order"] == "limit":
                    px = trig
                else:
                    px = max(trig, c.open) if up else min(trig, c.open)
                    px += (o["tick"] * cfg.slippage_ticks) * (1 if long_ else -1)
                    if (long_ and px > o["limit"]) or (not long_ and px < o["limit"]):
                        if (long_ and c.low <= o["limit"]) or (not long_ and c.high >= o["limit"]):
                            px = o["limit"]
                        else:
                            still.append(o)
                            continue
                if len(open_pos) >= cfg.max_concurrent:
                    continue                            # cap reached: the fill monitor had cancelled it
                open_pos[s] = Pos(s, o["side"], o["qty"], px, o["stop"], o["target"], o["r"], o["setup"], t, o["tick"])
                fills += 1
                entries[s] += 1
                # same-bar stop check (stop first)
                p = open_pos[s]
                if (long_ and c.low <= p.stop) or (not long_ and c.high >= p.stop):
                    close(p, p.stop, "stop", t)
                    del open_pos[s]
                continue
            if dead:
                continue
            still.append(o)
        resting = still
        for s in list(cooldown):
            cooldown[s] -= 1
            if cooldown[s] <= 0:
                del cooldown[s]
        # ---- the scan, at this bar's close (bar k is the last closed bar)
        close_t = bars[syms[0]][k].closes_at
        if close_t.time() < cfg.no_entry_before or close_t.time() >= cfg.last_entry:
            continue
        if cfg.lunch_block and cfg.lunch_block[0] <= close_t.time() < cfg.lunch_block[1]:
            continue
        if k < 2 + cfg.skip_first_bars_after_or:
            continue
        unreal = sum(((bars[s][k].close - p.entry) * p.qty * (1 if p.side == "B" else -1)
                      for s, p in open_pos.items()), D(0))
        if realised_gross + unreal <= -cfg.daily_loss:
            halted = halted or "daily loss limit"
            continue
        if cfg.stop_day_after_losses and losses_today >= cfg.stop_day_after_losses:
            halted = halted or f"{losses_today} losing trades"
            continue
        base = cfg.max_trades
        if fills >= cfg.max_trades_hard:
            continue
        # market direction for the gate
        trends = {}
        for s in syms:
            upto = bars[s][:k + 1]
            trends[s] = regime.stock_trend(hist[s] + upto, d, backtest._bars15(upto))
        market = regime.market(trends)
        candidates = []
        for s in syms:
            if s in open_pos or entries[s] >= cfg.max_entries_per_symbol or s in cooldown:
                continue
            found = playbook.detect(bars[s][:k + 1], d, data[s]["tick"], trends[s].get("score", 0.0))
            shown = strategy_book.apply(found, data[s]["tick"], book) if cfg.use_book else \
                [{**f, "edge": "untested"} for f in found]
            for f in shown:
                kk = strategy_book.key(f["setup"], f["side"])
                if cfg.allowed_setups is not None and kk not in cfg.allowed_setups:
                    continue
                if cfg.direction_gate and regime.check(f["side"], s, {"market": market, "stocks": trends}):
                    continue
                per = 0.0
                if book and kk in book.get("variants", {}):
                    per = float(book["variants"][kk]["per_trade"])
                candidates.append((per, s, f))
        candidates.sort(key=lambda x: -x[0])
        held_value = sum((p.entry * p.qty for p in open_pos.values()), D(0))
        rest_value = sum((o["entry"] * o["qty"] for o in resting), D(0))
        for per, s, f in candidates:
            if len(resting) >= cfg.max_resting:
                break
            if any(o["sym"] == s for o in resting):
                continue
            side = "B" if f["side"] == "BUY" else "S"
            tick = data[s]["tick"]
            entry, stop = D(f["entry"]), D(f["stop"])
            target = D(f["target"])
            if cfg.target_r is not None:
                r = abs(entry - stop)
                target = entry + r * cfg.target_r if side == "B" else entry - r * cfg.target_r
            try:
                plan = strat.make_plan(side, entry, stop, target, tick)
            except strat.PlanError:
                continue
            free_slots = cfg.max_concurrent - len(open_pos) - len(resting)
            if free_slots <= 0:
                break
            free_cash = max(cash * cfg.leverage - held_value - rest_value, D(0))
            slot_cash = free_cash / free_slots
            sizing = risk_mod.size_position(plan, slot_cash / cfg.leverage, limits)
            if not sizing.ok:
                continue
            if sizing.position_value < cfg.min_position_value or sizing.net_if_target_hits < cfg.min_net_target:
                continue
            # trade limit, profit-funded past it
            committed = fills + len(resting)
            if committed >= base:
                keep = realised_net * (1 - cfg.giveback_pct / 100) if realised_net > 0 else D(0)
                worst = realised_net
                for p in open_pos.values():
                    worst += (p.stop - p.entry) * p.qty * (1 if p.side == "B" else -1)
                for o in resting:
                    worst -= o["risk"]
                room = worst - keep if realised_net > 0 else D(0)
                if sizing.loss_at_stop + sizing.round_trip_cost > room:
                    continue
            day_state = risk_mod.DayState(trades_taken=0, open_symbols=tuple(open_pos), exposure=held_value,
                                          unrealised_pnl=unreal)
            v = risk_mod.check_entry_order(symbol=s, plan=plan, sizing=sizing, session=session, now=close_t,
                                           day=day_state, limits=replace(limits, max_trades_per_day=999),
                                           cash_for_leverage=cash)
            if not v.passed:
                continue
            lim = entry + tick * 2 if side == "B" else entry - tick * 2
            resting.append({"sym": s, "side": side, "trigger_side": "B" if f["order"] != "limit" and side == "B"
                            else ("S" if f["order"] != "limit" else ("S" if side == "B" else "B")),
                            "order": f.get("order", "stop"), "entry": plan.entry, "limit": lim, "stop": plan.stop,
                            "target": plan.target, "r": plan.risk_per_share, "qty": sizing.qty, "tick": tick,
                            "setup": f["setup"], "until": k + cfg.valid_bars,
                            "risk": sizing.loss_at_stop + sizing.round_trip_cost})
            rest_value += plan.entry * sizing.qty
    # anything left (data ended early)
    for s, p in list(open_pos.items()):
        close(p, bars[s][-1].close, "close", bars[s][-1].opens_at)
    log.extend(trades)
    return {"day": d.isoformat(), "start": float(cash), "gross": float(realised_gross),
            "net": float(realised_net), "end": float(cash + realised_net), "trades": len(trades),
            "wins": sum(1 for x in trades if x["net"] > 0), "halted": halted,
            "charges": float(sum(x["charges"] for x in trades))}


def run(data, cfg: Config, start: date, end: date) -> dict:
    days = trading_days(data)
    cash = cfg.capital
    daily, trades = [], []
    for i, d in enumerate(days):
        if d < start or d > end:
            continue
        book = book_for(data, days, i, cfg) if cfg.use_book else None
        r = run_day(data, d, days[:i], book, cash, cfg, trades)
        if book:
            r["active"] = [k for k, v in book["variants"].items() if v["status"] == "active"]
        daily.append(r)
        cash = D(str(r["end"]))
    net = cash - cfg.capital
    peak, dd = cfg.capital, D(0)
    c = cfg.capital
    for r in daily:
        c = D(str(r["end"]))
        peak = max(peak, c)
        dd = max(dd, peak - c)
    return {"config": cfg.name, "start": float(cfg.capital), "end": float(cash), "net": float(net),
            "return_pct": float(net / cfg.capital * 100), "days": len(daily),
            "green_days": sum(1 for r in daily if r["net"] > 0), "red_days": sum(1 for r in daily if r["net"] < 0),
            "trades": len(trades), "wins": sum(1 for t in trades if t["net"] > 0),
            "gross": sum(t["gross"] for t in trades), "charges": sum(t["charges"] for t in trades),
            "max_drawdown": float(dd), "daily": daily, "trade_log": trades}


if __name__ == "__main__":
    data = load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "candles.json.gz"))
    days = trading_days(data)
    print(len(days), days[0], days[-1])
