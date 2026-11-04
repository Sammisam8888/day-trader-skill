"""
Rank a universe by how far it actually moves intraday.

Setting a 5% target on a stock whose typical day is 1.2% does not produce 5%
trades; it produces stops. Aggression has to come from the universe, not from
the target multiple, so this measures the move that is actually there.

Three figures per stock, from daily candles:

  up_pct       (high - open) / open, the CEILING on an intraday long that
               day. A real entry is above the open and a real exit below the
               high, so anything here overstates what was capturable.
  range_pct    (high - low) / open, the day's whole swing.
  hit_rate(x)  share of days whose up_pct cleared x.

The F&O underlying list is the default universe: roughly 180-200 names that
the exchange has already screened for liquidity, which is most of what makes
a stop fill near its trigger.
"""

from __future__ import annotations

import os
import statistics
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bars import Candle  # noqa: E402

DEFAULT_THRESHOLDS = (Decimal("1"), Decimal("2"), Decimal("3"),
                      Decimal("5"), Decimal("10"))


@dataclass(frozen=True)
class Profile:
    symbol: str
    days: int
    median_up: Decimal
    median_range: Decimal
    p90_up: Decimal
    hit_rates: Dict[Decimal, Decimal]
    median_value: Decimal          # proxy for liquidity: close * volume

    def hits(self, threshold: Decimal) -> Decimal:
        return self.hit_rates.get(threshold, Decimal(0))


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return (part / whole * 100) if whole > 0 else Decimal(0)


def _median(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    return Decimal(str(statistics.median([float(v) for v in values]))).quantize(
        Decimal("0.01"))


def _percentile(values: Sequence[Decimal], p: float) -> Decimal:
    if not values:
        return Decimal(0)
    ordered = sorted(float(v) for v in values)
    idx = min(int(p * (len(ordered) - 1)), len(ordered) - 1)
    return Decimal(str(ordered[idx])).quantize(Decimal("0.01"))


def profile(symbol: str, candles: Iterable[Candle], *,
            thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS) -> Profile:
    ups: List[Decimal] = []
    ranges: List[Decimal] = []
    values: List[Decimal] = []

    for k in candles:
        if k.open <= 0:
            continue
        ups.append(_pct(k.high - k.open, k.open))
        ranges.append(_pct(k.high - k.low, k.open))
        values.append(k.close * Decimal(k.volume))

    n = len(ups)
    rates = {t: (_pct(Decimal(sum(1 for u in ups if u >= t)), Decimal(n))
                 if n else Decimal(0)).quantize(Decimal("0.1"))
             for t in thresholds}

    return Profile(symbol=symbol, days=n, median_up=_median(ups),
                   median_range=_median(ranges), p90_up=_percentile(ups, 0.90),
                   hit_rates=rates, median_value=_median(values))


def rank(profiles: Iterable[Profile], *, threshold: Decimal = Decimal("3"),
         min_days: int = 100,
         min_median_value: Decimal = Decimal("50000000")) -> List[Profile]:
    """Most-volatile first, among names with enough history and turnover.

    The turnover floor is not decoration. A stock can swing 8% a day and
    still be untradeable: a stop-limit in a thin book does not fill at its
    trigger, and the loss you planned is not the loss you take.
    """
    eligible = [p for p in profiles
                if p.days >= min_days and p.median_value >= min_median_value]
    return sorted(eligible, key=lambda p: (p.hits(threshold), p.median_up),
                  reverse=True)


def fno_underlyings(master) -> List[str]:
    """Symbols the exchange lists derivatives on: the liquid universe."""
    names = set()
    for row in getattr(master, "_rows", []):
        name = (row.symbol_name or "").strip().upper()
        if name:
            names.add(name)
    return sorted(names)
