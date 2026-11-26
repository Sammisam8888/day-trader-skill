"""
The paper bench: rule-based models trading on live prices with paper money,
side by side with the real account. No broker calls of any kind: prices come
from the tape (tape.py's live.json and its per-symbol recordings), fills are
simulated with a tick of slippage, and every round trip pays Shoonya's real
intraday charges (charges.py).

  models    each has its own Rs 10,000 at 5x (at most Rs 50,000 a trade),
            sized so the stop loses TRADER_PAPER_RISK (Rs 40), a target that
            pays its charges twice over, at most 6 trades a day, one open
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
CAPITAL = D("10000")          # default wallet per model; TRADER_BENCH_CAPITAL overrides (owner, 26 Nov: Rs 1 lakh)
LEVERAGE = D("5")


def capital() -> D:
    try:
        return D(common.setting("TRADER_BENCH_CAPITAL", str(CAPITAL)))
    except ArithmeticError:
        return CAPITAL


def target_stops() -> bool:
    """TRADER_TARGET_STOPS (default false, owner 26 Nov: "it should be hungry to book more profits").
    false: the day target is a milestone, logged once, and the model keeps trading.
    true: the old rule, the model is closed and done for the day at the target."""
    return common.setting("TRADER_TARGET_STOPS", "false").strip().lower() in ("1", "true", "yes", "on")


def day_target() -> D:
    """TRADER_BENCH_TARGET: the day net each model aims for (0 = none). See target_stops()."""
    try:
        return D(common.setting("TRADER_BENCH_TARGET", "0"))
    except ArithmeticError:
        return D(0)
ENTRY_FROM, ENTRY_UNTIL, FLAT_AT = (9, 30), (14, 35), (14, 50)
MAX_TRADES = 6          # per model per day: charges punish churn


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
# Every model is a fixed recipe with knobs. A signal is ("B" | "S", stop,
# target) or None; models are asked on each closed 1-minute bar with the
# day's closed bars so far and their knob values `p`.
#
# KNOBS[model] = {knob: (default, low, high, kind)}. The AI (tuner.py) may set
# a knob only inside [low, high]; anything else is clamped. Every model also
# has the shared knobs: enabled (0/1), invert (0/1: take the other side, the
# owner's 'fade it' idea), side (0 both, 1 long only, 2 short only).

Signal = Optional[tuple]

COMMON = {"enabled": (1, 0, 1, "int"), "invert": (0, 0, 1, "int"), "side": (0, 0, 2, "int")}


def m_orb(bars: List[Bar], state: dict, p: dict) -> Signal:
    minutes = int(p["minutes"])
    if state.get("done"):
        return None
    rng = [b for b in bars if _hm(b.start) < (9, 15 + minutes)]
    if len(rng) < minutes * 0.6 or _hm(bars[-1].start) < (9, 15 + minutes):
        return None
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    c = bars[-1].c
    width = hi - lo
    if width <= 0 or width / c * 100 > p["max_width_pct"]:
        return None
    if c > hi:
        state["done"] = True
        return ("B", hi - width * p["stop_frac"], c + p["target_x"] * width)
    if c < lo:
        state["done"] = True
        return ("S", lo + width * p["stop_frac"], c - p["target_x"] * width)
    return None


def m_vwap_revert(bars, state, p) -> Signal:
    if len(bars) < 30:
        return None
    vw, sd = vwap_sd(bars)
    if not vw or not sd:
        return None
    c = bars[-1].c
    if c < vw - p["band_sd"] * sd:
        return ("B", c - p["stop_sd"] * sd, vw)
    if c > vw + p["band_sd"] * sd:
        return ("S", c + p["stop_sd"] * sd, vw)
    return None


def m_vwap_trend(bars, state, p) -> Signal:
    k = int(p["confirm"])
    if len(bars) < 30 + k:
        return None
    vw, sd = vwap_sd(bars)
    a = atr1(bars)
    if not vw or not a:
        return None
    lastk = bars[-k:]
    c = bars[-1].c
    if all(b.c > vw for b in lastk) and bars[-k - 1].c <= vw:
        return ("B", c - p["stop_atr"] * a, c + p["target_atr"] * a)
    if all(b.c < vw for b in lastk) and bars[-k - 1].c >= vw:
        return ("S", c + p["stop_atr"] * a, c - p["target_atr"] * a)
    return None


def m_ema_cross(bars, state, p) -> Signal:
    fast, slow = int(p["fast"]), int(p["slow"])
    if fast >= slow or len(bars) % 5:
        return None
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < slow + 2:
        return None
    f0, s0 = ema(closes[:-1], fast), ema(closes[:-1], slow)
    f1, s1 = ema(closes, fast), ema(closes, slow)
    a = atr1(b5, 10)
    if None in (f0, s0, f1, s1) or not a:
        return None
    c = closes[-1]
    if f0 <= s0 and f1 > s1:
        return ("B", c - p["stop_atr"] * a, c + p["target_atr"] * a)
    if f0 >= s0 and f1 < s1:
        return ("S", c + p["stop_atr"] * a, c - p["target_atr"] * a)
    return None


def m_rsi2(bars, state, p) -> Signal:
    if len(bars) % 5:
        return None
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < 22:
        return None
    r = rsi(closes, 2)
    trend = ema(closes, 20)
    a = atr1(b5, 10)
    if r is None or trend is None or not a:
        return None
    c = closes[-1]
    if r < p["low"] and c > trend:
        return ("B", c - p["stop_atr"] * a, c + p["target_atr"] * a)
    if r > p["high"] and c < trend:
        return ("S", c + p["stop_atr"] * a, c - p["target_atr"] * a)
    return None


def m_bollinger_fade(bars, state, p) -> Signal:
    if len(bars) % 5:
        return None
    b5 = resample(bars, 5)
    closes = [b.c for b in b5]
    if len(closes) < 21:
        return None
    w = closes[-20:]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    if sd <= 0:
        return None
    c = closes[-1]
    if c < m - p["sd"] * sd:
        return ("B", c - p["stop_sd"] * sd, m)
    if c > m + p["sd"] * sd:
        return ("S", c + p["stop_sd"] * sd, m)
    return None


def m_squeeze_break(bars, state, p) -> Signal:
    if len(bars) % 5:
        return None
    b5 = resample(bars, 5)
    if len(b5) < 22:
        return None
    closes = [b.c for b in b5]
    w = closes[-21:-1]
    m = sum(w) / 20
    sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
    a = atr1(b5[:-1], 20)
    if not a or sd <= 0:
        return None
    squeezed = 2 * sd < p["squeeze"] * a                 # Bollinger inside Keltner
    c = closes[-1]
    if squeezed and c > m + 2 * sd:
        return ("B", c - p["stop_atr"] * a, c + p["target_atr"] * a)
    if squeezed and c < m - 2 * sd:
        return ("S", c + p["stop_atr"] * a, c - p["target_atr"] * a)
    return None


def m_volume_burst(bars, state, p) -> Signal:
    if len(bars) < 30:
        return None
    avg = sum(b.v for b in bars[-31:-1]) / 30
    b = bars[-1]
    a = atr1(bars)
    if not a or avg <= 0 or b.v < p["vol_x"] * avg:
        return None
    body = b.c - b.o
    if abs(body) < p["body_atr"] * a:
        return None
    if body > 0:
        return ("B", b.l, b.c + p["target_x"] * (b.c - b.l))
    return ("S", b.h, b.c - p["target_x"] * (b.h - b.c))


def m_opening_drive_fade(bars, state, p) -> Signal:
    """The first 30 minutes moved hard one way; fade it once price gives back `retrace` of the move."""
    if state.get("done") or _hm(bars[-1].start) < (9, 45):
        return None
    first = [b for b in bars if _hm(b.start) < (9, 45)]
    if len(first) < 15:
        return None
    o = first[0].o
    hi, lo = max(b.h for b in first), min(b.l for b in first)
    c = bars[-1].c
    mv = p["move_pct"] / 100
    if (hi - o) / o > mv and c < hi - p["retrace"] * (hi - o):
        state["done"] = True
        return ("S", hi, o)
    if (o - lo) / o > mv and c > lo + p["retrace"] * (o - lo):
        state["done"] = True
        return ("B", lo, o)
    return None


def _swings(seg: List[Bar], kind: str, k: int = 2) -> List[float]:
    out = []
    # strictly beyond the bars before it, at least as far as the bars after: a flat
    # stretch of identical bars is one swing, not a "pool" of equal lows
    for i in range(k, len(seg) - k):
        if kind == "low" and all(seg[i].l < seg[j].l for j in range(i - k, i)) \
                and all(seg[i].l <= seg[j].l for j in range(i + 1, i + k + 1)):
            out.append(seg[i].l)
        if kind == "high" and all(seg[i].h > seg[j].h for j in range(i - k, i)) \
                and all(seg[i].h >= seg[j].h for j in range(i + 1, i + k + 1)):
            out.append(seg[i].h)
    return out


def pools(seg: List[Bar], kind: str, touches: int, tol_pct: float) -> List[float]:
    """Liquidity pools: price levels where `touches` or more swing lows (or highs)
    sit within tol_pct of each other: equal lows / support, where the stop-losses
    of everyone who bought the support cluster (or sold the resistance)."""
    pts = _swings(seg, kind)
    levels: List[float] = []
    for p in pts:
        near = [q for q in pts if abs(q - p) / p * 100 <= tol_pct]
        if len(near) >= touches:
            lv = sum(near) / len(near)
            if all(abs(lv - x) / x * 100 > tol_pct for x in levels):
                levels.append(lv)
    return levels


def m_liq_sweep(bars, state, p) -> Signal:
    """Smart-money liquidity sweep (owner's videos, 26 Nov): price runs through a
    pool of equal lows/highs, takes the stops resting there, and snaps back inside
    within `reclaim_bars`: trade the reversal, stop beyond the sweep's extreme."""
    lb, r = int(p["lookback"]), int(p["reclaim_bars"])
    if len(bars) < lb + r + 6:
        return None
    hist, recent = bars[-(lb + r + 1):-(r + 1)], bars[-(r + 1):]
    used = state.setdefault("used", set())
    last = bars[-1]
    c, sw = last.c, p["sweep_pct"] / 100
    for lv in pools(hist, "low", int(p["touches"]), p["tol_pct"]):
        lo = min(b.l for b in recent)
        if round(lv, 2) in used or not (lo < lv * (1 - sw) and c > lv and last.c > last.o):
            continue
        stop = lo * (1 - 0.0005)
        if c - stop <= 0:
            continue
        used.add(round(lv, 2))
        return ("B", stop, c + p["target_r"] * (c - stop))
    for lv in pools(hist, "high", int(p["touches"]), p["tol_pct"]):
        hi = max(b.h for b in recent)
        if round(lv, 2) in used or not (hi > lv * (1 + sw) and c < lv and last.c < last.o):
            continue
        stop = hi * (1 + 0.0005)
        if stop - c <= 0:
            continue
        used.add(round(lv, 2))
        return ("S", stop, c - p["target_r"] * (stop - c))
    return None


def m_liq_run(bars, state, p) -> Signal:
    """The other side of the same trap: when a pool breaks and price HOLDS beyond it
    for `hold_bars`, the stops have fuelled a real move: ride the hunt, stop back
    inside the pool."""
    lb, h = int(p["lookback"]), int(p["hold_bars"])
    if len(bars) < lb + h + 6:
        return None
    hist, recent = bars[-(lb + h + 1):-(h + 1)], bars[-(h + 1):]
    used = state.setdefault("used", set())
    c, br = bars[-1].c, p["break_pct"] / 100
    for lv in pools(hist, "high", int(p["touches"]), p["tol_pct"]):
        if round(lv, 2) in used:
            continue
        if recent[0].c <= lv * (1 + br) and all(b.c > lv * (1 + br) for b in recent[1:]):
            stop = lv * (1 - br)
            used.add(round(lv, 2))
            return ("B", stop, c + p["target_r"] * (c - stop))
    for lv in pools(hist, "low", int(p["touches"]), p["tol_pct"]):
        if round(lv, 2) in used:
            continue
        if recent[0].c >= lv * (1 - br) and all(b.c < lv * (1 - br) for b in recent[1:]):
            stop = lv * (1 + br)
            used.add(round(lv, 2))
            return ("S", stop, c - p["target_r"] * (stop - c))
    return None


RECIPES: Dict[str, Callable] = {
    "orb": m_orb, "vwap_revert": m_vwap_revert, "vwap_trend": m_vwap_trend, "ema_cross": m_ema_cross,
    "rsi2": m_rsi2, "bollinger_fade": m_bollinger_fade, "squeeze_break": m_squeeze_break,
    "volume_burst": m_volume_burst, "opening_drive_fade": m_opening_drive_fade,
    "liq_sweep": m_liq_sweep, "liq_run": m_liq_run,
}
KNOBS: Dict[str, Dict[str, tuple]] = {
    "orb": {"minutes": (15, 5, 30, "int"), "max_width_pct": (1.2, 0.4, 2.0, "f"),
            "target_x": (2.0, 1.0, 4.0, "f"), "stop_frac": (0.5, 0.25, 1.0, "f")},
    "vwap_revert": {"band_sd": (2.0, 1.5, 3.0, "f"), "stop_sd": (1.0, 0.5, 2.0, "f")},
    "vwap_trend": {"confirm": (3, 2, 6, "int"), "stop_atr": (3.0, 1.5, 5.0, "f"), "target_atr": (6.0, 3.0, 12.0, "f")},
    "ema_cross": {"fast": (9, 5, 13, "int"), "slow": (21, 15, 34, "int"),
                  "stop_atr": (1.5, 1.0, 3.0, "f"), "target_atr": (3.0, 1.5, 6.0, "f")},
    "rsi2": {"low": (10.0, 3.0, 25.0, "f"), "high": (90.0, 75.0, 97.0, "f"),
             "stop_atr": (1.5, 1.0, 3.0, "f"), "target_atr": (1.5, 1.0, 4.0, "f")},
    "bollinger_fade": {"sd": (2.0, 1.5, 3.0, "f"), "stop_sd": (1.0, 0.5, 2.0, "f")},
    "squeeze_break": {"squeeze": (1.5, 1.0, 2.5, "f"), "stop_atr": (1.5, 1.0, 3.0, "f"), "target_atr": (3.0, 1.5, 6.0, "f")},
    "volume_burst": {"vol_x": (3.0, 2.0, 6.0, "f"), "body_atr": (1.5, 0.8, 3.0, "f"), "target_x": (2.0, 1.0, 4.0, "f")},
    "opening_drive_fade": {"move_pct": (0.8, 0.4, 1.6, "f"), "retrace": (0.5, 0.3, 0.8, "f")},
    "liq_sweep": {"lookback": (60, 30, 150, "int"), "touches": (3, 2, 5, "int"), "tol_pct": (0.12, 0.04, 0.3, "f"),
                  "sweep_pct": (0.08, 0.02, 0.4, "f"), "reclaim_bars": (3, 1, 8, "int"), "target_r": (2.0, 1.0, 4.0, "f")},
    "liq_run": {"lookback": (60, 30, 150, "int"), "touches": (3, 2, 5, "int"), "tol_pct": (0.12, 0.04, 0.3, "f"),
                "break_pct": (0.1, 0.03, 0.5, "f"), "hold_bars": (2, 1, 6, "int"), "target_r": (2.0, 1.0, 4.0, "f")},
}


def knobs_for(recipe: str) -> Dict[str, tuple]:
    return {**KNOBS[recipe], **COMMON}


def defaults(recipe: str) -> dict:
    return {k: v[0] for k, v in knobs_for(recipe).items()}


def clamp(recipe: str, values: dict) -> dict:
    """The recipe's knobs with `values` applied inside their tested ranges."""
    out = defaults(recipe)
    for k, (dflt, lo, hi, kind) in knobs_for(recipe).items():
        if k not in (values or {}):
            continue
        try:
            v = float(values[k])
        except (TypeError, ValueError):
            continue
        if v != v:                                     # NaN
            continue
        v = min(hi, max(lo, v))
        out[k] = int(round(v)) if kind == "int" else round(v, 3)
    return out


def apply(recipe: str, bars, state, p) -> Signal:
    """Run one recipe with knobs p, then the shared knobs."""
    if not p.get("enabled", 1):
        return None
    sig = RECIPES[recipe](bars, state, p)
    if not sig:
        return None
    side, stop, target = sig
    if p.get("invert"):
        side, stop, target = ("S" if side == "B" else "B"), target, stop
    if p.get("side") == 1 and side != "B" or p.get("side") == 2 and side != "S":
        return None
    return (side, stop, target)


def fixed(recipe: str, **over) -> Callable:
    p = clamp(recipe, over)
    return lambda bars, state: apply(recipe, bars, state, p)


# The AI's knob settings for today, written by tuner.py; re-read when the file changes.
_AI = {"mtime": None, "day": None, "params": {}}


def ai_params(recipe: str) -> dict:
    day = sess.now().strftime("%Y-%m-%d")
    f = PAPER_DIR / f"params-{day}.json"
    try:
        mt = f.stat().st_mtime
    except OSError:
        mt = None
    if mt != _AI["mtime"] or day != _AI["day"]:
        _AI.update(mtime=mt, day=day, params={})
        if mt is not None:
            try:
                _AI["params"] = (json.loads(f.read_text()).get("models") or {})
            except (OSError, ValueError):
                pass
    return clamp(recipe, _AI["params"].get(recipe) or {})


def tuned(recipe: str) -> Callable:
    return lambda bars, state: apply(recipe, bars, state, ai_params(recipe))


# Fixed defaults (names kept from the first bench day), their inverted twins,
# and one AI-tuned copy per recipe ("@ai"): fixed vs AI-tuned is the test of
# whether the AI's settings add anything.
MODELS: Dict[str, Callable] = {
    "orb5": fixed("orb", minutes=5), "orb15": fixed("orb", minutes=15), "orb30": fixed("orb", minutes=30),
    "vwap_revert": fixed("vwap_revert"), "vwap_trend": fixed("vwap_trend"), "ema_9_21": fixed("ema_cross"),
    "rsi2": fixed("rsi2"), "bollinger_fade": fixed("bollinger_fade"), "squeeze_break": fixed("squeeze_break"),
    "volume_burst": fixed("volume_burst"), "opening_drive_fade": fixed("opening_drive_fade"),
    "liq_sweep": fixed("liq_sweep"), "liq_run": fixed("liq_run"),
}
MODELS.update({
    "orb5_inv": fixed("orb", minutes=5, invert=1), "orb15_inv": fixed("orb", minutes=15, invert=1),
    "orb30_inv": fixed("orb", minutes=30, invert=1), "ema_9_21_inv": fixed("ema_cross", invert=1),
    **{f"{r}_inv": fixed(r, invert=1) for r in ("vwap_revert", "vwap_trend", "rsi2", "bollinger_fade",
                                                 "squeeze_break", "volume_burst", "opening_drive_fade")},
})
MODELS.update({f"{r}@ai": tuned(r) for r in RECIPES})


def books() -> List[str]:
    """Every paper book: the models, then the AI agent."""
    return list(MODELS) + ([AGENT] if agent_on() else [])


def inverted(fn: Callable) -> Callable:
    """Any signal function taken the other way (kept for research scripts)."""
    def g(bars, state) -> Signal:
        sig = fn(bars, state)
        if not sig:
            return None
        side, stop, target = sig
        return ("S" if side == "B" else "B", target, stop)
    return g


# ------------------------------------------------------------------ the AI agent
# Owner, 26 Nov: "Keep 1 separate 1 lakh deployed strategy particularly for ai based
# agentic control over multi strategies". The AI agent (agent.py, every 15 minutes)
# picks which bench models to follow and how much to risk on each; this book copies
# those models' new signals into the agent's own Rs 1 lakh wallet and closes what the
# agent says to close. Its decisions are timestamped, so a restart replays them.

AGENT = "ai_agent"
AGENT_RISK_MULT = (0.25, 2.0)


def agent_on() -> bool:
    return common.setting("TRADER_AGENT", "true").strip().lower() in ("1", "true", "yes", "on")


def agent_risk() -> D:
    try:
        return D(common.setting("TRADER_AGENT_RISK", common.setting("TRADER_PAPER_RISK", "500")))
    except ArithmeticError:
        return D("500")


_AGENT = {"mtime": None, "day": None, "rows": []}


def agent_decisions(day: str) -> List[dict]:
    """Every decision the AI agent made today, oldest first (agent-DAY.jsonl)."""
    f = PAPER_DIR / f"agent-{day}.jsonl"
    try:
        mt = f.stat().st_mtime
    except OSError:
        mt = None
    if mt != _AGENT["mtime"] or day != _AGENT["day"]:
        rows = []
        if mt is not None:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("t"), (int, float)):
                    rows.append(r)
        rows.sort(key=lambda r: r["t"])
        _AGENT.update(mtime=mt, day=day, rows=rows)
    return _AGENT["rows"]


def agent_decision(day: str, at: float) -> Optional[dict]:
    """The agent's decision in force at time `at` (None before its first one)."""
    cur = None
    for r in agent_decisions(day):
        if r["t"] <= at:
            cur = r
        else:
            break
    return cur


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
    src: str = ""                         # the AI agent's copies: which model's signal it followed


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
    last: Dict[str, float] = field(default_factory=dict)
    would_kill: Dict[str, dict] = field(default_factory=dict)
    kill_checked: float = 0.0
    target_hit: Dict[str, float] = field(default_factory=dict)
    fno: Optional[object] = None          # fno.FnoBook: the F&O sleeves, driven by live trades only
    live: bool = False                    # False while a day is replayed from the tape
    agent_seen: float = 0.0               # the agent decision whose exits were applied

    def risk_rs(self) -> D:
        try:
            return D(common.setting("TRADER_PAPER_RISK", "40"))
        except ArithmeticError:
            return D("40")

    def observe_only(self) -> bool:
        """TRADER_PAPER_UNCAPPED (default true): no kill and no trade cap on paper."""
        return common.setting("TRADER_PAPER_UNCAPPED", "true").lower() in ("1", "true", "yes")

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
        self._journal(t.model, at, "close", t.symbol, t.side, t.qty, round(x, 2), t.stop, t.target, net,
                      f"{why}; entry {t.entry}; day so far {self.realised[t.model]:.0f}")
        if self.fno is not None and self.live:
            try:
                self.fno.on_close(t.model, t.symbol, why, at)
            except Exception as exc:                               # noqa: BLE001
                common.log("paper", {"event": "fno_error", "where": "close", "error": str(exc)[:200]})

    def _journal(self, model, at, event, sym, side, qty, price, stop, target, net=None, note="") -> None:
        if not self.write:
            return
        import journal
        journal.row(model, self.day, time=datetime.fromtimestamp(at, sess.IST).strftime("%H:%M:%S"), event=event,
                    stock=sym, side={"B": "BUY", "S": "SELL"}.get(side, side), qty=qty, price=price, stop=stop,
                    target=target, net=net, note=note)

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
        tgt = day_target()
        self.agent_exits(prices, at)
        for m in books():
            if m in self.killed:
                continue
            day = self.realised.get(m, D(0)) + self.model_open_net(m, prices)
            if tgt > 0 and day >= tgt and m not in self.target_hit:
                stops = target_stops()
                if stops:
                    for key in [k for k in self.open if k[0] == m]:
                        self.close(key, prices.get(key[1], self.open[key].entry), "day target", at)
                    self.killed.add(m)
                self.target_hit[m] = round(at, 1)
                shown = self.realised.get(m, D(0)) if stops else day
                self._ledger({"ev": "target", "t": round(at, 1), "model": m, "day": str(shown)})
                if self.write:
                    import journal
                    journal.note(m, self.day, f"DAY TARGET reached at "
                                 f"{datetime.fromtimestamp(at, sess.IST):%H:%M}: net {shown:.0f}. "
                                 + ("Done for the day." if stops else "Keeps trading for more."))
                if stops:
                    continue
            if self.observe_only():
                # Paper runs uncapped; remember where the kill WOULD have fired, to learn from.
                if m not in self.would_kill and day <= -D(common.setting("TRADER_PAPER_KILL_STUDY", "75")):
                    self.would_kill[m] = {"t": round(at, 1), "net": str(day.quantize(D("0.01")))}
                    self._ledger({"ev": "would_kill", "t": round(at, 1), "model": m, "day": str(day)})
                continue
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
            if not self.observe_only() and self.trades.get(m, 0) + sum(1 for k in self.open if k[0] == m) >= MAX_TRADES:
                continue
            slip = tick_of(px)
            entry = px + slip if side == "B" else px - slip
            per_share = abs(entry - stop)
            if per_share <= 0:
                continue
            room = float(capital() * LEVERAGE) - sum(t.entry * t.qty for k, t in self.open.items() if k[0] == m)
            qty = int(min(room // entry, float(self.risk_rs()) / per_share))
            if qty < 1:
                continue
            # the target must pay the round trip's charges at least twice over
            gross_t = abs(target - entry) * qty
            cost = float(charges_mod.round_trip(D(str(round(entry * qty, 2))), D(str(round(target * qty, 2)))).total)
            if gross_t < 2 * cost:
                continue
            self.open[(m, sym)] = Trade(m, sym, side, qty, round(entry, 2), round(stop, 2), round(target, 2), at)
            self._ledger({"ev": "open", "t": round(at, 1), "model": m, "symbol": sym, "side": side, "qty": qty,
                          "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2)})
            self._journal(m, at, "open", sym, side, qty, round(entry, 2), round(stop, 2), round(target, 2))
            if self.fno is not None and self.live:
                try:
                    self.fno.on_open(m, sym, side, px, at)
                except Exception as exc:                           # noqa: BLE001
                    common.log("paper", {"event": "fno_error", "where": "open", "error": str(exc)[:200]})
            self.agent_follow(m, sym, side, px, stop, target, at)

    def agent_follow(self, m: str, sym: str, side: str, px: float, stop: float, target: float, at: float) -> None:
        """Model m just opened: if the AI agent follows m now, take the same trade in its own wallet."""
        if not agent_on() or AGENT in self.killed or (AGENT, sym) in self.open or _hm(at) >= ENTRY_UNTIL:
            return
        dec = agent_decision(self.day, at)
        if not dec or dec.get("stand_aside"):
            return
        f = (dec.get("follow") or {}).get(m)
        if not isinstance(f, dict):
            return
        want = str(f.get("side", "both"))
        if (want == "long" and side != "B") or (want == "short" and side != "S"):
            return
        try:
            mult = min(max(float(f.get("risk", 1.0)), AGENT_RISK_MULT[0]), AGENT_RISK_MULT[1])
        except (TypeError, ValueError):
            mult = 1.0
        slip = tick_of(px)
        entry = px + slip if side == "B" else px - slip
        per_share = abs(entry - stop)
        if per_share <= 0:
            return
        room = float(capital() * LEVERAGE) - sum(t.entry * t.qty for k, t in self.open.items() if k[0] == AGENT)
        qty = int(min(room // entry, float(agent_risk()) * mult / per_share))
        if qty < 1:
            return
        self.open[(AGENT, sym)] = Trade(AGENT, sym, side, qty, round(entry, 2), round(stop, 2), round(target, 2), at, src=m)
        self._ledger({"ev": "open", "t": round(at, 1), "model": AGENT, "symbol": sym, "side": side, "qty": qty,
                      "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "src": m,
                      "risk_x": mult})
        self._journal(AGENT, at, "open", sym, side, qty, round(entry, 2), round(stop, 2), round(target, 2),
                      note=f"followed {m} (risk x{mult:g}): {str(f.get('why', ''))[:120]}")

    def agent_exits(self, prices: Dict[str, float], at: float) -> None:
        """A new agent decision may close positions: all of them, or those that followed given models."""
        if not agent_on():
            return
        dec = agent_decision(self.day, at)
        if not dec or dec["t"] == self.agent_seen:
            return
        self.agent_seen = dec["t"]
        if self.write:
            import journal
            fl = ", ".join(f"{m} x{f.get('risk', 1):g}{'' if f.get('side', 'both') == 'both' else ' ' + str(f.get('side'))}"
                           for m, f in (dec.get("follow") or {}).items())
            journal.note(AGENT, self.day, f"DECISION {datetime.fromtimestamp(dec['t'], sess.IST):%H:%M}: follow "
                         f"[{fl or 'none'}]" + (f"; exit {', '.join(dec['exit'])}" if dec.get("exit") else "")
                         + ("; EXIT ALL" if dec.get("exit_all") else "")
                         + ("; stand aside" if dec.get("stand_aside") else "")
                         + (f". {dec['note']}" if dec.get("note") else ""))
        exit_all = bool(dec.get("exit_all"))
        exit_models = set(dec.get("exit") or [])
        for key in [k for k in self.open if k[0] == AGENT]:
            t = self.open[key]
            if exit_all or t.src in exit_models:
                self.close(key, prices.get(t.symbol, t.entry), "AI agent exit", at)

    def feed(self, sym: str, at: float, px: float, cum: Optional[float]) -> None:
        s = self.series.setdefault(sym, Series())
        self.last[sym] = px
        self.on_price(sym, px, at)
        if at - self.kill_checked >= 1.0:                  # once a second is plenty
            self.kill_checked = at
            self.check_kills(self.last, at)
        if s.add(at, px, cum):
            self.on_bar(sym, at)

    def summary(self, prices: Dict[str, float]) -> dict:
        rows = {}
        for m in books():
            op = self.model_open_net(m, prices)
            rows[m] = {"net": str((self.realised.get(m, D(0)) + op).quantize(D("0.01"))),
                       "realised": str(self.realised.get(m, D(0))), "open": str(op.quantize(D("0.01"))),
                       "trades": self.trades.get(m, 0), "wins": self.wins.get(m, 0),
                       "open_trades": sum(1 for k in self.open if k[0] == m), "killed": m in self.killed,
                       "would_kill": self.would_kill.get(m), "target_hit": self.target_hit.get(m)}
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
        import journal
        journal.reset_bench_day(day)                     # ... and the strategies' trade logs
        last = replay_day(bench, day)
        bench._ledger({"ev": "start", "t": time.time(), "replayed_symbols": len(last)})
        try:
            import fno
            if fno.enabled():
                bench.fno = fno.FnoBook(day, fno.live_quote(), fno.Contracts(fno.load_master(day), now.date()))
        except Exception as exc:                                   # noqa: BLE001
            common.log("paper", {"event": "fno_error", "where": "start", "error": str(exc)[:200]})
        bench.live = True
        marks: Dict[str, D] = {}
        last_mark = 0.0
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
            if bench.fno is not None and time.time() - last_mark >= 30:
                last_mark = time.time()
                try:
                    marks = bench.fno.mark(time.time(), flat=(sess.now().hour, sess.now().minute) >= FLAT_AT)
                except Exception as exc:                           # noqa: BLE001
                    common.log("paper", {"event": "fno_error", "where": "mark", "error": str(exc)[:200]})
            try:
                p = PAPER_DIR / f"{day}-summary.json"
                tmp = p.with_suffix(".tmp")
                summ = bench.summary(last)
                if bench.fno is not None:
                    for k, v in bench.fno.summary(marks).items():
                        summ["models"][k] = {**v, "killed": bool(v.get("target_hit")) and target_stops(), "would_kill": None}
                tmp.write_text(json.dumps(summ))
                tmp.replace(p)
            except OSError:
                pass
            time.sleep(1)


if __name__ == "__main__":
    main()
