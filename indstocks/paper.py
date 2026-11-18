"""
The paper bench: rule-based models trading on live prices with paper money,
side by side with the real account. No broker calls of any kind: prices come
from the tape (tape.py's live.json and its per-symbol recordings), fills are
simulated with a tick of slippage, and every round trip pays Shoonya's real
intraday charges (charges.py).

  models    each has its own Rs 10,000 at 5x (Rs 50,000 a trade), one open
            trade per symbol, its own daily kill (TRADER_PAPER_KILL, Rs 75),
            entries 09:30-14:35, everything flat at 14:50.
  bars      1-minute bars built from the tape; on a restart the day's bars
            are rebuilt from the tape recordings, so nothing is lost.
  ledger    STATE_DIR/paper/DAY.jsonl, one line per open and close;
            STATE_DIR/paper/DAY-summary.json, per-model P&L after charges.

The point is the comparison: which rules would have made money today, in
this market, next to what the AI did with real money. Models that prove
themselves here earn real capital later; nothing here can place an order.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal as D
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import charges as charges_mod  # noqa: E402
import common  # noqa: E402
import session as sess  # noqa: E402

PAPER_DIR = common.STATE_DIR / "paper"
CAPITAL = D("10000")
LEVERAGE = D("5")
ENTRY_FROM, ENTRY_UNTIL, FLAT_AT = (9, 30), (14, 35), (14, 50)


# ------------------------------------------------------------------ bars

@dataclass
class Bar:
    start: int          # epoch minute start
    o: float
    h: float
    l: float
    c: float
    v: float            # volume traded in the minute


class Series:
    """One symbol's 1-minute bars for the day, from (time, price, cum volume) ticks."""

    def __init__(self):
        self.bars: List[Bar] = []
        self.last_cum: Optional[float] = None

    def add(self, t: float, px: float, cum: Optional[float]) -> bool:
        """Add a tick; True when it opened a new minute (the previous bar closed)."""
        m = int(t // 60) * 60
        dv = 0.0
        if cum is not None:
            if self.last_cum is not None and cum >= self.last_cum:
                dv = cum - self.last_cum
            self.last_cum = cum
        if self.bars and self.bars[-1].start == m:
            b = self.bars[-1]
            b.h, b.l, b.c, b.v = max(b.h, px), min(b.l, px), px, b.v + dv
            return False
        if self.bars and m < self.bars[-1].start:
            return False                                   # out-of-order tick
        self.bars.append(Bar(m, px, px, px, px, dv))
        return len(self.bars) > 1

    def closed(self) -> List[Bar]:
        return self.bars[:-1]


def ema(xs: List[float], n: int) -> Optional[float]:
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def vwap_sd(bars: List[Bar]):
    pv = sum(((b.h + b.l + b.c) / 3) * b.v for b in bars)
    vol = sum(b.v for b in bars)
    if vol <= 0:
        return None, None
    vw = pv / vol
    var = sum((((b.h + b.l + b.c) / 3) - vw) ** 2 * b.v for b in bars) / vol
    return vw, var ** 0.5


def rsi(closes: List[float], n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(len(closes) - n, len(closes))]
    g, l_ = sum(gains) / n, sum(losses) / n
    return 100.0 if l_ == 0 else 100 - 100 / (1 + g / l_)


def atr1(bars: List[Bar], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / n


def resample(bars: List[Bar], k: int) -> List[Bar]:
    out: List[Bar] = []
    for b in bars:
        s = (b.start // (60 * k)) * 60 * k
        if out and out[-1].start == s:
            o = out[-1]
            o.h, o.l, o.c, o.v = max(o.h, b.h), min(o.l, b.l), b.c, o.v + b.v
        else:
            out.append(Bar(s, b.o, b.h, b.l, b.c, b.v))
    return out


def _hm(epoch: float):
    t = datetime.fromtimestamp(epoch, sess.IST)
    return t.hour, t.minute


# ------------------------------------------------------------------ models
# A signal: ("B" | "S", stop_price, target_price) or None. Called on each
# closed 1-minute bar with the day's closed bars so far.

Signal = Optional[tuple]


def _orb(minutes: int):
    def f(bars: List[Bar], state: dict) -> Signal:
        if state.get("done"):
            return None
        rng = [b for b in bars if _hm(b.start) < (9, 15 + minutes)]
        if len(rng) < minutes * 0.6 or _hm(bars[-1].start) < (9, 15 + minutes):
            return None
        hi, lo = max(b.h for b in rng), min(b.l for b in rng)
        c = bars[-1].c
        width = hi - lo
        if width <= 0 or width / c > 0.012:
            return None
        if c > hi:
            state["done"] = True
            return ("B", lo + width / 2, c + 2 * width)
        if c < lo:
            state["done"] = True
            return ("S", hi - width / 2, c - 2 * width)
        return None
    return f


def vwap_revert(bars, state) -> Signal:
    if len(bars) < 30:
        return None
    vw, sd = vwap_sd(bars)
    if not vw or not sd:
        return None
    c = bars[-1].c
    if c < vw - 2 * sd:
        return ("B", c - 1.0 * sd, vw)
    if c > vw + 2 * sd:
        return ("S", c + 1.0 * sd, vw)
    return None


def vwap_trend(bars, state) -> Signal:
    if len(bars) < 30:
        return None
    vw, sd = vwap_sd(bars)
    a = atr1(bars)
    if not vw or not a:
        return None
    last3 = bars[-3:]
    c = bars[-1].c
    if all(b.c > vw for b in last3) and bars[-4].c <= vw:
        return ("B", c - 3 * a, c + 6 * a)
    if all(b.c < vw for b in last3) and bars[-4].c >= vw:
        return ("S", c + 3 * a, c - 6 * a)
    return None


def ema_cross(bars, state) -> Signal:
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < 23:
        return None
    f0, s0 = ema(closes[:-1], 9), ema(closes[:-1], 21)
    f1, s1 = ema(closes, 9), ema(closes, 21)
    if None in (f0, s0, f1, s1) or len(bars) % 5:
        return None
    a = atr1(b5, 10)
    if not a:
        return None
    c = closes[-1]
    if f0 <= s0 and f1 > s1:
        return ("B", c - 1.5 * a, c + 3 * a)
    if f0 >= s0 and f1 < s1:
        return ("S", c + 1.5 * a, c - 3 * a)
    return None


def rsi2(bars, state) -> Signal:
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < 22 or len(bars) % 5:
        return None
    r = rsi(closes, 2)
    trend = ema(closes, 20)
    a = atr1(b5, 10)
    if r is None or trend is None or not a:
        return None
    c = closes[-1]
    if r < 10 and c > trend:
        return ("B", c - 1.5 * a, c + 1.5 * a)
    if r > 90 and c < trend:
        return ("S", c + 1.5 * a, c - 1.5 * a)
    return None


def bollinger_fade(bars, state) -> Signal:
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < 21 or len(bars) % 5:
        return None
    w = closes[-20:]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    if sd <= 0:
        return None
    c = closes[-1]
    if c < m - 2 * sd:
        return ("B", c - sd, m)
    if c > m + 2 * sd:
        return ("S", c + sd, m)
    return None


def squeeze_break(bars, state) -> Signal:
    b5 = resample(bars, 5)
    if len(b5) < 22 or len(bars) % 5:
        return None
    closes = [b.c for b in b5]
    w = closes[-21:-1]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    a = atr1(b5[:-1], 20)
    if not a or sd <= 0:
        return None
    squeezed = 2 * sd < 1.5 * a                         # Bollinger inside Keltner
    c = closes[-1]
    if squeezed and c > m + 2 * sd:
        return ("B", c - 1.5 * a, c + 3 * a)
    if squeezed and c < m - 2 * sd:
        return ("S", c + 1.5 * a, c - 3 * a)
    return None


def volume_burst(bars, state) -> Signal:
    if len(bars) < 30:
        return None
    avg = sum(b.v for b in bars[-31:-1]) / 30
    b = bars[-1]
    a = atr1(bars)
    if not a or avg <= 0 or b.v < 3 * avg:
        return None
    body = b.c - b.o
    if abs(body) < 1.5 * a:
        return None
    if body > 0:
        return ("B", b.l, b.c + 2 * (b.c - b.l))
    return ("S", b.h, b.c - 2 * (b.h - b.c))


def opening_drive_fade(bars, state) -> Signal:
    """The first 30 minutes moved hard one way; fade the first bar that closes back inside."""
    if state.get("done") or _hm(bars[-1].start) < (9, 45):
        return None
    first = [b for b in bars if _hm(b.start) < (9, 45)]
    if len(first) < 15:
        return None
    o = first[0].o
    hi, lo = max(b.h for b in first), min(b.l for b in first)
    c = bars[-1].c
    if (hi - o) / o > 0.008 and c < hi - 0.5 * (hi - o):
        state["done"] = True
        return ("S", hi, o)
    if (o - lo) / o > 0.008 and c > lo + 0.5 * (o - lo):
        state["done"] = True
        return ("B", lo, o)
    return None


MODELS: Dict[str, Callable] = {
    "orb5": _orb(5), "orb15": _orb(15), "orb30": _orb(30),
    "vwap_revert": vwap_revert, "vwap_trend": vwap_trend,
    "ema_9_21": ema_cross, "rsi2": rsi2, "bollinger_fade": bollinger_fade,
    "squeeze_break": squeeze_break, "volume_burst": volume_burst,
    "opening_drive_fade": opening_drive_fade,
}


# ------------------------------------------------------------------ the book

def tick_of(px: float) -> float:
    return 0.01 if px < 500 else 0.05


def round_trip_net(side: str, entry: float, exit_: float, qty: int) -> D:
    e, x = D(str(round(entry, 2))), D(str(round(exit_, 2)))
    gross = (x - e) * qty if side == "B" else (e - x) * qty
    buy, sell = (e * qty, x * qty) if side == "B" else (x * qty, e * qty)
    return (gross - charges_mod.round_trip(buy, sell).total).quantize(D("0.01"))


@dataclass
class Trade:
    model: str
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    opened: float
    best: float = 0.0


@dataclass
class Bench:
    day: str
    series: Dict[str, Series] = field(default_factory=dict)
    open: Dict[tuple, Trade] = field(default_factory=dict)
    realised: Dict[str, D] = field(default_factory=dict)
    trades: Dict[str, int] = field(default_factory=dict)
    wins: Dict[str, int] = field(default_factory=dict)
    killed: set = field(default_factory=set)
    mstate: Dict[tuple, dict] = field(default_factory=dict)
    write: bool = True

    def kill_rs(self) -> D:
        try:
            return D(common.setting("TRADER_PAPER_KILL", "75"))
        except ArithmeticError:
            return D("75")

    def _ledger(self, rec: dict) -> None:
        if not self.write:
            return
        PAPER_DIR.mkdir(parents=True, exist_ok=True)
        with open(PAPER_DIR / f"{self.day}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def model_open_net(self, model: str, prices: Dict[str, float]) -> D:
        tot = D(0)
        for (m, s), t in self.open.items():
            if m == model and s in prices:
                tot += round_trip_net(t.side, t.entry, prices[s], t.qty)
        return tot

    def close(self, key: tuple, px: float, why: str, at: float) -> None:
        t = self.open.pop(key)
        slip = tick_of(px)
        x = px - slip if t.side == "B" else px + slip
        net = round_trip_net(t.side, t.entry, x, t.qty)
        self.realised[t.model] = self.realised.get(t.model, D(0)) + net
        self.trades[t.model] = self.trades.get(t.model, 0) + 1
        if net > 0:
            self.wins[t.model] = self.wins.get(t.model, 0) + 1
        self._ledger({"ev": "close", "t": round(at, 1), "model": t.model, "symbol": t.symbol, "side": t.side,
                      "qty": t.qty, "entry": t.entry, "exit": round(x, 2), "net": str(net), "why": why})

    def on_price(self, sym: str, px: float, at: float) -> None:
        """Stops, targets and the day's flat time, checked on every price."""
        hm = _hm(at)
        for key in [k for k, t in self.open.items() if t.symbol == sym]:
            t = self.open[key]
            if hm >= FLAT_AT:
                self.close(key, px, "flat 14:50", at)
            elif (t.side == "B" and px <= t.stop) or (t.side == "S" and px >= t.stop):
                self.close(key, px, "stop", at)
            elif (t.side == "B" and px >= t.target) or (t.side == "S" and px <= t.target):
                self.close(key, px, "target", at)

    def check_kills(self, prices: Dict[str, float], at: float) -> None:
        for m in MODELS:
            if m in self.killed:
                continue
            day = self.realised.get(m, D(0)) + self.model_open_net(m, prices)
            if day <= -self.kill_rs():
                for key in [k for k in self.open if k[0] == m]:
                    s = key[1]
                    self.close(key, prices.get(s, self.open[key].entry), "model kill", at)
                self.killed.add(m)
                self._ledger({"ev": "kill", "t": round(at, 1), "model": m, "day": str(day)})

    def on_bar(self, sym: str, at: float) -> None:
        """A 1-minute bar closed for sym: ask every model."""
        hm = _hm(at)
        if not (ENTRY_FROM <= hm < ENTRY_UNTIL):
            return
        bars = self.series[sym].closed()
        if len(bars) < 5:
            return
        px = bars[-1].c
        for m, fn in MODELS.items():
            if m in self.killed or (m, sym) in self.open:
                continue
            st = self.mstate.setdefault((m, sym), {})
            try:
                sig = fn(bars, st)
            except Exception:                                  # noqa: BLE001
                sig = None
            if not sig:
                continue
            side, stop, target = sig
            if (side == "B" and not (stop < px < target)) or (side == "S" and not (target < px < stop)):
                continue
            slip = tick_of(px)
            entry = px + slip if side == "B" else px - slip
            qty = int(float(CAPITAL * LEVERAGE) // entry)
            if qty < 1:
                continue
            self.open[(m, sym)] = Trade(m, sym, side, qty, round(entry, 2), round(stop, 2), round(target, 2), at)
            self._ledger({"ev": "open", "t": round(at, 1), "model": m, "symbol": sym, "side": side, "qty": qty,
                          "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2)})

    def feed(self, sym: str, at: float, px: float, cum: Optional[float]) -> None:
        s = self.series.setdefault(sym, Series())
        self.on_price(sym, px, at)
        if s.add(at, px, cum):
            self.on_bar(sym, at)

    def summary(self, prices: Dict[str, float]) -> dict:
        rows = {}
        for m in MODELS:
            op = self.model_open_net(m, prices)
            rows[m] = {"net": str((self.realised.get(m, D(0)) + op).quantize(D("0.01"))),
                       "realised": str(self.realised.get(m, D(0))), "open": str(op.quantize(D("0.01"))),
                       "trades": self.trades.get(m, 0), "wins": self.wins.get(m, 0),
                       "open_trades": sum(1 for k in self.open if k[0] == m), "killed": m in self.killed}
        total = sum((D(r["net"]) for r in rows.values()), D(0))
        return {"day": self.day, "at": time.time(), "total": str(total), "models": rows}


# ------------------------------------------------------------------ the service

def replay_day(bench: Bench, day: str) -> Dict[str, float]:
    """Rebuild today's state from the tape recordings (a restart loses nothing)."""
    d = common.STATE_DIR / "tape" / day
    ticks = []
    if d.exists():
        for f in d.glob("*.jsonl"):
            sym = f.stem
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("lp") is not None:
                    ticks.append((r["t"], sym, float(r["lp"]), r.get("v")))
    ticks.sort()
    last = {}
    for t, sym, px, v in ticks:
        bench.feed(sym, t, px, float(v) if v is not None else None)
        last[sym] = px
    return last


def main() -> None:
    import tape
    while True:
        now = sess.now()
        hm = (now.hour, now.minute)
        if now.weekday() >= 5 or hm < (9, 15) or hm >= (15, 5):
            time.sleep(60)
            continue
        day = now.strftime("%Y-%m-%d")
        bench = Bench(day)
        PAPER_DIR.mkdir(parents=True, exist_ok=True)
        (PAPER_DIR / f"{day}.jsonl").write_text("")     # the replay rewrites the whole day's ledger
        last = replay_day(bench, day)
        bench._ledger({"ev": "start", "t": time.time(), "replayed_symbols": len(last)})
        seen: Dict[str, float] = {}
        while sess.now().strftime("%Y-%m-%d") == day and (sess.now().hour, sess.now().minute) < (15, 5):
            live = tape.read_live(max_age_s=15)
            for sym, f in live.items():
                if f.get("ltp") is None or seen.get(sym) == f.get("updated"):
                    continue
                seen[sym] = f.get("updated")
                last[sym] = float(f["ltp"])
                bench.feed(sym, float(f["updated"]), float(f["ltp"]), None if f.get("vol_cum") is None else float(f["vol_cum"]))
            bench.check_kills(last, time.time())
            try:
                p = PAPER_DIR / f"{day}-summary.json"
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(bench.summary(last)))
                tmp.replace(p)
            except OSError:
                pass
            time.sleep(1)


if __name__ == "__main__":
    main()
