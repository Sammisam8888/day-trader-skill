"""
Opening-range breakout: entry detection and the exit plan.

Entry rule (the one the upstream pack's historical odds describe): the first
COMPLETED 5-minute candle that CLOSES above the opening-range high, inside
the entry window. Not a touch of the high -- a close above it. A forming
candle never triggers, because its close can still move back under the level.

Exit plan: stop at the opening-range midpoint by default, target at a
multiple of the risk. Prices are snapped to the instrument's real tick, with
the stop rounded DOWN and the target UP so rounding never tightens the stop
or shortens the target.

Long side only, matching upstream. Pure functions over candles; no network.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bars import Candle, opening_range  # noqa: E402
from session import IST, Session  # noqa: E402
from smartorder import to_tick  # noqa: E402

WAITING = "waiting"
TRIGGERED = "triggered"
NO_RANGE = "no_range"
OUTSIDE_WINDOW = "outside_window"


@dataclass(frozen=True)
class EntrySignal:
    state: str
    range_high: Optional[Decimal] = None
    range_low: Optional[Decimal] = None
    price: Optional[Decimal] = None
    at: Optional[datetime] = None
    distance: Optional[Decimal] = None   # how far under the level, when waiting
    reason: str = ""

    @property
    def triggered(self) -> bool:
        return self.state == TRIGGERED


def check_entry(candles: Iterable[Candle], session: Session, now: datetime,
                *, min_volume: int = 0) -> EntrySignal:
    candles = list(candles)
    rng = opening_range(candles, session)
    if rng is None:
        return EntrySignal(NO_RANGE, reason="no candles inside the opening range yet")
    high, low = rng

    now = now.astimezone(IST)
    phase = session.phase(now)
    if phase in ("pre_open", "opening_range"):
        return EntrySignal(WAITING, high, low,
                           reason=f"opening range still forming ({phase})")

    last_close: Optional[Decimal] = None
    for c in candles:
        # Strictly after the range, complete, and inside the entry window.
        if c.opens_at < session.opening_range_end:
            continue
        if not c.is_complete_at(now):
            continue
        last_close = c.close
        if not session.accepts_new_entries(c.closes_at):
            continue
        if c.close > high and c.volume >= min_volume:
            return EntrySignal(TRIGGERED, high, low, price=c.close,
                               at=c.closes_at,
                               reason=f"5m close {c.close} above range high {high}")

    if not session.accepts_new_entries(now):
        return EntrySignal(OUTSIDE_WINDOW, high, low,
                           reason=f"entry window closed ({phase})")

    dist = None if last_close is None else (high - last_close)
    return EntrySignal(WAITING, high, low, price=last_close, distance=dist,
                       reason="no completed candle has closed above the range high")


@dataclass(frozen=True)
class ExitPlan:
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_per_share: Decimal
    reward_to_risk: Decimal

    @property
    def is_sane(self) -> bool:
        return self.stop < self.entry < self.target


class PlanError(ValueError):
    pass


def plan_exit(entry: Decimal, range_high: Decimal, range_low: Decimal,
              tick: Decimal, *, stop_mode: str = "or-mid",
              reward_multiple: Decimal = Decimal(2),
              min_risk_ticks: int = 2) -> ExitPlan:
    """Stop and target for a long opening-range breakout.

    stop_mode: "or-mid" (default) puts the stop at the range midpoint,
    "or-low" at the range low.
    """
    if stop_mode == "or-mid":
        raw_stop = (range_high + range_low) / 2
    elif stop_mode == "or-low":
        raw_stop = range_low
    else:
        raise PlanError(f"unknown stop_mode {stop_mode!r}")

    # Round the stop DOWN and the target UP: rounding must never tighten the
    # stop or shorten the target, which would quietly change the plan's risk.
    stop = to_tick(raw_stop, tick, mode="down")
    if stop >= entry:
        raise PlanError(
            f"stop {stop} is not below entry {entry} -- the breakout candle "
            "closed below the range midpoint, so this setup has no room"
        )

    risk = entry - stop
    if risk < tick * min_risk_ticks:
        raise PlanError(
            f"risk of {risk} is under {min_risk_ticks} ticks ({tick * min_risk_ticks}); "
            "too tight to survive the spread"
        )

    target = to_tick(entry + risk * reward_multiple, tick, mode="up")
    if target <= entry:
        raise PlanError(f"target {target} is not above entry {entry}")

    return ExitPlan(entry=entry, stop=stop, target=target, risk_per_share=risk,
                    reward_to_risk=(target - entry) / risk)


def plan_trigger_exit(range_high: Decimal, range_low: Decimal, tick: Decimal, *,
                      stop_mode: str = "or-mid",
                      reward_multiple: Decimal = Decimal(2),
                      slippage_ticks: int = 2) -> ExitPlan:
    """Stop and target for a RESTING trigger entry at the range high.

    The entry price is not yet known when the order is placed, so the plan is
    measured against the trigger-limit price -- the worst price the entry can
    fill at -- rather than an observed close. Measuring against the trigger
    itself would understate the risk by the slippage band.
    """
    entry = to_tick(range_high + tick + tick * slippage_ticks, tick, mode="up")
    return plan_exit(entry, range_high, range_low, tick, stop_mode=stop_mode,
                     reward_multiple=reward_multiple)
