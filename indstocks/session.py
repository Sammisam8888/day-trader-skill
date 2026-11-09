"""
NSE session clock: IST, trading days, the opening range and the flat-by time.

The upstream pack is built on the NY session. Everything here replaces that.
Pure arithmetic on an injected clock, so it is fully testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# Opening range: the first 15 minutes, 09:15 to 09:30.
OPENING_RANGE_MINUTES = 15

# No new entries after this. A breakout at 14:00 has no room to work before
# the flat-by time, and INTRADAY product is auto-squared-off by the broker
# anyway -- better to close deliberately than be closed.
# 2026-11-05: all three fills came after 13:35 on a rally that faded within
# the hour. A breakout at 14:00 has 75 minutes to work before the flatten;
# one at 10:30 has five hours. Afternoon breakouts are the ones that get
# sold into the close at whatever price is there.
LAST_ENTRY = time(12, 30)
# Our own square-off, ahead of the broker's (Shoonya MIS ~15:15-15:20). The
# fill monitor runs the flatten at this moment; the flatten timers are a
# backup for when the monitor is down.
FLAT_BY = time(15, 5)

# Special sessions: (open, last_entry, flat_by, close). A date here trades
# even on a weekend or a listed holiday. Muhurat 2026: Sunday 8 Nov,
# 14:20-15:30 (NSE circular; pre-open 14:05-14:20).
SPECIAL_SESSIONS = {
    "2026-11-08": (time(14, 20), time(14, 55), time(15, 10), time(15, 30)),
}

# NSE trading holidays. 2026 confirmed; extend each year. A date missing here
# is treated as a trading day, so an unlisted holiday means wasted API calls
# and no candles, not a bad order.
# NSE equity holidays 2026 (weekdays). Corrected 2026-11-09 against the
# published list: Balipratipada is 10 Nov, Dussehra 20 Oct, Gurpurb 24 Nov.
HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}


def now(clock=None) -> datetime:
    return (clock() if clock else datetime.now(IST)).astimezone(IST)


def is_trading_day(d: date, holidays: Optional[Iterable[str]] = None) -> bool:
    if d.isoformat() in SPECIAL_SESSIONS:
        return True
    if d.weekday() >= 5:            # Saturday, Sunday
        return False
    hol = set(holidays) if holidays is not None else HOLIDAYS_2026
    return d.isoformat() not in hol


def at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


@dataclass(frozen=True)
class Session:
    """The named moments of one trading day."""
    day: date

    @property
    def special(self) -> Optional[tuple]:
        return SPECIAL_SESSIONS.get(self.day.isoformat())

    @property
    def open(self) -> datetime:
        return at(self.day, self.special[0] if self.special else MARKET_OPEN)

    @property
    def opening_range_end(self) -> datetime:
        return self.open + timedelta(minutes=OPENING_RANGE_MINUTES)

    @property
    def last_entry(self) -> datetime:
        return at(self.day, self.special[1] if self.special else LAST_ENTRY)

    @property
    def flat_by(self) -> datetime:
        return at(self.day, self.special[2] if self.special else FLAT_BY)

    @property
    def close(self) -> datetime:
        return at(self.day, self.special[3] if self.special else MARKET_CLOSE)

    def phase(self, t: datetime) -> str:
        """Where in the day we are: one of
        pre_open / opening_range / entry_window / no_new_entries / flatten / closed
        """
        t = t.astimezone(IST)
        if t < self.open:
            return "pre_open"
        if t < self.opening_range_end:
            return "opening_range"
        if t < self.last_entry:
            return "entry_window"
        if t < self.flat_by:
            return "no_new_entries"
        if t < self.close:
            return "flatten"
        return "closed"

    def accepts_new_entries(self, t: datetime) -> bool:
        return self.phase(t) == "entry_window"


def session_for(t: datetime, holidays: Optional[Iterable[str]] = None) -> Optional[Session]:
    """The Session for the given moment, or None if that day does not trade."""
    t = t.astimezone(IST)
    return Session(t.date()) if is_trading_day(t.date(), holidays) else None


def previous_trading_days(d: date, count: int,
                          holidays: Optional[Iterable[str]] = None) -> list[date]:
    """The `count` trading days strictly before `d`, most recent first."""
    out: list[date] = []
    cur = d
    # Guard rather than loop forever if a holiday list is pathological.
    for _ in range(count * 4 + 20):
        cur -= timedelta(days=1)
        if is_trading_day(cur, holidays):
            out.append(cur)
            if len(out) == count:
                break
    return out


def epoch_ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def from_candle_ts(ts: int) -> datetime:
    """Candle `ts` is epoch SECONDS; request windows are MILLISECONDS."""
    return datetime.fromtimestamp(ts, IST)
