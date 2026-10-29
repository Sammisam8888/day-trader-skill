"""
Position sizing and the risk gate.

Sizing answers "how many shares fit inside every limit at once", and says
which limit decided it -- a stop too far away for the max loss is a refusal,
never a quietly smaller order.

The gate is the last check before a ticket can be shown. It returns PASS or
BLOCK with a reason, and it is deliberately boring: every rule is a simple
comparison, because a clever risk check that nobody can read is worse than a
dull one.

Closing a position is never blocked by a day limit. Being unable to get out
because you have hit your trade count is the one failure mode that turns a
bad day into an unbounded one.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session import IST, Session  # noqa: E402

BROKERAGE_PER_ORDER = Decimal("10")      # flat, per the dashboard


@dataclass
class Limits:
    max_loss_per_trade: Decimal = Decimal("500")
    daily_loss_limit: Decimal = Decimal("1500")
    max_trades_per_day: int = 3
    max_position_value: Decimal = Decimal("50000")
    cash_buffer_pct: Decimal = Decimal("10")      # keep this % of cash unspent
    min_reward_to_risk: Decimal = Decimal("1.5")
    blocked_symbols: tuple = ()


@dataclass(frozen=True)
class Sizing:
    qty: int
    limit_hit: str
    position_value: Decimal
    loss_at_stop: Decimal
    round_trip_cost: Decimal

    @property
    def ok(self) -> bool:
        return self.qty > 0


def size_position(plan, cash: Decimal, limits: Limits) -> Sizing:
    """Whole shares that fit inside every limit. qty 0 means refused."""
    risk = plan.risk_per_share
    if risk <= 0:
        return Sizing(0, "invalid risk per share", Decimal(0), Decimal(0), Decimal(0))

    spendable = cash * (Decimal(100) - limits.cash_buffer_pct) / Decimal(100)

    caps = {
        "max loss per trade": int(limits.max_loss_per_trade / risk),
        "position cap": int(limits.max_position_value / plan.entry),
        "cash after buffer": int(spendable / plan.entry),
    }
    qty = min(caps.values())
    decided_by = min(caps, key=lambda k: caps[k])

    if qty <= 0:
        return Sizing(0, f"{decided_by} allows 0 shares", Decimal(0), Decimal(0), Decimal(0))

    # Two orders per round trip: the entry, and whichever leg closes it.
    cost = BROKERAGE_PER_ORDER * 2
    return Sizing(qty, decided_by, plan.entry * qty, risk * qty, cost)


@dataclass
class DayState:
    """What has already happened today. Supplied by the caller."""
    trades_taken: int = 0
    risk_committed: Decimal = Decimal(0)
    open_symbols: tuple = ()


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = "PASS" if self.passed else "BLOCK"
        return head + (": " + "; ".join(self.reasons) if self.reasons else "")


def check_entry_order(*, symbol: str, plan, sizing: Sizing, session: Session,
                      now: datetime, day: DayState, limits: Limits) -> Verdict:
    reasons: List[str] = []

    if symbol.upper() in {s.upper() for s in limits.blocked_symbols}:
        reasons.append(f"{symbol} is on your blocked list")

    phase = session.phase(now.astimezone(IST))
    if not session.accepts_new_entries(now):
        reasons.append(f"no new entries during {phase}")

    if not sizing.ok:
        reasons.append(f"sizing refused: {sizing.limit_hit}")

    if not plan.is_sane:
        reasons.append(f"plan is not long-shaped: {plan.stop} / {plan.entry} / {plan.target}")

    if plan.reward_to_risk < limits.min_reward_to_risk:
        reasons.append(
            f"reward:risk {plan.reward_to_risk:.2f} is under your minimum "
            f"{limits.min_reward_to_risk}"
        )

    if sizing.loss_at_stop > limits.max_loss_per_trade:
        reasons.append(
            f"loss at stop {sizing.loss_at_stop} exceeds max per trade "
            f"{limits.max_loss_per_trade}"
        )

    if day.trades_taken >= limits.max_trades_per_day:
        reasons.append(
            f"already took {day.trades_taken} trades today "
            f"(limit {limits.max_trades_per_day})"
        )

    committed = day.risk_committed + sizing.loss_at_stop
    if committed > limits.daily_loss_limit:
        reasons.append(
            f"total risk today would be {committed}, over your daily limit "
            f"{limits.daily_loss_limit}"
        )

    if symbol.upper() in {s.upper() for s in day.open_symbols}:
        reasons.append(f"already holding {symbol}; this would double the position")

    return Verdict(not reasons, reasons)


def check_exit_order(*, symbol: str, qty: int, held_qty: int) -> Verdict:
    """Getting out is never blocked by a day limit -- only by arithmetic."""
    reasons: List[str] = []
    if qty <= 0:
        reasons.append("exit quantity must be positive")
    if qty > held_qty:
        reasons.append(f"cannot sell {qty}; only {held_qty} held in {symbol}")
    return Verdict(not reasons, reasons)
