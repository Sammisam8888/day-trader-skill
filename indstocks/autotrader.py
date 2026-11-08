"""
The scanner: every few minutes in the entry window, for each symbol on the
list, decide whether to rest a breakout entry at the exchange.

Rule (unchanged from the INDstocks version, now on Shoonya):
  - opening range = high/low of the first 15 minutes
  - rest a BUY SL-LMT: trigger one tick above the range high, limit two
    ticks above that (the worst fill the plan accepts)
  - stop at the range midpoint, target at 2R; the FILL MONITOR places the
    stop at the exchange the moment the entry fills, and manages the target
  - resting orders are free and do not count; FILLS count toward the
    concurrent cap, the trade cap and leverage

Guards, each of which ends the run rather than warning:
  LIVE flag, kill switch, not logged in, daily loss (realised + open),
  leverage already at the cap, trade cap, cost gate, circuit distance,
  reward:risk, and "price already through the trigger" (never chase).

Every decision is logged with its reason. Nothing is placed unless
TRADER_LIVE=true; otherwise the same decisions are logged as would_place.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import risk as risk_mod                                  # noqa: E402
import session as sess                                   # noqa: E402
import strategy as strat                                 # noqa: E402
from market import opening_range, to_tick                # noqa: E402
from shoonya import LoginRequired, Shoonya, ShoonyaError  # noqa: E402

JOB = "auto"


def log(rec: dict) -> dict:
    return common.log(JOB, rec)


def min_order_value() -> Decimal:
    return Decimal(common.setting("TRADER_MIN_ORDER_VALUE", "5000"))


def max_resting() -> int:
    return int(common.setting("TRADER_MAX_RESTING", "6"))


def reconcile(broker, placements: list) -> dict:
    """Split today's placements by what the broker says happened.

    filled   traded_qty > 0     a trade; holds capital; counts toward caps
    resting  pending, 0 traded  free; remembered so it is not placed twice
    dead     cancelled/rejected with nothing traded: forgotten
    """
    try:
        book = {o["id"]: o for o in broker.orders()}
    except ShoonyaError:
        # Conservative on error: treat every placement as resting so nothing
        # is placed twice, and none as filled so nothing is double-counted.
        return {"filled": [], "resting": [p.get("symbol") for p in placements],
                "risk": Decimal(0)}
    filled, resting, risk = [], [], Decimal(0)
    for p in placements:
        o = book.get(str(p.get("order_id")))
        if o is None:
            resting.append(p.get("symbol"))
            continue
        if o["filled"] > 0:
            filled.append(p.get("symbol"))
            risk += Decimal(str(p.get("risk") or 0))
        elif o["pending"]:
            resting.append(p.get("symbol"))
    return {"filled": filled, "resting": resting, "risk": risk}


def run(broker=None, now=None) -> int:
    now = now or sess.now()
    session = sess.session_for(now)

    reason = common.halted()
    if reason:
        log({"event": "halted", "reason": reason})
        return 0
    if session is None:
        return 0
    if not session.accepts_new_entries(now):
        log({"event": "skipped", "reason": f"phase {session.phase(now)}"})
        return 0

    broker = broker or Shoonya()
    limits = common.limits()
    try:
        cash = broker.cash()
        positions = broker.positions()
    except LoginRequired as exc:
        log({"event": "login_required", "reason": str(exc)})
        common.notify("Shoonya login needed", "Open the dashboard and tap Log in.", priority="high")
        return 0
    except ShoonyaError as exc:
        log({"event": "abort", "reason": f"could not read account: {exc}"})
        return 1

    held = [p for p in positions if p["qty"] > 0]
    exposure = sum((p["avg"] * p["qty"] for p in held), Decimal(0))
    realised = sum((p["realised"] for p in positions), Decimal(0))
    unrealised = sum((p["unrealised"] for p in positions), Decimal(0))

    if realised + unrealised <= -limits.daily_loss_limit:
        log({"event": "halted", "reason": "daily loss limit reached",
             "realised": str(realised), "unrealised": str(unrealised),
             "limit": str(limits.daily_loss_limit)})
        return 0
    if cash > 0 and exposure >= cash * limits.max_leverage:
        log({"event": "halted", "reason": "leverage ceiling already reached",
             "exposure": str(exposure), "cash": str(cash)})
        return 0

    placements = common.events("placed")
    rec = reconcile(broker, placements)
    if len(rec["filled"]) >= limits.max_trades_per_day:
        log({"event": "halted", "reason": "trade limit reached", "fills": len(rec["filled"])})
        return 0

    held_syms = {p["symbol"] for p in held}
    day = risk_mod.DayState(trades_taken=len(rec["filled"]), risk_committed=rec["risk"],
                            open_symbols=tuple(held_syms), exposure=exposure,
                            unrealised_pnl=unrealised)

    # Size each order so that if every free slot fills at once, the total
    # still fits inside cash x leverage. Slots are capped by how many viable
    # orders the free cash can fund, so a large slot count cannot shrink
    # each order to nothing (2026-11-05: Rs 1,387 per order, zero shares).
    buying_power = cash * limits.max_leverage
    free_cash = max(buying_power - exposure, Decimal(0))
    free_slots = max(limits.max_concurrent_positions - len(held_syms), 0)
    viable = max(int(free_cash / min_order_value()), 1) if free_cash > 0 else 0
    slots = min(free_slots, viable)
    per_order = (free_cash / slots) if slots else Decimal(0)
    # sizing applies max_leverage itself; hand it cash-equivalent
    per_order_cash = per_order / limits.max_leverage if limits.max_leverage > 0 else Decimal(0)

    log({"event": "run_start", "phase": session.phase(now), "cash": str(cash),
         "exposure": str(exposure), "realised": str(realised), "unrealised": str(unrealised),
         "fills": len(rec["filled"]), "resting": len(rec["resting"]),
         "free_slots": free_slots, "per_order": str(round(per_order, 2))})

    if slots <= 0:
        log({"event": "stop", "reason": "no free slots"})
        return 0

    placed = 0
    budget = max_resting() - len(rec["resting"])
    for sym in common.symbols():
        if placed >= budget:
            log({"event": "stop", "reason": "resting-order ceiling reached"})
            break
        if sym in held_syms or sym in rec["resting"]:
            continue
        if common.halted():
            log({"event": "halted", "reason": "kill switch during run"})
            break
        try:
            inst = broker.instrument(sym)
            candles = broker.candles(inst.token, session.open, min(now, session.close))
            q = broker.quote(inst.token, inst.exch)
        except KeyError as exc:
            log({"event": "skip", "symbol": sym, "reason": str(exc)})
            continue
        except ShoonyaError as exc:
            log({"event": "skip", "symbol": sym, "reason": f"data: {exc}"[:200]})
            continue

        rng = opening_range(candles, session)
        if rng is None:
            log({"event": "no_data", "symbol": sym, "reason": "no opening-range candles yet"})
            continue
        high, low = rng
        try:
            plan = strat.plan_trigger_exit(high, low, inst.tick)
        except strat.PlanError as exc:
            log({"event": "skip", "symbol": sym, "reason": str(exc)})
            continue
        trigger = to_tick(high + inst.tick, inst.tick, mode="up")
        ltp = q["ltp"]
        if ltp >= trigger:
            # A buy trigger must sit above the market. Placing one below it
            # fills immediately at whatever the price is: a chase.
            log({"event": "skip", "symbol": sym,
                 "reason": f"price {ltp} already through trigger {trigger}; not chasing"})
            continue
        if ltp < low:
            log({"event": "skip", "symbol": sym,
                 "reason": f"price {ltp} below the range low {low}; no breakout setup"})
            continue

        sizing = risk_mod.size_position(plan, per_order_cash, limits)
        if not sizing.ok:
            log({"event": "unsized", "symbol": sym, "reason": sizing.limit_hit,
                 "per_order": str(round(per_order, 2))})
            continue
        verdict = risk_mod.check_entry_order(
            symbol=sym, plan=plan, sizing=sizing, session=session, now=now, day=day,
            limits=limits, cash_for_leverage=cash,
            upper_limit=q["uc"] or None, lower_limit=q["lc"] or None)
        if not verdict.passed:
            log({"event": "blocked", "symbol": sym, "reasons": verdict.reasons})
            continue

        value = sizing.position_value
        record = {
            "symbol": sym, "tsym": inst.tsym, "token": inst.token, "exch": inst.exch,
            "tick": str(inst.tick), "qty": sizing.qty,
            "trigger": str(trigger), "entry": str(plan.entry),
            "stop": str(plan.stop), "target": str(plan.target),
            "range_high": str(high), "range_low": str(low), "ltp_at_placement": str(ltp),
            "risk": str(sizing.loss_at_stop), "costs": str(sizing.round_trip_cost),
            "gross_if_target": str(sizing.gain_at_target),
            "net_if_target": str(sizing.net_if_target_hits),
            "position_value": str(value),
            "leverage": str(round(value / cash, 2)) if cash else "0",
            "why": (f"opening range {session.open:%H:%M}-{session.opening_range_end:%H:%M} "
                    f"was {low}-{high}; buy if it trades at {trigger} (range high + 1 tick), "
                    f"limit {plan.entry}; stop {plan.stop} at the range midpoint, "
                    f"target {plan.target} = 2R"),
        }
        if not common.live():
            log({"event": "would_place", **record})
            placed += 1
            continue
        try:
            oid = broker.place(inst, side="B", qty=sizing.qty, price=plan.entry,
                               trigger=trigger, remarks=f"auto/{sym}")
        except ShoonyaError as exc:
            log({"event": "order_failed", "symbol": sym, "reason": str(exc)[:300]})
            continue
        log({"event": "placed", "order_id": oid, **record})
        placed += 1

    log({"event": "run_end", "placed": placed})
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
