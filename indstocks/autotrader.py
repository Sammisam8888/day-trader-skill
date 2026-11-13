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
import profit_budget                                     # noqa: E402
import common                                            # noqa: E402
import control                                           # noqa: E402
import daymode                                           # noqa: E402
import realtime                                          # noqa: E402
import regime                                            # noqa: E402
import risk as risk_mod                                  # noqa: E402
import sentiment                                         # noqa: E402
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


def parse_side(v):
    """BUY / SELL only. Anything else (blank, "hold", a typo) is not a trade:
    it must never default to a short."""
    s = str(v or "").strip().upper()
    if s in ("BUY", "B", "LONG"):
        return "B"
    if s in ("SELL", "S", "SHORT"):
        return "S"
    return None


def build_plan(d: dict, inst, q: dict):
    """(plan, trigger_or_None, limit_price) from a decision, or raise PlanError.

    For a stop entry the plan is measured from the worst fill the order can
    get (trigger plus slippage), not the trigger itself."""
    side = parse_side(d.get("side"))
    if side is None:
        raise strat.PlanError(f"side {d.get('side')!r} is not BUY or SELL")
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
        # The limit beyond the trigger must survive a fast break: 2 ticks missed the L&T
        # breakdown on 12 Nov. Use the larger of 2 ticks and TRADER_ENTRY_BAND_PCT (0.1%).
        # Capped at a quarter of the trade's risk so it never eats the reward:risk.
        # ...and so the plan still clears the minimum reward:risk measured from the limit.
        pct_band = trigger * Decimal(common.setting("TRADER_ENTRY_BAND_PCT", "0.1")) / 100
        stop_, tgt_ = Decimal(str(d["stop"])), Decimal(str(d["target"]))
        r_, rew_ = abs(trigger - stop_), abs(tgt_ - trigger)
        m_ = common.limits().min_reward_to_risk
        rr_room = (rew_ - m_ * r_) / (1 + m_) if rew_ > m_ * r_ else Decimal(0)
        band = max(tick * SLIP_TICKS, to_tick(min(pct_band, r_ / 4, rr_room), tick, mode="down"))
        limit = trigger + band if side == "B" else trigger - band
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
    manual = plan is None
    long_ = pos["qty"] > 0
    ltp = pos["ltp"]
    try:
        ltp = broker.quote(pos["token"])["ltp"] or ltp
    except ShoonyaError:
        pass
    if manual:
        # The owner's own position: Claude adopts it. Its first stop is placed
        # at the exchange here; the owner's loss limits do not cap a stop on a
        # trade the owner chose to take, but the stop must be on the losing side.
        try:
            tick = broker.instrument_for(pos).tick
        except Exception:                               # noqa: BLE001
            tick = Decimal("0.05")
        parent = f"manual:{pos['token']}"
    else:
        tick = Decimal(str(plan.get("tick") or "0.01"))
        parent = str(plan["order_id"])
    qty = abs(pos["qty"])
    stop_order = next((o for o in orders if o["pending"] and o["token"] == pos["token"]
                       and o["side"] == ("S" if long_ else "B")
                       and (o.get("type") == "SL-LMT" or str(o.get("remarks", "")).startswith("stop/"))), None)
    new_stop = new_target = None
    problems = []
    if d.get("stop") is not None:
        new_stop = to_tick(Decimal(str(d["stop"])), tick, mode="down" if long_ else "up")
        if long_ and new_stop >= ltp or (not long_ and new_stop <= ltp):
            problems.append(f"stop {new_stop} is not on the losing side of the price {ltp}")
        loss = (pos["avg"] - new_stop) * qty if long_ else (new_stop - pos["avg"]) * qty
        if not manual:
            if loss > limits.max_loss_per_trade:
                problems.append(f"stop {new_stop} risks {loss:.0f}, over max loss per trade {limits.max_loss_per_trade}")
            room = limits.daily_loss_limit + min(day_pnl, Decimal(0))
            if loss > 0 and loss > room:
                problems.append(f"stop {new_stop} risks {loss:.0f}, more than the {room:.0f} of loss room left today")
            if stop_order is None:
                problems.append("no stop order at the exchange to move (the fill monitor is placing one)")
    elif manual and stop_order is None and d.get("target") is not None:
        problems.append("a manual position needs a stop when Claude takes it over")
    if d.get("target") is not None:
        new_target = to_tick(Decimal(str(d["target"])), tick, mode="down" if long_ else "up")
        if long_ and new_target <= ltp or (not long_ and new_target >= ltp):
            problems.append(f"target {new_target} is not on the winning side of the price {ltp}")
        if not manual:
            # Targets only come closer. Pushing them away as price approaches is
            # how a winner never gets booked (13 Nov).
            current = Decimal(str(plan.get("target")))
            for r in common.events("plan_adjusted"):
                if str(r.get("parent")) == str(plan["order_id"]) and r.get("target"):
                    current = Decimal(str(r["target"]))
            if (long_ and new_target > current) or (not long_ and new_target < current):
                problems.append(f"target {new_target} is further than the current {current}: "
                                "targets may only move closer (book the profit, don't chase it)")
    if new_stop is None and new_target is None:
        problems.append("adjust needs a stop, a target or both")
    if problems:
        log({"event": "refused", "symbol": sym, "reason": "adjust: " + "; ".join(problems), "decider_reason": why})
        return "refused", "; ".join(problems)
    changes = []
    if new_stop is not None and stop_order is None and common.live():
        inst = broker.instrument_for(pos)
        try:
            oid = broker.place(inst, side="S" if long_ else "B", qty=qty,
                               price=stop_limit(new_stop, tick, "S" if long_ else "B"), trigger=new_stop,
                               remarks=f"stop/{sym}")
        except ShoonyaError as exc:
            log({"event": "stop_failed", "symbol": sym, "reason": str(exc)[:200]})
            return "failed", f"stop order rejected: {exc}"[:300]
        log({"event": "stop_placed", "parent": parent, "order_id": oid, "symbol": sym,
             "side": "S" if long_ else "B", "qty": qty, "trigger": str(new_stop), "adopted": True})
        changes.append(f"took over your {sym}: stop {new_stop} placed at the exchange")
    elif new_stop is not None and common.live():
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


def margin_needed(broker, inst, side, qty, limit, trigger, lev) -> Decimal:
    """Shoonya's margin for this order; if it cannot say, value / leverage plus 10%."""
    m = None
    try:
        m = broker.order_margin(inst, side=side, qty=qty, price=limit, trigger=trigger)
    except Exception:                                   # noqa: BLE001
        m = None
    return m if m is not None else (limit * qty / lev) * Decimal("1.10")


def gates_enforced() -> bool:
    """TRADER_GATES=enforce makes the direction/sentiment gates refuse trades;
    the default, advisory, leaves every trading decision to Claude."""
    return common.setting("TRADER_GATES", "advisory").lower() == "enforce"


def valid_minutes(d: dict):
    try:
        return max(5, min(int(d.get("valid_minutes")), 180))
    except (TypeError, ValueError):
        return None


def retire_against(broker, orders: list, placements: list, direction: dict, rec: dict) -> None:
    """A resting entry the market has turned against is cancelled: a buy
    trigger left from 09:30 must not fill into a falling market at 11:00."""
    ours = {str(p.get("order_id")): p for p in placements}
    for o in orders:
        p = ours.get(o["id"])
        if not p or not o["pending"] or o["filled"] > 0:
            continue
        why = regime.check("BUY" if o["side"] == "B" else "SELL", p["symbol"], direction)
        if not why or any("direction gate waived" in str(x) for x in (p.get("advisory") or [])):
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
    if control.check_raises(realised + unrealised):
        limits = common.limits()
    try:
        import metrics
        day_net = metrics.pnl(positions, {})["net"]      # after charges: what the account actually lost
    except Exception:                                   # noqa: BLE001
        day_net = realised + unrealised
    if min(day_net, realised + unrealised) <= -limits.daily_loss_limit:
        log({"event": "halted", "reason": "daily loss limit reached (after charges)", "realised": str(realised),
             "unrealised": str(unrealised), "net": str(day_net), "limit": str(limits.daily_loss_limit)})
        return 0
    if cash > 0 and exposure >= cash * limits.max_leverage:
        log({"event": "halted", "reason": "leverage ceiling already reached",
             "exposure": str(exposure), "cash": str(cash)})
        return 0

    placements = common.events("placed")
    rec = reconcile(orders, placements)
    base_trades = limits.max_trades_per_day
    extra = None
    if len(rec["filled"]) + len(rec["resting"]) >= base_trades:
        extra = profit_budget.room(positions, orders, placements, limits.max_loss_per_trade)
        if len(rec["filled"]) >= profit_budget.hard_cap(base_trades) or (
                len(rec["filled"]) >= base_trades and extra["room"] <= 0):
            log({"event": "halted", "reason": "trade limit reached", "fills": len(rec["filled"]),
                 "limit": base_trades, "hard_cap": profit_budget.hard_cap(base_trades), **{k: str(v) for k, v in extra.items()}})
            return 0
        import dataclasses
        limits = dataclasses.replace(limits, max_trades_per_day=profit_budget.hard_cap(base_trades))
        log({"event": "profit_funded", **{k: str(v) for k, v in extra.items()}, "fills": len(rec["filled"])})

    out = (decide or brain.decide)(broker, session, now, cash=cash, positions=positions, orders=orders,
                                   placements=placements, limits=limits)
    call_id = now.strftime("%H%M%S")
    log({"event": "brain_view", "call_id": call_id, "source": out.get("source"),
         "view": str(out.get("view", ""))[:500], "decisions": len(out.get("decisions", []))})
    actions: list = []
    direction = (out.get("ctx") or {}).get("direction")
    if (out.get("ctx") or {}).get("day_mode"):
        log({"event": "day_mode", "call_id": call_id, **{k: v for k, v in out["ctx"]["day_mode"].items()}})
    mood = sentiment.load(now)
    if direction:
        log({"event": "direction", "call_id": call_id, **direction["market"]})
        if daymode.direction_enforced() and not ((out.get("ctx") or {}).get("day_mode") or {}).get("flipped"):
            retire_against(broker, orders, placements, direction, rec)

    def act(i: int, outcome: str, reason: str = "", **extra) -> None:
        actions.append({"decision": i, "outcome": outcome, "reason": reason, **extra})

    day = risk_mod.DayState(trades_taken=len(rec["filled"]), risk_committed=rec["risk"],
                            open_symbols=tuple(held_syms), exposure=exposure, unrealised_pnl=unrealised)
    buying_power = cash * limits.max_leverage
    # Pending entries block margin at Shoonya just like positions: count them.
    our_ids = {str(p.get("order_id")) for p in placements}
    resting_value = sum((o["price"] * o["qty"] for o in orders
                         if o["id"] in our_ids and o["pending"] and o["filled"] == 0), Decimal(0))
    free_cash = max(buying_power - exposure - resting_value, Decimal(0))
    try:
        margin_free = broker.free_margin()
    except Exception:                                   # noqa: BLE001
        margin_free = None
    free_slots = max(limits.max_concurrent_positions - len(held_syms), 0)
    viable = int(free_cash / min_order_value()) if free_cash > 0 else 0
    enterable = len([s for s in common.symbols() if s not in held_syms])
    slots = max(min(free_slots, viable, enterable), 0)
    slot_cash = (free_cash / slots) if slots else Decimal(0)
    universe = set(common.symbols())
    resting = set(rec["resting"])
    budget = max_resting() - len(resting)

    placed = 0
    stop_entries = False
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
            # Free its slot and its margin for an entry later in this same response
            # (cancel the stale LT short, re-enter it at today's price).
            pc = next((p for p in placements if str(p.get("order_id")) == oid), None)
            if pc:
                resting.discard((pc["symbol"], pc.get("side", "B")))
                if (pc["symbol"], pc.get("side", "B")) in rec["resting"]:
                    rec["resting"].remove((pc["symbol"], pc.get("side", "B")))
                if margin_free is not None and pc.get("margin"):
                    try:
                        margin_free += Decimal(str(pc["margin"]))
                    except ArithmeticError:
                        pass
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
                if oid is None:
                    act(i, "skipped", "already closed (its stop or target filled first)")
                    continue
                log({"event": "sold" if pos["qty"] > 0 else "covered", "symbol": sym, "qty": abs(pos["qty"]),
                     "order_id": oid, "reason": f"decider: {why}"})
                act(i, "exited", f"closed {abs(pos['qty'])} with a marketable limit", order_id=oid)
            else:
                log({"event": "would_exit", "symbol": sym, "reason": why})
                act(i, "would_exit", "shadow mode: nothing sent")
            continue
        if action == "control":
            import triggers as trg
            facts = trg.facts(day_pnl=realised + unrealised, realised=realised, orders=orders,
                              markets=(out.get("ctx") or {}).get("markets"), now=now)
            act(i, *control.apply(d, by="claude", day_pnl=realised + unrealised, broker=broker, now=now, facts=facts))
            if common.halted():
                stop_entries = True
            continue
        if action == "adjust":
            act(i, *adjust(broker, d, held, orders, placements, limits, realised + unrealised))
            continue
        if action != "enter":
            act(i, "ignored", f"unknown action {action!r}")
            continue

        side = parse_side(d.get("side"))
        if side is None:
            log({"event": "refused", "symbol": sym, "reason": f"side {d.get('side')!r} is not BUY or SELL"})
            act(i, "refused", f"side {d.get('side')!r} is not BUY or SELL")
            continue
        if stop_entries:
            act(i, "refused", f"halted: {common.halted()}")
            continue
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
        # Direction and sentiment are Claude's inputs. In "advisory" mode (the
        # default) they never refuse or resize Claude's trade; what they would
        # have said is recorded next to it, so the scorecard can judge both.
        against = regime.check("BUY" if side == "B" else "SELL", sym, direction)
        mood_refusal, mood_mult, mood_note = sentiment.check(
            "BUY" if side == "B" else "SELL", sym, mood, now, ((direction or {}).get("market") or {}).get("state"))
        advisory = [x for x in (against, mood_refusal, mood_note) if x]
        mode = (out.get("ctx") or {}).get("day_mode") or {}
        is_flip = str(d.get("setup") or "").endswith(daymode.FLIP_SUFFIX)
        if gates_enforced():
            problems += [x for x in (against, mood_refusal) if x]
        else:
            mood_mult = 1.0
            cat = realtime.catalyst(sym, side, now) if against else None
            if against and daymode.direction_enforced() and not is_flip and not cat:
                problems.append(against)
            if cat:
                advisory.append(f"direction gate waived: {cat}")
        if mode.get("halted"):
            problems.append(mode["halted"])
        if mode.get("flipped") and not is_flip:
            problems.append("flip mode: today only the listed *_flip setups may be entered")
        window = daymode.no_entry_window(now)
        if window:
            problems.append(window)
        if ("BUY" if side == "B" else "SELL") not in control.sides_allowed():
            problems.append(f"only {'/'.join(control.sides_allowed())} today (control)")
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
        try:                                            # Claude took a minute or two: price may have moved
            q = {**q, **{k: v for k, v in broker.quote(inst.token, inst.exch).items() if v}}
        except (ShoonyaError, Exception):               # noqa: BLE001
            pass
        try:
            plan, trigger, limit = build_plan(d, inst, q)
        except (strat.PlanError, KeyError, ValueError, ArithmeticError) as exc:
            log({"event": "refused", "symbol": sym, "side": side, "reason": str(exc)[:300], "decider_reason": why})
            act(i, "refused", str(exc)[:300])
            continue
        size = min(max(Decimal(str(d.get("size", 1) or 1)), Decimal("0.25")), Decimal(1))
        size = max(size * Decimal(str(mood_mult)) * control.size_mult(), Decimal("0.25"))
        lev = limits.max_leverage if limits.max_leverage > 0 else Decimal(1)
        sizing = risk_mod.size_position(plan, slot_cash * size / lev, limits)
        # Fit the order inside the margin Shoonya has free right now (with a 3% cushion),
        # so it is not rejected for a few hundred rupees.
        need = None
        if sizing.ok and margin_free is not None:
            need = margin_needed(broker, inst, side, sizing.qty, limit, trigger, lev)
            room = margin_free * Decimal("0.97")
            if need > room:
                ratio = room / need if need > 0 else Decimal(0)
                sizing = risk_mod.size_position(plan, slot_cash * size / lev * ratio, limits)
                need = margin_needed(broker, inst, side, sizing.qty, limit, trigger, lev) if sizing.ok else need
                if not sizing.ok or need > room:
                    log({"event": "refused", "symbol": sym, "side": side,
                         "reason": f"margin: needs {need:.0f}, Shoonya has {margin_free:.0f} free", "decider_reason": why})
                    act(i, "refused", f"margin: needs {need:.0f}, Shoonya has {margin_free:.0f} free")
                    continue
        if not sizing.ok:
            log({"event": "unsized", "symbol": sym, "side": side, "reason": sizing.limit_hit})
            act(i, "refused", f"sizing: {sizing.limit_hit}")
            continue
        floor_value = Decimal(common.setting("TRADER_MIN_POSITION_VALUE", "0") or 0)
        floor_net = Decimal(common.setting("TRADER_MIN_NET_TARGET", "0") or 0)
        small = []
        if floor_value and sizing.position_value < floor_value:
            small.append(f"position {sizing.position_value:.0f} is under the minimum {floor_value:.0f}")
        if floor_net and sizing.net_if_target_hits < floor_net:
            small.append(f"net at target {sizing.net_if_target_hits:.0f} is under the minimum {floor_net:.0f}: "
                         f"aim for a bigger move or a tighter stop")
        if small:
            log({"event": "refused", "symbol": sym, "side": side, "reason": "; ".join(small), "decider_reason": why})
            act(i, "refused", "; ".join(small))
            continue
        if extra is not None and len(rec["filled"]) + len(rec["resting"]) + placed >= base_trades:
            cost_of_one = sizing.loss_at_stop + sizing.round_trip_cost
            if cost_of_one > extra["room"]:
                log({"event": "refused", "symbol": sym, "reason": f"past {base_trades} trades: worst case "
                     f"{cost_of_one:.0f} > profit room {extra['room']}", "decider_reason": why})
                act(i, "refused", f"past the {base_trades}-trade limit, extra trades are paid from today's profit: "
                    f"worst case {cost_of_one:.0f} exceeds the room {extra['room']} "
                    f"(keeping {100 - extra['giveback_pct']}% of {extra['realised_net']})")
                continue
            extra["room"] -= cost_of_one
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
            "size_mult": str(size), "sentiment_note": mood_note, "advisory": advisory,
            "valid_minutes": valid_minutes(d), "setup": str(d.get("setup") or "")[:30] or None,
            "lessons": [str(x)[:8] for x in (d.get("lessons") or [])][:6] if isinstance(d.get("lessons"), list) else [],
            "sentiment_bias": (mood or {}).get("tilt", {}).get("bias"),
            "direction": ((direction or {}).get("market") or {}).get("state"),
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
        if margin_free is not None and need is not None:
            margin_free -= need
        log({"event": "placed", "order_id": oid, "margin": str(need) if need is not None else None, **record})
        act(i, "placed", summary + (f" · noted: {'; '.join(advisory)}" if advisory else ""), order_id=oid)
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
