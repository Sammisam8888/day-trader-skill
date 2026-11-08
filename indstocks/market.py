"""
Broker-neutral market primitives: candles, the opening range, tick rounding.

These used to live in the INDstocks modules (bars.py, smartorder.py). They
are pure arithmetic and belong to no broker, so the strategy, the backtest
and the volatility profile import them from here.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session import IST  # noqa: E402


@dataclass(frozen=True)
class Candle:
    opens_at: datetime          # the candle's OPEN time, in IST
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    interval_minutes: int

    @property
    def closes_at(self) -> datetime:
        return self.opens_at + timedelta(minutes=self.interval_minutes)

    def is_complete_at(self, t: datetime) -> bool:
        """A forming candle must never trigger an entry: its close can still
        move. Only a candle whose window has fully elapsed counts."""
        return t.astimezone(IST) >= self.closes_at


def opening_range(candles: Iterable[Candle], session) -> Optional[tuple]:
    """(high, low) of the candles inside the opening range window."""
    lo: Optional[Decimal] = None
    hi: Optional[Decimal] = None
    for c in candles:
        if c.opens_at >= session.open and c.closes_at <= session.opening_range_end:
            hi = c.high if hi is None else max(hi, c.high)
            lo = c.low if lo is None else min(lo, c.low)
    if hi is None or lo is None:
        return None
    return hi, lo


class TickError(ValueError):
    pass


def to_tick(price: Decimal, tick: Decimal, *, mode: str = "nearest") -> Decimal:
    """Snap a price to a multiple of the tick size.

    mode="down" for a long stop (further from entry = safer), "up" for a
    long target, "nearest" otherwise.
    """
    if tick <= 0:
        raise TickError("tick size must be > 0")
    units = Decimal(price) / tick
    if mode == "down":
        units = units.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        units = units.to_integral_value(rounding=ROUND_UP)
    else:
        units = Decimal(int(units + Decimal("0.5")))
    return (units * tick).quantize(tick)


def on_tick(price: Decimal, tick: Decimal) -> bool:
    return (price / tick) == (price / tick).to_integral_value()
