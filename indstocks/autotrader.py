"""
The scanner: every few minutes, ask the decider (brain.py: Claude, or the
opening-range rule as fallback) what to do, then validate, size and place.

Claude decides WHAT and WHERE. This file decides WHETHER it is allowed and
HOW MUCH, and nothing here can be talked out of a limit:

  account   LIVE flag, kill switch, login, daily loss (realised + open),
            leverage ceiling, trade cap
  per entry symbol in the universe, not already held or resting on that
            side, entries per symbol, shorts allowed, levels in the right
            order for the side, trigger on the right side of the market,
            stop at least 2 ticks and 0.1% away, reward:risk, cost gate,
            circuit distance, entry window, sizing from max loss per trade
            and free buying power

Every decision and every refusal is logged with its reason. With
TRADER_LIVE=false the same pipeline runs and logs would_place.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brain                                             # noqa: E402
import common                                            # noqa: E402
import regime                                            # noqa: E402
import risk as risk_mod                                  # noqa: E402
import session as sess                                   # noqa: E402
import strategy as strat                                 # noqa: E402
from market import to_tick                               # noqa: E402
from shoonya import LoginRequired, Shoonya, ShoonyaError  # noqa: E402

JOB = "auto"
SLIP_TICKS = 2


def log(rec: dict) -> dict:
    return common.log(JOB, rec)


def min_order_value() -> Decimal:
    return Decimal(common.setting("TRADER_MIN_ORDER_VALUE", "5000"))


def max_resting() -> int:
    return int(common.setting("TRADER_MAX_RESTING", "6"))


def max_entries() -> int:
    return int(common.setting("TRADER_MAX_ENTRIES_PER_SYMBOL", "2"))


def reconcile(broker_orders: list, placements: list) -> dict:
    """filled (a trade), resting (free, remembered), per symbol."""
    book = {o["id"]: o for o in broker_orders}
    filled, resting, fills_by_symbol, risk = [], [], {}, Decimal(0)
    for p in placements:
        o = book.get(str(p.get("order_id")))
        sym, side = p.get("symbol"), p.get("side", "B")
        if o is None:
            continue
        if o["filled"] > 0:
            filled.append(sym)
            fills_by_symbol[sym] = fills_by_symbol.get(sym, 0) + 1
            risk += Decimal(str(p.get("risk") or 0))
        elif o["pending"]:
            resting.append((sym, side))
    return {"filled": filled, "resting": resting, "by_symbol": fills_by_symbol, "risk": risk}


def build_plan(d: dict, inst, q: dict):
    """(plan, trigger_or_None, limit_price) from a decision, or raise PlanError.

    For a stop entry the plan is measured from the worst fill the order can
    get (trigger plus slippage), not the trigger itself."""
    side = "B" if str(d.get("side", "")).upper() in ("BUY", "B", "LONG") else "S"
    order = str(d.get("order", "stop")).lower()
    tick = inst.tick
    entry = Decimal(str(d["entry"]))
    stop = Decimal(str(d["stop"]))
    target = Decimal(str(d["target"]))
    ltp = q["ltp"]
    trigger = None
    if order == "stop":
        trigger = to_tick(entry, tick, mode="up" if side == "B" else "down")
        if side == "B" and trigger <= ltp:
            raise strat.PlanError(f"buy trigger {trigger} is not above the price {ltp}; that would chase")
        if side == "S" and trigger >= ltp:
            raise strat.PlanError(f"sell trigger {trigger} is not below the price {ltp}; that would chase")
        limit = trigger + tick * SLIP_TICKS if side == "B" else trigger - tick * SLIP_TICKS
    elif order == "limit":
        limit = to_tick(entry, tick)
        if side == "B" and limit >= ltp:
            raise strat.PlanError(f"buy limit {limit} is not below the price {ltp}; use order 'now'")
        if side == "S" and limit <= ltp:
            raise strat.PlanError(f"sell limit {limit} is not above the price {ltp}; use order 'now'")
    elif order == "now":
        ref = (q["ask"] or ltp) if side == "B" else (q["bid"] or ltp)
        limit = to_tick(ref * (Decimal("1.002") if side == "B" else Decimal("0.998")), tick,
                        mode="up" if side == "B" else "down")
    else:
        raise strat.PlanError(f"unknown order type {order!r}")
    plan = strat.make_plan(side, limit, stop, target, tick)
    if plan.risk_per_share < limit * Decimal("0.001"):
        raise strat.PlanError(f"stop {plan.stop} is within 0.1% of entry {limit}: inside normal noise")
    return plan, trigger, limit


def adjust(broker, d: dict, held: list, orders: list, placements: list, limits, day_pnl: Decimal) -> tuple:
    """Move the stop and/or target of a position this system opened.
    Returns (outcome, reason) for the transcript.

    The new stop must sit on the losing side of the price, and the loss it
    allows (from the fill price, for the open quantity) must fit both the
    max loss per trade and the loss room left today. Targets must be on the
    winning side of the price. The stop order at the exchange is modified
    now; the target is recorded for the fill monitor."""
    from fillmonitor import stop_limit
    sym = str(d.get("symbol", "")).upper()
    why = str(d.get("reason", ""))[:300]
    pos = next((p for p in held if p["symbol"] == sym), None)
    if not pos:
        return "refused", f"adjust {sym}: no open position"
    book = {o["id"]: o for o in orders}
    plan = next((p for p in placements if str(p.get("token")) == str(pos["token"])
                 and book.get(str(p.get("order_id")), {}).get("filled", 0) > 0), None)
    if not plan:
        return "refused", f"adjust {sym}: not opened by this system (manual positions keep their % guard)"
    long_ = pos["qty"] > 0
    ltp = pos["ltp"]
    try:
        ltp = broker.quote(pos["token"])["ltp"] or ltp
    except ShoonyaError:
        pass
    tick = Decimal(str(plan.get("tick") or "0.01"))
    qty = abs(pos["qty"])
    parent = str(plan["order_id"])
    stop_order = next((o for o in orders if o["pending"] and o["token"] == pos["token"]
                       and o["side"] == ("S" if long_ else "B")), None)
    new_stop = new_target = None
    problems = []
    if d.get("stop") is not None:
        new_stop = to_tick(Decimal(str(d["stop"])), tick, mode="down" if long_ else "up")
        if long_ and new_stop >= ltp or (not long_ and new_stop <= ltp):
            problems.append(f"stop {new_stop} is not on the losing side of the price {ltp}")
        loss = (pos["avg"] - new_stop) * qty if long_ else (new_stop - pos["avg"]) * qty
        if loss > limits.max_loss_per_trade:
            problems.append(f"stop {new_stop} risks {loss:.0f}, over max loss per trade {limits.max_loss_per_trade}")
        room = limits.daily_loss_limit + min(day_pnl, Decimal(0))
        if loss > 0 and loss > room:
            problems.append(f"stop {new_stop} risks {loss:.0f}, more than the {room:.0f} of loss room left today")
        if stop_order is None:
            problems.append("no stop order at the exchange to move (the fill monitor is placing one)")
    if d.get("target") is not None:
        new_target = to_tick(Decimal(str(d["target"])), tick, mode="down" if long_ else "up")
        if long_ and new_target <= ltp or (not long_ and new_target >= ltp):
            problems.append(f"target {new_target} is not on the winning side of the price {ltp}")
    if new_stop is None and new_target is None:
        problems.append("adjust needs a stop, a target or both")
    if problems:
        log({"event": "refused", "symbol": sym, "reason": "adjust: " + "; ".join(problems), "decider_reason": why})
        return "refused", "; ".join(problems)
    changes = []
    if new_stop is not None and common.live():
        inst = broker.instrument_for(pos)
        try:
            broker.modify(stop_order, inst, qty=stop_order["qty"],
                          price=stop_limit(new_stop, tick, "S" if long_ else "B"), trigger=new_stop)
        except ShoonyaError as exc:
            log({"event": "modify_failed", "order_id": stop_order["id"], "reason": str(exc)[:200]})
            return "failed", f"stop modify rejected: {exc}"[:300]
        changes.append(f"stop {stop_order['trigger']} -> {new_stop}")
    elif new_stop is not None:
        changes.append(f"(shadow) stop -> {new_stop}")
    if new_target is not None:
        changes.append(f"target -> {new_target}")
    log({"event": "plan_adjusted", "parent": parent, "symbol": sym,
         "stop": str(new_stop) if new_stop is not None else None,
         "target": str(new_target) if new_target is not None else None, "reason": f"decider: {why}"})
    return "adjusted", "; ".join(changes)


def retire_against(broker, orders: list, placements: list, direction: dict, rec: dict) -> None:
    """A resting entry the market has turned against is cancelled: a buy
    trigger left from 09:30 must not fill into a falling market at 11:00."""
    ours = {str(p.get("order_id")): p for p in placements}
    for o in orders:
        p = ours.get(o["id"])
        if not p or not o["pending"] or o["filled"] > 0:
            continue
        why = regime.check("BUY" if o["side"] == "B" else "SELL", p["symbol"], direction)
        if not why:
            continue
        if common.live():
            try:
                broker.cancel(o["id"])
            except ShoonyaError as exc:
                log({"event": "cancel_failed", "order_id": o["id"], "reason": str(exc)[:200]})
                continue
        log({"event": "entry_cancelled", "symbol": p["symbol"], "order_id": o["id"],
             "reason": f"direction changed: {why}"})
        key = (p["symbol"], p.get("side", "B"))
        if key in rec["resting"]:
            rec["resting"].remove(key)


def run(broker=None, now=None, *, decide=None) -> int:
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
        orders = broker.orders()
    except LoginRequired as exc:
        log({"event": "login_required", "reason": str(exc)})
        return 0
    except ShoonyaError as exc:
        log({"event": "abort", "reason": f"could not read account: {exc}"})
        return 1

    held = [p for p in positions if p["qty"] != 0]
    held_syms = {p["symbol"] for p in held}
    exposure = sum((abs(p["avg"] * p["qty"]) for p in held), Decimal(0))
    realised = sum((p["realised"] for p in positions), Decimal(0))
    unrealised = sum((p["unrealised"] for p in positions), Decimal(0))
    if realised + unrealised <= -limits.daily_loss_limit:
        log({"event": "halted", "reason": "daily loss limit reached", "realised": str(realised),
             "unrealised": str(unrealised), "limit": str(limits.daily_loss_limit)})
        return 0
    if cash > 0 and exposure >= cash * limits.max_leverage:
        log({"event": "halted", "reason": "leverage ceiling already reached",
             "exposure": str(exposure), "cash": str(cash)})
        return 0

    placements = common.events("placed")
    rec = reconcile(orders, placements)
    if len(rec["filled"]) >= limits.max_trades_per_day:
        log({"event": "halted", "reason": "trade limit reached", "fills": len(rec["filled"])})
        return 0

    out = (decide or brain.decide)(broker, session, now, cash=cash, positions=positions, orders=orders,
                                   placements=placements, limits=limits)
    call_id = now.strftime("%H%M%S")
    log({"event": "brain_view", "call_id": call_id, "source": out.get("source"),
         "view": str(out.get("view", ""))[:500], "decisions": len(out.get("decisions", []))})
    actions: list = []
    direction = (out.get("ctx") or {}).get("direction")
    if direction:
        log({"event": "direction", "call_id": call_id, **direction["market"]})
        retire_against(broker, orders, placements, direction, rec)

    def act(i: int, outcome: str, reason: str = "", **extra) -> None:
        actions.append({"decision": i, "outcome": outcome, "reason": reason, **extra})

    day = risk_mod.DayState(trades_taken=len(rec["filled"]), risk_committed=rec["risk"],
                            open_symbols=tuple(held_syms), exposure=exposure, unrealised_pnl=unrealised)
    buying_power = cash * limits.max_leverage
    free_cash = max(buying_power - exposure, Decimal(0))
    free_slots = max(limits.max_concurrent_positions - len(held_syms), 0)
    viable = int(free_cash / min_order_value()) if free_cash > 0 else 0
    enterable = len([s for s in common.symbols() if s not in held_syms])
    slots = max(min(free_slots, viable, enterable), 0)
    slot_cash = (free_cash / slots) if slots else Decimal(0)
    universe = set(common.symbols())
    resting = set(rec["resting"])
    budget = max_resting() - len(resting)

    placed = 0
    for i, d in enumerate(out.get("decisions", [])):
        action = str(d.get("action", "")).lower()
        sym = str(d.get("symbol", "")).upper()
        why = str(d.get("reason", ""))[:400]
        if action == "cancel":
            oid = str(d.get("order_id", ""))
            mine = {str(p.get("order_id")) for p in placements}
            if oid not in mine:
                log({"event": "refused", "symbol": sym, "reason": f"cancel {oid}: not an entry this system placed"})
                act(i, "refused", f"cancel {oid}: not an entry this system placed")
                continue
            if common.live():
                try:
                    broker.cancel(oid)
                except ShoonyaError as exc:
                    log({"event": "cancel_failed", "order_id": oid, "reason": str(exc)[:200]})
                    act(i, "failed", f"cancel failed: {exc}"[:200], order_id=oid)
                    continue
            log({"event": "entry_cancelled", "symbol": sym, "order_id": oid, "reason": f"decider: {why}"})
            act(i, "cancelled", f"entry {oid} cancelled", order_id=oid)
            continue
        if action == "exit":
            pos = next((p for p in held if p["symbol"] == sym), None)
            if not pos:
                log({"event": "refused", "symbol": sym, "reason": "exit: no open position"})
                act(i, "refused", "exit: no open position")
                continue
            if common.live():
                import flatten
                oid = flatten.close_position(broker, pos, remarks=f"exit/{sym}")
                log({"event": "sold" if pos["qty"] > 0 else "covered", "symbol": sym, "qty": abs(pos["qty"]),
                     "order_id": oid, "reason": f"decider: {why}"})
                act(i, "exited", f"closed {abs(pos['qty'])} with a marketable limit", order_id=oid)
            else:
                log({"event": "would_exit", "symbol": sym, "reason": why})
                act(i, "would_exit", "shadow mode: nothing sent")
            continue
        if action == "adjust":
            act(i, *adjust(broker, d, held, orders, placements, limits, realised + unrealised))
            continue
        if action != "enter":
            act(i, "ignored", f"unknown action {action!r}")
            continue

        side = "B" if str(d.get("side", "")).upper() in ("BUY", "B", "LONG") else "S"
        if placed >= budget:
            log({"event": "refused", "symbol": sym, "reason": "resting-order ceiling reached"})
            act(i, "refused", "resting-order ceiling reached")
            continue
        problems = []
        if sym not in universe:
            problems.append(f"{sym} is not in the trading list")
        if sym in held_syms:
            problems.append(f"already holding {sym}")
        if (sym, side) in resting:
            problems.append(f"an entry on this side of {sym} is already resting")
        if rec["by_symbol"].get(sym, 0) >= max_entries():
            problems.append(f"{sym} already entered {rec['by_symbol'][sym]} time(s) today (max {max_entries()})")
        if side == "S" and not brain.allow_short():
            problems.append("shorts are switched off")
        against = regime.check("BUY" if side == "B" else "SELL", sym, direction)
        if against:
            problems.append(against)
        if slots <= 0:
            problems.append("no free slot or buying power")
        if problems:
            log({"event": "refused", "symbol": sym, "side": side, "reason": "; ".join(problems), "decider_reason": why})
            act(i, "refused", "; ".join(problems))
            continue
        try:
            inst, _, q = out["insts"][sym]
        except KeyError:
            log({"event": "refused", "symbol": sym, "reason": "no market data this scan"})
            act(i, "refused", "no market data this scan")
            continue
        try:
            plan, trigger, limit = build_plan(d, inst, q)
        except (strat.PlanError, KeyError, ValueError, ArithmeticError) as exc:
            log({"event": "refused", "symbol": sym, "side": side, "reason": str(exc)[:300], "decider_reason": why})
            act(i, "refused", str(exc)[:300])
            continue
        size = min(max(Decimal(str(d.get("size", 1) or 1)), Decimal("0.25")), Decimal(1))
        lev = limits.max_leverage if limits.max_leverage > 0 else Decimal(1)
        sizing = risk_mod.size_position(plan, slot_cash * size / lev, limits)
        if not sizing.ok:
            log({"event": "unsized", "symbol": sym, "side": side, "reason": sizing.limit_hit})
            act(i, "refused", f"sizing: {sizing.limit_hit}")
            continue
        verdict = risk_mod.check_entry_order(
            symbol=sym, plan=plan, sizing=sizing, session=session, now=now, day=day, limits=limits,
            cash_for_leverage=cash, upper_limit=q["uc"] or None, lower_limit=q["lc"] or None)
        if not verdict.passed:
            log({"event": "blocked", "symbol": sym, "side": side, "reasons": verdict.reasons, "decider_reason": why})
            act(i, "refused", "; ".join(verdict.reasons))
            continue

        record = {
            "symbol": sym, "side": side, "tsym": inst.tsym, "token": inst.token, "exch": inst.exch,
            "tick": str(inst.tick), "qty": sizing.qty, "order_type": str(d.get("order", "stop")),
            "trigger": str(trigger) if trigger is not None else None, "entry": str(limit),
            "stop": str(plan.stop), "target": str(plan.target),
            "risk": str(sizing.loss_at_stop), "costs": str(sizing.round_trip_cost),
            "gross_if_target": str(sizing.gain_at_target), "net_if_target": str(sizing.net_if_target_hits),
            "position_value": str(sizing.position_value),
            "leverage": str(round(sizing.position_value / cash, 2)) if cash else "0",
            "ltp_at_placement": str(q["ltp"]), "decided_by": out.get("source"),
            "confidence": d.get("confidence"), "why": why, "call_id": call_id,
        }
        # the two extremes, after charges: target hits vs stop hits
        import charges as charges_mod
        q_ = sizing.qty
        at_stop = (charges_mod.round_trip(limit * q_, plan.stop * q_) if side == "B"
                   else charges_mod.round_trip(plan.stop * q_, limit * q_)).total
        record["net_if_stop"] = str(-(sizing.loss_at_stop + at_stop))
        summary = (f"{'BUY' if side == 'B' else 'SELL'} {sizing.qty} {sym} "
                   f"{'trigger ' + str(trigger) if trigger is not None else 'limit ' + str(limit)} · "
                   f"SL {plan.stop} · TP {plan.target} · net {record['net_if_target']} / {record['net_if_stop']}")
        if not common.live():
            log({"event": "would_place", **record})
            act(i, "would_place", f"shadow: {summary}")
            placed += 1
            continue
        try:
            oid = broker.place(inst, side=side, qty=sizing.qty, price=limit, trigger=trigger,
                               remarks=f"auto/{sym}")
        except ShoonyaError as exc:
            log({"event": "order_failed", "symbol": sym, "reason": str(exc)[:300]})
            act(i, "failed", f"broker rejected: {exc}"[:300])
            continue
        log({"event": "placed", "order_id": oid, **record})
        act(i, "placed", summary, order_id=oid)
        resting.add((sym, side))
        placed += 1

    log({"event": "run_end", "placed": placed, "source": out.get("source")})
    tr = out.get("transcript") or {}
    brain.save_transcript({
        "id": call_id, "ts": now.isoformat(timespec="seconds"), "source": out.get("source"),
        "phase": session.phase(now), "latency_s": tr.get("latency_s"), "error": tr.get("error"),
        "asked": {"symbols": len(out.get("ctx", {}).get("markets", [])), "cash": str(cash),
                  "exposure": str(exposure), "positions": len(held), "resting": len(resting),
                  "pnl": str(realised + unrealised),
                  "direction": (direction or {}).get("market")},
        "prompt": tr.get("prompt", ""), "reply": tr.get("raw", ""),
        "view": out.get("view", ""), "decisions": out.get("decisions", []), "actions": actions,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
