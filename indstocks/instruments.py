"""
Instruments master: ticker -> security_id, tick size, lot and freeze quantity.

Orders take a numeric security_id, not a symbol. The skill pack speaks in
tickers, so everything downstream needs this resolver.

Why it matters beyond the id: smartorder.py has been defaulting tick_size to
0.05. That is right for most NSE equities and wrong for some, and an off-tick
leg price is rejected outright. The real tick comes from here.

The file is a CSV, refreshed daily by the exchange, so the cache is keyed by
date. `equity` carries 26 columns; `fno` and `mcx` 23; `index` is a different
three-column file whose second column is labelled SEGMENT but holds the index
name, so it must be read positionally.

Standard library only.
"""

from __future__ import annotations

import csv
import io
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, Optional

CACHE_DIR = Path(os.environ.get("INDSTOCKS_CACHE_DIR", "/var/lib/indstocks"))


def _dec(value: str, default: Optional[Decimal] = None) -> Optional[Decimal]:
    try:
        return Decimal((value or "").strip())
    except (InvalidOperation, AttributeError):
        return default


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float((value or "").strip()))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class Instrument:
    exchange: str
    segment: str
    security_id: str
    trading_symbol: str
    symbol_name: str
    series: str
    tick_size: Decimal
    lot_units: int
    freeze_qty: int
    isin: str = ""
    upper_limit: Optional[Decimal] = None
    lower_limit: Optional[Decimal] = None

    @property
    def is_equity_eq(self) -> bool:
        """A normal cash-market equity row, not BE/BZ/illiquid series."""
        return self.series.upper() == "EQ"


def parse(csv_text: str) -> list[Instrument]:
    rows = csv.DictReader(io.StringIO(csv_text))
    out: list[Instrument] = []
    for r in rows:
        sid = (r.get("SECURITY_ID") or "").strip()
        if not sid:
            continue
        tick = _dec(r.get("TICK_SIZE", ""), Decimal("0.05")) or Decimal("0.05")
        if tick <= 0:
            tick = Decimal("0.05")
        out.append(Instrument(
            exchange=(r.get("EXCH") or "").strip().upper(),
            segment=(r.get("SEGMENT") or "").strip().upper(),
            security_id=sid,
            trading_symbol=(r.get("TRADING_SYMBOL") or "").strip().upper(),
            symbol_name=(r.get("SYMBOL_NAME") or "").strip().upper(),
            series=(r.get("SERIES") or "").strip().upper(),
            tick_size=tick,
            lot_units=_int(r.get("LOT_UNITS", ""), 1) or 1,
            freeze_qty=_int(r.get("FREEZE_QTY", ""), 0),
            isin=(r.get("ISIN") or "").strip(),
            upper_limit=_dec(r.get("UPPER_LIMIT", "")),
            lower_limit=_dec(r.get("LOWER_LIMIT", "")),
        ))
    return out


class Instruments:
    """Loads, caches and indexes the instrument master for one source."""

    def __init__(self, client=None, source: str = "equity", cache_dir: Path = None,
                 today=None):
        self.client = client
        self.source = source
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._today = today or (lambda: time.strftime("%Y-%m-%d"))
        self._rows: list[Instrument] = []
        self._by_symbol: Dict[str, Instrument] = {}
        self._by_id: Dict[str, Instrument] = {}

    # ------------------------------------------------------------ loading

    def _cache_path(self) -> Path:
        return self.cache_dir / f"instruments-{self.source}-{self._today()}.csv"

    def load(self, *, refresh: bool = False) -> "Instruments":
        path = self._cache_path()
        if not refresh and path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            if self.client is None:
                raise RuntimeError("no cached master for today and no client to fetch it")
            blob = self.client.get("/market/instruments",
                                   params={"source": self.source}, raw=True)
            text = blob.decode("utf-8", errors="replace")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            # Yesterday's masters are stale the moment the exchange republishes.
            for old in self.cache_dir.glob(f"instruments-{self.source}-*.csv"):
                if old != path:
                    try:
                        old.unlink()
                    except OSError:
                        pass
        self.index(parse(text))
        return self

    def index(self, rows: Iterable[Instrument]) -> "Instruments":
        self._rows = list(rows)
        self._by_id = {r.security_id: r for r in self._rows}
        by_symbol: Dict[str, Instrument] = {}
        for r in self._rows:
            for key in (r.trading_symbol, r.symbol_name):
                if not key:
                    continue
                prior = by_symbol.get(key)
                # Prefer NSE, and prefer the plain EQ series over BE/BZ.
                if prior is None or self._better(r, prior):
                    by_symbol[key] = r
        self._by_symbol = by_symbol
        return self

    @staticmethod
    def _better(new: Instrument, old: Instrument) -> bool:
        def score(i: Instrument) -> tuple:
            return (i.exchange == "NSE", i.is_equity_eq)
        return score(new) > score(old)

    # ------------------------------------------------------------ lookup

    def resolve(self, symbol: str) -> Instrument:
        key = (symbol or "").strip().upper()
        # Tolerate the Yahoo-style suffixes the upstream skills use.
        for suffix in (".NS", ".BO", "-EQ"):
            if key.endswith(suffix):
                key = key[: -len(suffix)]
        try:
            return self._by_symbol[key]
        except KeyError:
            raise KeyError(
                f"{symbol!r} is not in the {self.source} master "
                f"({len(self._rows)} instruments loaded)"
            ) from None

    def by_id(self, security_id: str) -> Instrument:
        return self._by_id[str(security_id)]

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, symbol: str) -> bool:
        try:
            self.resolve(symbol)
            return True
        except KeyError:
            return False
