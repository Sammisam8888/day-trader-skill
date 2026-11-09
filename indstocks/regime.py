"""
Which way is the market going? Measured, not guessed.

Every scan, from the same 5-minute candles Claude sees, each symbol gets a
trend score from -1 (falling) to +1 (rising). Five facts, one vote each:

  vwap     last close above / below today's VWAP
  open     last close above / below today's open
  prev     last close above / below yesterday's close
  ema      9-period EMA of 5-minute closes above / below the 21-period EMA
  swing    the last three 15-minute bars make higher highs and higher lows
           (+1), lower highs and lower lows (-1), or neither (0)

The market's direction comes from the index proxy (NIFTYBEES by default,
double weight) plus breadth: how many of the stocks score up vs down.

  UP     longs only. A short against a rising market is refused.
  DOWN   shorts only. A long against a falling market is refused.
  MIXED  either side, but only with the stock's own trend: long the strong,
         short the weak.
  FORMING  under 3 bars today (before ~09:30). Nothing is gated yet; the
         opening-range entries rest as triggers and are re-checked each scan.

In every state a trade against the stock's own clear trend (score at or
past -/+ STOCK_AGAINST) is refused: no buying a stock that is falling hard
because the index is green.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from market import Candle

UP, DOWN, MIXED, FORMING = "UP", "DOWN", "MIXED", "FORMING"
MARKET_TREND = 0.4          # index-weighted score at or past this is a trend day
BREADTH = 0.6               # and at least this share of stocks agree
STOCK_AGAINST = 0.4         # a stock scoring this hard the other way is not traded against
MIN_BARS = 3


def _ema(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _swing(bars15: List[Candle]) -> int:
    b = bars15[-3:]
    if len(b) < 3:
        return 0
    hh = b[0].high < b[1].high < b[2].high
    hl = b[0].low < b[1].low < b[2].low
    lh = b[0].high > b[1].high > b[2].high
    ll = b[0].low > b[1].low > b[2].low
    if hh and hl:
        return 1
    if lh and ll:
        return -1
    if hh or hl:
        return 1 if not (lh or ll) else 0
    if lh or ll:
        return -1
    return 0


def _sign(a: float, b: float, eps: float) -> int:
    if a > b + eps:
        return 1
    if a < b - eps:
        return -1
    return 0


def stock_trend(candles: List[Candle], today, bars15: List[Candle]) -> Dict:
    """Score one symbol. `candles` are 5-minute, several days; `bars15` today's 15-minute bars."""
    tc = [c for c in candles if c.opens_at.date() == today]
    prev = [c for c in candles if c.opens_at.date() < today]
    if len(tc) < MIN_BARS:
        return {"score": 0.0, "state": FORMING, "bars": len(tc)}
    last = float(tc[-1].close)
    eps = last * 0.0003                                   # ~0.03%: inside this is "at", not above/below
    vol = sum(c.volume for c in tc)
    vwap = (sum(float(c.high + c.low + c.close) / 3 * c.volume for c in tc) / vol) if vol else None
    closes = [float(c.close) for c in candles if c.opens_at.date() <= today]
    e9, e21 = _ema(closes, 9), _ema(closes, 21)
    votes = {
        "vwap": _sign(last, vwap, eps) if vwap else 0,
        "open": _sign(last, float(tc[0].open), eps),
        "prev": _sign(last, float(prev[-1].close), eps) if prev else 0,
        "ema": _sign(e9, e21, eps / 3) if e9 is not None and e21 is not None else 0,
        "swing": _swing(bars15),
    }
    score = round(sum(votes.values()) / len(votes), 2)
    chg = round((last / float(prev[-1].close) - 1) * 100, 2) if prev else None
    return {"score": score, "state": UP if score >= STOCK_AGAINST else DOWN if score <= -STOCK_AGAINST else MIXED,
            "votes": votes, "vwap": round(vwap, 2) if vwap else None, "change_pct": chg, "bars": len(tc)}


def market(trends: Dict[str, Dict], index: str = "NIFTYBEES") -> Dict:
    """Combine per-symbol trends into the market's direction and the sides allowed."""
    live = {s: t for s, t in trends.items() if t.get("state") != FORMING}
    if not live:
        return {"state": FORMING, "score": 0.0, "breadth_up": None, "index": index,
                "allowed": ["BUY", "SELL"], "why": "under 3 bars today: direction not measured yet"}
    n = len(live)
    up = sum(1 for t in live.values() if t["score"] > 0) / n
    down = sum(1 for t in live.values() if t["score"] < 0) / n
    idx = live.get(index)
    avg = sum(t["score"] for t in live.values()) / n
    score = round((2 * idx["score"] + avg) / 3, 2) if idx else round(avg, 2)
    if score >= MARKET_TREND and up >= BREADTH:
        state, allowed = UP, ["BUY"]
    elif score <= -MARKET_TREND and down >= BREADTH:
        state, allowed = DOWN, ["SELL"]
    else:
        state, allowed = MIXED, ["BUY", "SELL"]
    why = (f"{index} {idx['score']:+.2f}, " if idx else "") + \
          f"average {avg:+.2f}, {round(up * 100)}% of stocks up / {round(down * 100)}% down"
    return {"state": state, "score": score, "breadth_up": round(up, 2), "breadth_down": round(down, 2),
            "index": index, "allowed": allowed, "why": why}


def check(side: str, symbol: str, direction: Optional[Dict]) -> Optional[str]:
    """None if a `side` ("BUY"/"SELL") entry on `symbol` goes with the measured
    direction, else the reason it does not. No direction -> no gate."""
    if not direction:
        return None
    m = direction.get("market") or {}
    st = (direction.get("stocks") or {}).get(symbol) or {}
    if side not in m.get("allowed", ["BUY", "SELL"]):
        return (f"market is {m.get('state')} ({m.get('why')}): only "
                f"{'longs' if m.get('allowed') == ['BUY'] else 'shorts'} today")
    s = st.get("score")
    if s is not None and st.get("state") != FORMING:
        if side == "BUY" and s <= -STOCK_AGAINST:
            return f"{symbol} itself is falling (trend {s:+.2f}): no long against it"
        if side == "SELL" and s >= STOCK_AGAINST:
            return f"{symbol} itself is rising (trend {s:+.2f}): no short against it"
    return None
