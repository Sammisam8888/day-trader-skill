"""
The playbook: long and short setups, detected by code from 5-minute candles.

Each setup comes with an exact entry (a stop trigger: it fills only if
price proves the idea), a stop where the idea is wrong, and a target at
2R. Claude sees the live ones every scan and decides which to take; the
backtest replays the same detector bar by bar to measure each one.

  setup          long                                  short
  -------------  ------------------------------------  ------------------------------------
  orb            09:30-11:00: trigger above the        mirror: below the opening-range low
                 opening-range high, stop at the
                 range midpoint
  vwap_pullback  stock trending up: a bar dips to      stock trending down: a bar rallies to
                 VWAP and closes back above it green;  VWAP and closes back below it red;
                 trigger above that bar's high,        trigger below its low, stop above
                 stop under its low / VWAP             its high / VWAP
  failed_break   price broke the opening-range low     price broke the opening-range high
                 in the last 3 bars and closed back    in the last 3 bars and closed back
                 inside: trigger above the last bar,   inside: trigger below the last bar,
                 stop under the failed low             stop above the failed high
  day_break      after 10:30, trending up and          after 10:30, trending down and
                 within 0.25% of the day high:         within 0.25% of the day low:
                 trigger above the high, stop under    trigger below the low, stop above
                 the last 3 bars' low                  the last 3 bars' high

Risk per share must be 0.15%-1.2% of price: tighter is noise, wider is too
far for a day trade. Only closed bars are used (no look-ahead).
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Dict, List, Optional

from market import Candle, to_tick

R_MULT = Decimal(2)
MIN_RISK_PCT, MAX_RISK_PCT = Decimal("0.15"), Decimal("1.2")


def _vwap(tc: List[Candle]) -> Optional[Decimal]:
    vol = sum(c.volume for c in tc)
    if not vol:
        return None
    return sum((c.high + c.low + c.close) / 3 * c.volume for c in tc) / vol


def _atr(cs: List[Candle], n: int = 14) -> Decimal:
    cs = cs[-(n + 1):]
    if len(cs) < 2:
        return cs[0].high - cs[0].low if cs else Decimal(0)
    trs = [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close)) for a, b in zip(cs, cs[1:])]
    return sum(trs) / len(trs)


def _plan(name: str, side: str, entry: Decimal, stop: Decimal, tick: Decimal, why: str) -> Optional[dict]:
    entry = to_tick(entry, tick, mode="up" if side == "BUY" else "down")
    stop = to_tick(stop, tick, mode="down" if side == "BUY" else "up")
    risk = entry - stop if side == "BUY" else stop - entry
    if risk <= 0:
        return None
    pct = risk / entry * 100
    if not (MIN_RISK_PCT <= pct <= MAX_RISK_PCT):
        return None
    target = entry + risk * R_MULT if side == "BUY" else entry - risk * R_MULT
    target = to_tick(target, tick, mode="down" if side == "BUY" else "up")
    return {"setup": name, "side": side, "order": "stop", "entry": str(entry), "stop": str(stop),
            "target": str(target), "risk_pct": str(round(pct, 2)), "why": why}


def detect(candles: List[Candle], day, tick: Decimal, trend: float = 0.0, *,
           now: Optional[datetime] = None) -> List[dict]:
    """Live setups for one symbol from today's closed 5-minute bars."""
    tc = [c for c in candles if c.opens_at.date() == day]
    if now is not None:
        tc = [c for c in tc if c.closes_at <= now]
    if len(tc) < 3:
        return []
    orh, orl = max(c.high for c in tc[:3]), min(c.low for c in tc[:3])
    mid = (orh + orl) / 2
    last = tc[-1]
    t = (last.opens_at.hour, last.opens_at.minute)
    clock = time(*t)
    vwap = _vwap(tc)
    atr = _atr(tc)
    hi, lo = max(c.high for c in tc), min(c.low for c in tc)
    out: List[dict] = []

    def add(p):
        if p:
            out.append(p)

    # orb: before 11:00, price still inside or at the range
    if clock < time(11, 0) and len(tc) >= 3:
        if last.close <= orh and trend >= 0:
            add(_plan("orb", "BUY", orh + tick, mid, tick, f"opening range {orl}-{orh}: break above {orh}"))
        if last.close >= orl and trend <= 0:
            add(_plan("orb", "SELL", orl - tick, mid, tick, f"opening range {orl}-{orh}: break below {orl}"))

    if vwap is not None and len(tc) >= 6:
        band = vwap * Decimal("0.0015")
        if trend >= 0.4 and last.low <= vwap + band and last.close > vwap and last.close > last.open:
            add(_plan("vwap_pullback", "BUY", last.high + tick, min(last.low, vwap) - atr / 4, tick,
                      f"uptrend: pullback to VWAP {vwap:.2f} held, bar closed green at {last.close}"))
        if trend <= -0.4 and last.high >= vwap - band and last.close < vwap and last.close < last.open:
            add(_plan("vwap_pullback", "SELL", last.low - tick, max(last.high, vwap) + atr / 4, tick,
                      f"downtrend: rally to VWAP {vwap:.2f} rejected, bar closed red at {last.close}"))

    if len(tc) >= 5:
        recent = tc[-3:]
        broke_hi = max(c.high for c in recent)
        broke_lo = min(c.low for c in recent)
        if broke_hi > orh and last.close < orh:
            add(_plan("failed_break", "SELL", last.low - tick, broke_hi + tick, tick,
                      f"broke the range high {orh} to {broke_hi} and closed back inside at {last.close}"))
        if broke_lo < orl and last.close > orl:
            add(_plan("failed_break", "BUY", last.high + tick, broke_lo - tick, tick,
                      f"broke the range low {orl} to {broke_lo} and closed back inside at {last.close}"))

    if clock >= time(10, 30) and len(tc) >= 4:
        swing_lo, swing_hi = min(c.low for c in tc[-3:]), max(c.high for c in tc[-3:])
        if trend >= 0.4 and last.close >= hi * (1 - Decimal("0.0025")):
            add(_plan("day_break", "BUY", hi + tick, swing_lo - tick, tick,
                      f"uptrend pressing the day high {hi}: break above it"))
        if trend <= -0.4 and last.close <= lo * (1 + Decimal("0.0025")):
            add(_plan("day_break", "SELL", lo - tick, swing_hi + tick, tick,
                      f"downtrend pressing the day low {lo}: break below it"))
    return out


def rank(trends: Dict[str, dict], index: str = "NIFTYBEES") -> Dict[str, List[str]]:
    """Strongest and weakest names by trend score (index excluded)."""
    rows = sorted(((s, t.get("score", 0)) for s, t in trends.items()
                   if s != index and t.get("state") != "FORMING"), key=lambda x: x[1])
    return {"strongest": [s for s, v in reversed(rows[-3:]) if v > 0],
            "weakest": [s for s, v in rows[:3] if v < 0]}
