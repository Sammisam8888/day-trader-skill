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

import charges as charges_mod  # noqa: E402
from session import IST, Session  # noqa: E402

BROKERAGE_PER_ORDER = charges_mod.BROKERAGE_PER_ORDER

# A trade has to be worth doing after costs. At Rs 10 an order, a round trip
# is Rs 20 before STT and exchange charges, and on a small account that can
# exceed the entire gain at the target: a 1-share KOTAKBANK trade risking
# Rs 2.10 to make Rs 4.30 loses Rs 15.70 WHEN IT WINS.
#
# So the gain at target must clear costs by this multiple. Three is not a
# precise figure -- it is the point where costs stop dominating the outcome.
MIN_REWARD_TO_COST = Decimal(3)

# How much position value may be held per rupee of actual cash. INTRADAY
# (MIS) product carries broker leverage -- roughly 5x on liquid large caps --
# so the margin balance will happily fund several times the cash. On
# 2026-11-04 three duplicate orders put 68,338 of RELIANCE against 25,189 of
# margin, 2.7x, and nothing objected: leverage absorbed it silently.
#
# 1.0 means never hold more position value than cash. Raising it is a
# deliberate decision about borrowed money, not a tuning parameter.
MAX_LEVERAGE = Decimal(os.environ.get("TRADER_MAX_LEVERAGE", "1.0"))


@dataclass
class Limits:
    max_loss_per_trade: Decimal = Decimal("500")
    max_leverage: Decimal = MAX_LEVERAGE
    daily_loss_limit: Decimal = Decimal("1500")
    max_trades_per_day: int = 3
    # None means "derive it from cash and max_leverage" rather than a fixed
    # figure that silently permits leverage on a small account.
    max_position_value: Optional[Decimal] = None
    cash_buffer_pct: Decimal = Decimal("10")      # keep this % of cash unspent
    min_reward_to_risk: Decimal = Decimal("1.5")
    min_reward_to_cost: Decimal = MIN_REWARD_TO_COST
    blocked_symbols: tuple = ()
    # Every setup here is "long breakout on a large cap". On a day the index
    # drops 1.5% they all stop out together, within minutes: the daily limit
    # is reached in one move rather than one trade at a time. Fewer
    # concurrent positions is the only honest answer to a correlated book.
    max_concurrent_positions: int = 2
    # Distance from the day's circuit band. A stock at its lower circuit has
    # no matched sellers and cannot be exited at any price; one near its
    # upper circuit has no room left for a target to fill.
    min_circuit_distance_pct: Decimal = Decimal("2")


@dataclass(frozen=True)
class Sizing:
    qty: int
    limit_hit: str
    position_value: Decimal
    loss_at_stop: Decimal
    round_trip_cost: Decimal
    gain_at_target: Decimal = Decimal(0)

    @property
    def ok(self) -> bool:
        return self.qty > 0

    @property
    def net_if_target_hits(self) -> Decimal:
        return self.gain_at_target - self.round_trip_cost


def _setting(key: str, default: str) -> str:
    try:
        import common
        return common.setting(key, default)
    except Exception:                                   # noqa: BLE001
        return default


def size_position(plan, cash: Decimal, limits: Limits) -> Sizing:
    """Whole shares that fit inside every limit. qty 0 means refused."""
    risk = plan.risk_per_share
    if risk <= 0:
        return Sizing(0, "invalid risk per share", Decimal(0), Decimal(0), Decimal(0))

    # Both caps scale with max_leverage, so the knob means what it says. At
    # the default of 1.0 neither cap ever exceeds cash.
    buying_power = cash * limits.max_leverage
    spendable = buying_power * (Decimal(100) - limits.cash_buffer_pct) / Decimal(100)
    position_cap = (limits.max_position_value
                    if limits.max_position_value is not None
                    else buying_power)

    # The loss at the stop is not just entry-to-stop: the stop order fills
    # past its trigger in a fast move, and the losing round trip still pays
    # charges. Both come out of the max loss per trade.
    import os as _os
    try:
        slip_pct = Decimal(_os.environ.get("TRADER_STOP_SLIP_PCT") or _setting("TRADER_STOP_SLIP_PCT", "0.05"))
    except Exception:                                   # noqa: BLE001
        slip_pct = Decimal("0.05")
    slip = plan.entry * slip_pct / 100
    per_share_cost = plan.entry * Decimal("0.0006")      # STT+exchange+stamp+GST, both legs, about 0.06%
    room = limits.max_loss_per_trade - 2 * charges_mod.BROKERAGE_PER_ORDER
    caps = {
        "max loss per trade": int(max(room, Decimal(0)) / (risk + slip + per_share_cost)),
        "position cap": int(position_cap / plan.entry),
        "cash after buffer": int(spendable / plan.entry),
    }
    qty = min(caps.values())
    decided_by = min(caps, key=lambda k: caps[k])

    if qty <= 0:
        return Sizing(0, f"{decided_by} allows 0 shares", Decimal(0), Decimal(0),
                      Decimal(0))

    # Every statutory charge, not just brokerage: STT, exchange, SEBI, IPFT,
    # stamp duty and GST. On a small position GST alone is most of the gap
    # between the flat Rs 20 this used to assume and the real figure.
    short = getattr(plan, "side", "B") == "S"
    cost = (charges_mod.round_trip(plan.target * qty, plan.entry * qty) if short
            else charges_mod.cost_of_plan(plan.entry, plan.target, qty)).total
    return Sizing(qty, decided_by, plan.entry * qty, risk * qty, cost,
                  gain_at_target=abs(plan.target - plan.entry) * qty)


@dataclass
class DayState:
    """What has already happened today. Supplied by the caller."""
    trades_taken: int = 0
    risk_committed: Decimal = Decimal(0)
    open_symbols: tuple = ()
    # Position value already held or committed, for the leverage cap.
    exposure: Decimal = Decimal(0)
    # Open-position P&L. A loss that has not been realised is still a loss,
    # and a daily limit that ignores it is not a daily limit.
    unrealised_pnl: Decimal = Decimal(0)


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        head = "PASS" if self.passed else "BLOCK"
        return head + (": " + "; ".join(self.reasons) if self.reasons else "")


def check_entry_order(*, symbol: str, plan, sizing: Sizing, session: Session,
                      now: datetime, day: DayState, limits: Limits,
                      cash_for_leverage: Decimal = Decimal(0),
                      upper_limit: Optional[Decimal] = None,
                      lower_limit: Optional[Decimal] = None) -> Verdict:
    reasons: List[str] = []

    open_count = len(set(day.open_symbols))
    if open_count >= limits.max_concurrent_positions:
        reasons.append(
            f"already {open_count} open position(s); limit "
            f"{limits.max_concurrent_positions} because they are all the "
            f"same bet on the same index"
        )

    short = getattr(plan, "side", "B") == "S"
    if short:
        # mirror: the target needs room above the lower circuit, the stop
        # must stay exitable below the upper one
        if lower_limit and lower_limit > 0:
            room = (plan.target - lower_limit) / plan.entry * 100
            if room < limits.min_circuit_distance_pct:
                reasons.append(f"short target {plan.target} is within {room:.1f}% of the lower circuit {lower_limit}")
        if upper_limit and upper_limit > 0:
            room = (upper_limit - plan.stop) / plan.entry * 100
            if room < limits.min_circuit_distance_pct:
                reasons.append(f"short stop {plan.stop} is within {room:.1f}% of the upper circuit {upper_limit}")
        upper_limit = lower_limit = None
    if upper_limit and upper_limit > 0:
        room = (upper_limit - plan.target) / plan.entry * 100
        if room < limits.min_circuit_distance_pct:
            reasons.append(
                f"target {plan.target} is within {room:.1f}% of the upper "
                f"circuit {upper_limit}; no room for the move"
            )
    if lower_limit and lower_limit > 0:
        room = (plan.stop - lower_limit) / plan.entry * 100
        if room < limits.min_circuit_distance_pct:
            reasons.append(
                f"stop {plan.stop} is within {room:.1f}% of the lower "
                f"circuit {lower_limit}; it may not be exitable"
            )

    if symbol.upper() in {s.upper() for s in limits.blocked_symbols}:
        reasons.append(f"{symbol} is on your blocked list")

    phase = session.phase(now.astimezone(IST))
    if not session.accepts_new_entries(now):
        reasons.append(f"no new entries during {phase}")

    if not sizing.ok:
        reasons.append(f"sizing refused: {sizing.limit_hit}")

    if not plan.is_sane:
        reasons.append(f"plan levels are not in order for a {'short' if short else 'long'}: "
                       f"stop {plan.stop} / entry {plan.entry} / target {plan.target}")

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

    # Costs are not a rounding error on a small account: they can exceed the
    # entire gain at the target, making a winning trade a losing one. The
    # multiple is the owner's to set, but never below 1: a trade that loses
    # when it wins is not a trade at any setting.
    if sizing.ok and sizing.gain_at_target < sizing.round_trip_cost * max(limits.min_reward_to_cost, Decimal(1)):
        net = sizing.net_if_target_hits
        reasons.append(
            f"not worth the costs: gain at target {sizing.gain_at_target:.2f} "
            f"vs {sizing.round_trip_cost} brokerage"
            + (f" -- this LOSES {-net:.2f} even if the target hits"
               if net <= 0 else
               f" -- only {net:.2f} net, before STT and exchange charges")
        )

    # Leverage: total held plus this order, against actual cash.
    exposure = day.exposure + sizing.position_value
    ceiling = cash_for_leverage * limits.max_leverage
    if cash_for_leverage > 0 and exposure > ceiling:
        reasons.append(
            f"leverage: this would hold {exposure:.0f} of position against "
            f"{cash_for_leverage:.0f} cash "
            f"({exposure / cash_for_leverage:.2f}x, limit {limits.max_leverage}x)"
        )

    # Unrealised losses count against the daily limit. They are the only kind
    # you have while a position is still open.
    total_pnl = day.unrealised_pnl
    if total_pnl < 0 and -total_pnl + sizing.loss_at_stop > limits.daily_loss_limit:
        reasons.append(
            f"open positions are down {-total_pnl:.0f}; adding {sizing.loss_at_stop:.0f} "
            f"of risk would pass the daily limit {limits.daily_loss_limit}"
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
