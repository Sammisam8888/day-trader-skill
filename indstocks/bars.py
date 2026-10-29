"""
Candle fetching for NSE instruments.

Wraps GET /market/historical/{interval}. The contract has several edges that
are easy to get wrong and expensive to get wrong late:

  - `scrip-codes` takes at most FIVE codes per call; more is a 400.
  - The segment travels in the code prefix: NSE_2885, not a segment param.
  - Request windows are epoch MILLISECONDS; candle `ts` is epoch SECONDS.
  - `ts` is the candle's OPEN time; it covers [ts, ts + interval).
  - The response uses `success`, not `status`, unlike every other endpoint.
  - Codes with no data are absent from `data` rather than present and empty.

Standard library only.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session import IST, epoch_ms, from_candle_ts  # noqa: E402

MAX_CODES_PER_CALL = 5

INTERVAL_MINUTES = {
    "1minute": 1, "2minute": 2, "3minute": 3, "4minute": 4, "5minute": 5,
    "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60,
    "120minute": 120, "180minute": 180, "240minute": 240,
}


@dataclass(frozen=True)
class Candle:
    opens_at: datetime          # `ts`, the candle's OPEN time, in IST
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


def scrip_code(instrument, segment_prefix: str = "NSE") -> str:
    exch = getattr(instrument, "exchange", None) or segment_prefix
    return f"{exch}_{instrument.security_id}"


def _to_candle(raw: dict, interval_minutes: int) -> Candle:
    return Candle(
        opens_at=from_candle_ts(int(raw["ts"])),
        open=Decimal(str(raw["o"])),
        high=Decimal(str(raw["h"])),
        low=Decimal(str(raw["l"])),
        close=Decimal(str(raw["c"])),
        volume=int(raw.get("v") or 0),
        interval_minutes=interval_minutes,
    )


def parse_response(payload: dict, interval: str) -> Dict[str, List[Candle]]:
    if not payload.get("success", False):
        raise RuntimeError(f"historical data call was not successful: {str(payload)[:200]}")
    minutes = INTERVAL_MINUTES[interval]
    out: Dict[str, List[Candle]] = {}
    for code, block in (payload.get("data") or {}).items():
        candles = [_to_candle(c, minutes) for c in (block.get("candles") or [])]
        candles.sort(key=lambda c: c.opens_at)
        out[code] = candles
    return out


class Bars:
    def __init__(self, client, interval: str = "5minute"):
        if interval not in INTERVAL_MINUTES:
            raise ValueError(f"unsupported interval {interval!r}")
        self.client = client
        self.interval = interval

    @property
    def interval_minutes(self) -> int:
        return INTERVAL_MINUTES[self.interval]

    def fetch(self, codes: Iterable[str], start: datetime,
              end: datetime) -> Dict[str, List[Candle]]:
        """Candles for the half-open window [start, end), batching by five."""
        codes = list(dict.fromkeys(codes))       # de-dupe, keep order
        result: Dict[str, List[Candle]] = {}
        for i in range(0, len(codes), MAX_CODES_PER_CALL):
            batch = codes[i:i + MAX_CODES_PER_CALL]
            payload = self.client.get(
                f"/market/historical/{self.interval}",
                params={
                    "scrip-codes": ",".join(batch),
                    "start_time": epoch_ms(start),
                    "end_time": epoch_ms(end),
                },
            )
            result.update(parse_response(payload, self.interval))
        # A code with no candles is absent from `data`; make that explicit so
        # callers can tell "no data" from "never asked".
        for code in codes:
            result.setdefault(code, [])
        return result

    def fetch_one(self, code: str, start: datetime, end: datetime) -> List[Candle]:
        return self.fetch([code], start, end)[code]


def opening_range(candles: Iterable[Candle], session) -> Optional[tuple]:
    """(high, low) of the candles inside the opening range window.

    Only candles that both open at or after the session open and close at or
    before the opening-range end count: a 5-minute candle opening at 09:25
    closes at 09:30 and is the last one in a 09:15-09:30 range.
    """
    lo: Optional[Decimal] = None
    hi: Optional[Decimal] = None
    for c in candles:
        if c.opens_at >= session.open and c.closes_at <= session.opening_range_end:
            hi = c.high if hi is None else max(hi, c.high)
            lo = c.low if lo is None else min(lo, c.low)
    if hi is None or lo is None:
        return None
    return hi, lo
