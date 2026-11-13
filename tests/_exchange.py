"""
A simulated NSE exchange + Shoonya account, for replaying whole trading days
through the REAL fill monitor, scan and flatten code.

The exchange behaves like the real one where it matters for our bugs:
  * LMT and SL-LMT only; SL triggers on the last price, then rests as a limit
  * rejects: price not a multiple of the tick, outside the circuit band, a
    stop whose trigger is already through the price, margin shortfall
  * cancel/modify of an order that is no longer open fails (the real race)
  * chaos (optional): partial fills, a PlaceOrder whose reply is lost (the
    order may or may not exist), slow fills
Positions net like Shoonya MIS: one row per token, realised P&L on reductions.

A scripted decider stands in for Claude: it reads the same STATE the model
gets and answers with plausible, sometimes sloppy, sometimes hostile JSON.

Invariants are checked after every fill-monitor tick; a violation is a bug.
"""

from __future__ import annotations

import json
import random
import traceback
from datetime import datetime, timedelta
from decimal import Decimal as D
from typing import Dict, List, Optional

import session as sess
from market import Candle, to_tick
from shoonya import Instrument, NetworkError, ShoonyaError

STEP = timedelta(seconds=10)


def _ts(t: datetime) -> str:
    return t.strftime("%H:%M:%S %d-%m-%Y")


class SimExchange:
    def __init__(self, day_candles: Dict[str, List[Candle]], ticks: Dict[str, D], *, cash="26000",
                 seed: int = 1, chaos: float = 0.0, leverage: D = D(5)):
        self.rng = random.Random(seed)
        self.chaos = chaos
        self.lev = leverage
        self._cash = D(cash)
        self.now: Optional[datetime] = None
        self.insts = {}
        self.path: Dict[str, list] = {}
        self.candles_all = day_candles
        for i, (sym, cs) in enumerate(sorted(day_candles.items())):
            self.insts[sym] = Instrument(sym, f"{sym}-EQ", str(1000 + i), "NSE", ticks[sym], 1)
            self.path[sym] = self._make_path(cs, ticks[sym])
        self.by_token = {i.token: s for s, i in self.insts.items()}
        self.ref = {s: cs[0].open for s, cs in day_candles.items()}            # circuit band reference
        self._orders: List[dict] = []
        self.pos: Dict[str, dict] = {}
        self.n = 0
        self.rejects: List[str] = []
        self.lost_replies = 0

    # ---------------------------------------------------------------- prices
    def _make_path(self, cs: List[Candle], tick: D) -> list:
        """(time, price) every 10 s through each candle: open, then the extreme
        nearer the open, then the other, then close."""
        out = []
        for c in cs:
            first, second = (c.low, c.high) if (c.open - c.low) < (c.high - c.open) else (c.high, c.low)
            pts = [c.open, first, second, c.close]
            n = 30                                       # 30 steps of 10 s = 5 min
            for k in range(n):
                seg = k * 3 / n
                i = min(int(seg), 2)
                f = D(str(seg - i))
                px = pts[i] + (pts[i + 1] - pts[i]) * f
                out.append((c.opens_at + STEP * k, to_tick(px, tick)))
        return out

    def ltp(self, sym: str) -> D:
        p = self.path[sym]
        if self.now is None or self.now < p[0][0]:
            return p[0][1]
        idx = min(int((self.now - p[0][0]) / STEP), len(p) - 1)
        return p[idx][1]

    def band(self, sym):
        r = self.ref[sym]
        t = self.insts[sym].tick
        return to_tick(r * D("0.8"), t, mode="up"), to_tick(r * D("1.2"), t, mode="down")

    # ---------------------------------------------------------------- matching
    def step(self, now: datetime) -> None:
        self.now = now
        for o in self._orders:
            if not o["pending"]:
                continue
            if o.get("not_before") and now < o["not_before"]:
                continue                                 # still on its way to the exchange / in the queue
            sym = self.by_token[o["token"]]
            px = self.ltp(sym)
            if o["status"] == "TRIGGER_PENDING":
                hit = px >= o["trigger"] if o["side"] == "B" else px <= o["trigger"]
                if not hit:
                    continue
                o["status"] = "OPEN"
            ok = px <= o["price"] if o["side"] == "B" else px >= o["price"]
            if not ok:
                continue
            left = o["qty"] - o["filled"]
            q = left
            if self.chaos and left > 1 and self.rng.random() < self.chaos:
                q = max(1, left // 2)                    # a partial fill
            self._fill(o, q, px)

    def _fill(self, o: dict, q: int, px: D) -> None:
        o["avg"] = ((o["avg"] * o["filled"] + px * q) / (o["filled"] + q)).quantize(D("0.01"))
        o["filled"] += q
        if o["filled"] >= o["qty"]:
            o["status"], o["pending"] = "COMPLETE", False
        p = self.pos.setdefault(o["token"], {"qty": 0, "avg": D(0), "realised": D(0), "bq": 0, "bv": D(0),
                                             "sq": 0, "sv": D(0)})
        sgn = 1 if o["side"] == "B" else -1
        if o["side"] == "B":
            p["bq"] += q; p["bv"] += px * q
        else:
            p["sq"] += q; p["sv"] += px * q
        cur = p["qty"]
        if cur == 0 or (cur > 0) == (sgn > 0):
            p["avg"] = ((p["avg"] * abs(cur) + px * q) / (abs(cur) + q)) if (abs(cur) + q) else D(0)
            p["qty"] = cur + sgn * q
        else:
            close_q = min(q, abs(cur))
            p["realised"] += (px - p["avg"]) * close_q * (1 if cur > 0 else -1)
            p["qty"] = cur + sgn * q
            if abs(q) > abs(cur):                        # reversed through zero
                p["avg"] = px
            elif p["qty"] == 0:
                p["avg"] = D(0)

    # ---------------------------------------------------------------- account
    def cash(self):
        return self._cash

    def limits(self):
        return {"cash": str(self._cash)}

    def _margin_used(self) -> D:
        used = D(0)
        for tok, p in self.pos.items():
            if p["qty"]:
                used += abs(p["qty"]) * self.ltp(self.by_token[tok]) / self.lev
        for o in self._orders:
            if o["pending"]:
                cur = self.pos.get(o["token"], {}).get("qty", 0)
                reduces = cur and ((cur > 0) != (o["side"] == "B"))
                if not reduces:
                    used += (o["qty"] - o["filled"]) * (o["price"] or o["trigger"]) / self.lev
        return used

    def free_margin(self):
        return self._cash - self._margin_used()

    def order_margin(self, inst, *, side, qty, price, trigger=None):
        return (D(price) * qty / self.lev).quantize(D("0.01"))

    def positions(self, intraday_only=True):
        out = []
        for tok, p in self.pos.items():
            sym = self.by_token[tok]
            ltp = self.ltp(sym)
            out.append({"symbol": sym, "tsym": f"{sym}-EQ", "token": tok, "exch": "NSE", "product": "I",
                        "qty": p["qty"], "avg": p["avg"].quantize(D("0.01")), "ltp": ltp,
                        "realised": p["realised"].quantize(D("0.01")),
                        "unrealised": ((ltp - p["avg"]) * p["qty"]).quantize(D("0.01")),
                        "buy_qty": p["bq"], "buy_avg": (p["bv"] / p["bq"]).quantize(D("0.01")) if p["bq"] else D(0),
                        "sell_qty": p["sq"], "sell_avg": (p["sv"] / p["sq"]).quantize(D("0.01")) if p["sq"] else D(0),
                        "tick": D(0)})                    # Shoonya's position rows omit the tick: the harder case
        return out

    def orders(self):
        return [dict(o) for o in self._orders]

    # ---------------------------------------------------------------- market data
    def quote(self, token, exch="NSE"):
        sym = self.by_token[str(token)]
        px = self.ltp(sym)
        t = self.insts[sym].tick
        lc, uc = self.band(sym)
        done = [c for c in self.candles_all[sym] if c.opens_at <= self.now]
        return {"ltp": px, "uc": uc, "lc": lc, "bid": px - t, "ask": px + t,
                "open": done[0].open if done else px, "high": max([c.high for c in done] + [px]),
                "low": min([c.low for c in done] + [px]), "volume": 0, "tick": t}

    def ltps(self, tokens, exch="NSE"):
        return {str(t): self.ltp(self.by_token[str(t)]) for t in tokens if str(t) in self.by_token}

    def instrument(self, symbol, exch="NSE"):
        s = symbol.upper()
        if s not in self.insts:
            raise KeyError(f"{symbol} not found")
        return self.insts[s]

    def instrument_for(self, row):
        return self.insts[row["symbol"]]

    def candles(self, token, start, end, interval=5, exch="NSE"):
        sym = self.by_token[str(token)]
        return [c for c in self.candles_all[sym] if start <= c.opens_at and c.opens_at + timedelta(minutes=5) <= self.now]

    def search(self, text, exch="NSE"):
        return []

    def call(self, *a, **k):
        raise ShoonyaError("not simulated")

    def marketable(self, inst, side, q=None):
        q = q or self.quote(inst.token)
        lc, uc = self.band(inst.symbol)
        if side == "S":
            return max(to_tick(q["bid"] * D("0.997"), inst.tick, mode="down"), lc)
        return min(to_tick(q["ask"] * D("1.003"), inst.tick, mode="up"), uc)

    # ---------------------------------------------------------------- orders
    def _reject(self, o: dict, why: str) -> None:
        o["status"], o["pending"], o["dead"], o["reason"] = "REJECTED", False, True, why
        self.rejects.append(f"{o['symbol']} {o['remarks'][:12]}: {why}")

    def place(self, inst, *, side, qty, price, trigger=None, remarks=""):
        if side not in ("B", "S") or qty <= 0:
            raise ShoonyaError("bad order")
        self.n += 1
        oid = f"{self.now:%H%M%S}{self.n:04d}"
        price, trig = D(price), (None if trigger is None else D(trigger))
        o = {"id": oid, "symbol": inst.symbol, "tsym": inst.tsym, "token": inst.token, "exch": "NSE",
             "side": side, "type": "SL-LMT" if trig is not None else "LMT", "product": "I",
             "status": "TRIGGER_PENDING" if trig is not None else "OPEN", "pending": True, "dead": False,
             "qty": qty, "filled": 0, "avg": D(0), "price": price, "trigger": trig or D(0),
             "remarks": remarks[:60], "reason": "", "time": _ts(self.now)}
        if self.chaos and self.rng.random() < self.chaos / 3:
            # The request never reached the exchange. (A lost REPLY to an order that
            # did arrive is recovered inside the real Shoonya.place, tested separately.)
            self.lost_replies += 1
            raise NetworkError("PlaceOrder: network: timed out")
        if self.chaos and self.rng.random() < self.chaos * 2:
            o["not_before"] = self.now + timedelta(seconds=self.rng.choice([10, 20, 30]))   # a slow fill
        self._orders.append(o)
        self._validate(o)
        if o["pending"] and o["type"] == "LMT":
            self.step(self.now)                          # a marketable limit fills at once
        return oid

    def _validate(self, o: dict) -> None:
        sym = o["symbol"]
        t = self.insts[sym].tick
        lc, uc = self.band(sym)
        px = self.ltp(sym)
        for name in ("price", "trigger"):
            v = o[name]
            if v and (v / t) != (v / t).to_integral_value():
                return self._reject(o, f"{name} {v} not a multiple of tick {t}")
        if not (lc <= o["price"] <= uc):
            return self._reject(o, f"price {o['price']} outside band {lc}-{uc}")
        if o["type"] == "SL-LMT":
            if o["side"] == "S" and o["trigger"] >= px + 0 and o["trigger"] > px:
                return self._reject(o, f"sell stop trigger {o['trigger']} above LTP {px}")
            if o["side"] == "B" and o["trigger"] < px:
                return self._reject(o, f"buy stop trigger {o['trigger']} below LTP {px}")
            if o["side"] == "S" and o["price"] > o["trigger"]:
                return self._reject(o, "sell stop limit above trigger")
            if o["side"] == "B" and o["price"] < o["trigger"]:
                return self._reject(o, "buy stop limit below trigger")
        cur = self.pos.get(o["token"], {}).get("qty", 0)
        reduces = cur and ((cur > 0) != (o["side"] == "B"))
        if not reduces and self._margin_used() > self._cash:
            return self._reject(o, "RMS: margin shortfall")

    def modify(self, o, inst, *, qty, price, trigger=None):
        cur = next((x for x in self._orders if x["id"] == o["id"]), None)
        if cur is None or not cur["pending"]:
            raise ShoonyaError("ModifyOrder: order is not open")
        if qty < cur["filled"]:
            raise ShoonyaError("ModifyOrder: qty below filled")
        backup = dict(cur)
        cur["qty"], cur["price"] = qty, D(price)
        if trigger is not None:
            cur["trigger"] = D(trigger)
        self._validate(cur)
        if not cur["pending"]:                           # a rejected modify leaves the order as it was
            cur.update(backup)
            raise ShoonyaError(f"ModifyOrder rejected: {self.rejects[-1]}")
        if cur["type"] == "LMT":
            self.step(self.now)
        return o["id"]

    def cancel(self, oid):
        cur = next((x for x in self._orders if x["id"] == str(oid)), None)
        if cur is None or not cur["pending"]:
            raise ShoonyaError("CancelOrder: order is not open")
        cur["status"], cur["pending"], cur["dead"] = "CANCELED", False, True


# -------------------------------------------------------------------- the decider

class Decider:
    """Stands in for Claude: plausible decisions, some sloppy, some hostile."""

    def __init__(self, ex: SimExchange, seed: int = 7, hostile: float = 0.1):
        self.ex = ex
        self.rng = random.Random(seed)
        self.hostile = hostile

    def __call__(self, prompt: str) -> str:
        ctx = json.loads(prompt.split("STATE:\n", 1)[1])
        r = self.rng
        out = []
        for p in ctx.get("positions") or []:
            sym = p.get("symbol")
            x = r.random()
            ltp = self.ex.ltp(sym)
            long_ = (p.get("qty") or 0) > 0 if isinstance(p.get("qty"), (int, float)) else str(p.get("side", "")).upper() in ("B", "BUY", "LONG")
            if x < 0.15:
                out.append({"action": "exit", "symbol": sym, "reason": "sim exit"})
            elif x < 0.45:
                k = D(str(r.uniform(0.002, 0.008)))
                stop = ltp * (1 - k) if long_ else ltp * (1 + k)
                tgt = ltp * (1 + k * 3) if long_ else ltp * (1 - k * 3)
                out.append({"action": "adjust", "symbol": sym, "stop": float(stop), "target": float(tgt),
                            "reason": "sim adjust"})
        for o in (ctx.get("resting_entries") or []):
            if r.random() < 0.2:
                out.append({"action": "cancel", "order_id": o.get("order_id"), "reason": "sim cancel"})
        for _ in range(r.randint(0, 3)):
            sym = r.choice(sorted(self.ex.insts))
            ltp = self.ex.ltp(sym)
            side = r.choice(["BUY", "SELL"])
            kind = r.choice(["stop", "stop", "now", "limit"])
            off = D(str(r.uniform(0.0005, 0.004)))
            risk = D(str(r.uniform(0.003, 0.009)))
            up = side == "BUY"
            entry = ltp * (1 + off if up else 1 - off) if kind == "stop" else (ltp * (1 - off if up else 1 + off) if kind == "limit" else ltp)
            stop = entry * (1 - risk if up else 1 + risk)
            tgt = entry * (1 + risk * 4 if up else 1 - risk * 4)
            d = {"action": "enter", "symbol": sym, "side": side, "order": kind, "entry": float(entry),
                 "stop": float(stop), "target": float(tgt), "size": r.choice([0.5, 1.0]),
                 "valid_minutes": r.choice([15, 30, 60]), "setup": "orb", "lessons": ["L001"], "reason": "sim"}
            if r.random() < self.hostile:
                bad = r.choice(["side", "stop_wrong", "nan", "huge", "missing", "neg"])
                if bad == "side":
                    d["side"] = r.choice(["", "HOLD", "buyy", None])
                elif bad == "stop_wrong":
                    d["stop"] = d["target"]
                elif bad == "nan":
                    d["entry"] = "abc"
                elif bad == "huge":
                    d["size"] = 50
                    d["qty"] = 100000
                elif bad == "missing":
                    d.pop("stop")
                elif bad == "neg":
                    d["stop"] = -5
            out.append(d)
        if r.random() < self.hostile / 3:
            return "I think the market looks interesting today."           # not JSON at all
        return json.dumps({"view": "sim", "decisions": out})


# -------------------------------------------------------------------- invariants

class Checker:
    def __init__(self, ex: SimExchange):
        self.ex = ex
        self.violations: List[str] = []
        self.unprotected_since: Dict[str, datetime] = {}
        self.orphan_since: Dict[str, datetime] = {}

    def flag(self, what: str) -> None:
        if len(self.violations) < 200:
            self.violations.append(f"{self.ex.now:%H:%M:%S} {what}")

    def check(self, placements: List[dict], now: datetime, session) -> None:
        ex = self.ex
        entry_ids = {str(p.get("order_id")) for p in placements}
        bot_tokens = {str(p.get("token")) for p in placements}
        for tok, p in ex.pos.items():
            q = p["qty"]
            sym = ex.by_token[tok]
            closing = [o for o in ex._orders if o["pending"] and o["token"] == tok and o["id"] not in entry_ids
                       and q != 0 and o["side"] == ("S" if q > 0 else "B")]
            pend = sum(o["qty"] - o["filled"] for o in closing)
            if q != 0 and pend > abs(q):
                self.flag(f"OVER-COVER {sym}: position {q}, pending closing {pend} "
                          f"({', '.join(o['remarks'] for o in closing)})")
            # a resting stop on a flat stock opens a new position if it triggers
            stray = [o for o in ex._orders if o["pending"] and o["token"] == tok and o["type"] == "SL-LMT"
                     and o["id"] not in entry_ids and (q == 0 or (q > 0) == (o["side"] == "B"))]
            if stray:
                since = self.orphan_since.setdefault(tok, now)
                if now - since > timedelta(seconds=30):
                    self.flag(f"ORPHAN STOP {sym}: position {q}, stop {stray[0]['remarks']} resting {now - since}")
                    self.orphan_since[tok] = now
            else:
                self.orphan_since.pop(tok, None)
            # an open bot position must have a stop or a working exit within 30 s
            if q != 0 and tok in bot_tokens and now < session.flat_by:
                protected = any(o["pending"] and o["token"] == tok and o["id"] not in entry_ids
                                and o["side"] == ("S" if q > 0 else "B") for o in ex._orders)
                if not protected:
                    since = self.unprotected_since.setdefault(tok, now)
                    if now - since > timedelta(seconds=30):
                        self.flag(f"UNPROTECTED {sym}: position {q} with no stop or exit for {now - since}")
                        self.unprotected_since[tok] = now
                else:
                    self.unprotected_since.pop(tok, None)
            else:
                self.unprotected_since.pop(tok, None)

    def end_of_day(self, session) -> None:
        for tok, p in self.ex.pos.items():
            if p["qty"] != 0:
                self.flag(f"NOT FLAT {self.ex.by_token[tok]}: {p['qty']} after the flatten")
        left = [o for o in self.ex._orders if o["pending"]]
        if left:
            self.flag(f"ORDERS LEFT after the flatten: {[o['remarks'] for o in left]}")


# -------------------------------------------------------------------- one day

def run_day(day_candles, ticks, *, seed=1, chaos=0.0, hostile=0.1, scans=True, state=None) -> dict:
    """Replay one trading day through the real code. Returns violations, errors, stats."""
    import autotrader
    import brain
    import common
    import fillmonitor as fm
    import flatten

    day = next(iter(day_candles.values()))[0].opens_at.date()
    session = sess.Session(day)
    ex = SimExchange(day_candles, ticks, seed=seed, chaos=chaos)
    chk = Checker(ex)
    decide_run = Decider(ex, seed=seed + 100, hostile=hostile)
    errors: List[str] = []
    clock = {"t": session.open - timedelta(minutes=6)}
    sess.now = lambda clock_=None: clock["t"]
    brain.find_claude = lambda: "/sim/claude"
    flatten.WAIT_S = 6
    state = {} if state is None else state             # one fill-monitor process can live for days

    def sim_sleep(s):
        """Waiting moves the market on: the exchange keeps matching."""
        clock["t"] = clock["t"] + timedelta(seconds=float(s))
        ex.step(clock["t"])
    flatten.run.__kwdefaults__["sleep"] = sim_sleep
    flatten.close_position.__kwdefaults__["sleep"] = sim_sleep
    end = session.flat_by + timedelta(minutes=12)
    last_scan = None
    while clock["t"] <= end:
        now = clock["t"]
        ex.step(now)
        try:
            fm.tick(ex, now, state)
        except Exception as exc:                        # noqa: BLE001
            errors.append(f"{now:%H:%M:%S} fill monitor: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}")
        if scans and now.minute % 5 == 0 and now.second == 30 and session.open <= now < session.flat_by \
                and last_scan != now:
            last_scan = now
            try:
                autotrader.run(ex, now=now, decide=lambda *a, **k: brain.decide(*a, **k, run=decide_run))
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{now:%H:%M:%S} scan: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}")
        if now.strftime("%H:%M:%S") in ("15:08:00", "15:14:00"):
            try:
                flatten.run(ex, now=now)
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{now:%H:%M:%S} flatten timer: {type(exc).__name__}: {exc}")
        chk.check(common.events("placed"), clock["t"], session)
        clock["t"] = max(clock["t"], now) + STEP
        clock["t"] = clock["t"].replace(second=clock["t"].second - clock["t"].second % 10, microsecond=0)
    chk.end_of_day(session)
    net = sum((p["realised"] for p in ex.pos.values()), D(0))
    return {"day": day.isoformat(), "violations": chk.violations, "errors": errors, "rejects": ex.rejects,
            "orders": len(ex._orders), "gross": net, "lost_replies": ex.lost_replies}
