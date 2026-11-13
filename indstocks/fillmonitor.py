"""
Fill monitor: the always-on loop (every few seconds) that manages every
order and position between the scanner's placements and the close.

Shoonya has no bracket/OCO for intraday equity, so protection is built here:

  1. PROTECT   The moment an auto entry fills, a SELL SL-LMT stop is placed
               at the exchange for the filled quantity. It rests at NSE, so
               a dead server still leaves the position stopped. A partial
               fill that grows gets its stop resized.
  2. TARGET    When LTP reaches the plan target: cancel the stop, then sell
               with a marketable limit. If the cancel fails the stop has
               probably executed, so nothing is sold (no accidental short).
  3. TRAIL     Off by default: Claude moves stops and targets (adjust).
               TRADER_AUTO_TRAIL=true restores breakeven at +1R and a 1R trail.
  4. PRUNE     Resting entries are cancelled when fills reach the concurrent
               cap, when one more fill would breach cash x leverage, when
               price falls below the opening-range low (thesis dead), when
               they have rested past the TTL, and after the last-entry time.
  5. MANUAL    An intraday position with no sell order of its own (bought by
               hand) is sold at +TARGET_PCT / -STOP_PCT from its average.
  6. FLATTEN   At the session's flat-by time, and again five minutes later,
               the flatten runs from here. The flatten timers are a backup.
  7. SHORTS    A negative position (should never exist) is bought back.

No judgement here: every action executes a plan Claude made (its stop,
its target, its validity), or enforces the owner's limits and the close.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import flatten as flatten_mod                            # noqa: E402
import metrics                                           # noqa: E402
import profitlock                                        # noqa: E402
import session as sess                                   # noqa: E402
from market import to_tick                               # noqa: E402
from shoonya import D, Instrument, LoginRequired, Shoonya, ShoonyaError  # noqa: E402

JOB = "fill"
POLL_S = float(os.environ.get("TRADER_FILL_POLL_S", "4"))


def log(rec: dict) -> dict:
    return common.log(JOB, rec)


def S(key: str, default: str) -> Decimal:
    return Decimal(common.setting(key, default))


# ------------------------------------------------------------------ plans

def inst_of(p: dict) -> Instrument:
    return Instrument(p["symbol"], p["tsym"], str(p["token"]), p.get("exch") or "NSE",
                      D(p.get("tick"), "0.01"), 1)


def stop_limit(trigger: Decimal, tick: Decimal, side: str = "S") -> Decimal:
    """Limit for a stop order, far enough past the trigger to fill in a gap.
    side is the STOP's side: S (protecting a long) or B (protecting a short)."""
    band = trigger * S("TRADER_STOP_BAND_PCT", "0.5") / 100
    if side == "B":
        return to_tick(trigger + band, tick, mode="up")
    return to_tick(trigger - band, tick, mode="down")


def new_stop(entry: Decimal, stop: Decimal, ltp: Decimal, tick: Decimal,
             current: Decimal, side: str = "B"):
    """Where the stop should be now, or None if it should not move.
    side is the POSITION's side. Breakeven at +1R, then trail 1R behind
    price; a long's stop only rises, a short's only falls."""
    r = abs(entry - stop)
    if r <= 0 or ltp <= 0:
        return None
    gain_r = ((ltp - entry) if side == "B" else (entry - ltp)) / r
    be = S("TRADER_BREAKEVEN_R", "1.0")
    if gain_r < be:
        return None
    trail = S("TRADER_TRAIL_R", "1.0")
    if side == "B":
        cand = entry if gain_r < be + Decimal("0.5") else max(entry, ltp - r * trail)
        cand = to_tick(cand, tick, mode="down")
        return cand if cand > current else None
    cand = entry if gain_r < be + Decimal("0.5") else min(entry, ltp + r * trail)
    cand = to_tick(cand, tick, mode="up")
    return cand if (current <= 0 or cand < current) else None


def manual_decision(avg: Decimal, ltp: Decimal, side: str = "B"):
    if avg <= 0 or ltp <= 0:
        return None
    move = (ltp - avg) / avg * 100 * (1 if side == "B" else -1)
    if move >= S("TRADER_MANUAL_TARGET_PCT", "1.0"):
        return f"in profit {move:.2f}%"
    if move <= -S("TRADER_MANUAL_STOP_PCT", "0.5"):
        return f"in loss {-move:.2f}%"
    return None


def latest_by(events: list, key: str) -> dict:
    out = {}
    for e in events:
        if e.get(key):
            out[str(e[key])] = e
    return out


def opp(side: str) -> str:
    return "S" if side == "B" else "B"


OUR_PREFIX = ("auto/", "stop/", "tgt/", "man/", "flat/", "exit/", "chat/", "ctl/", "brk/", "lock/")


def is_ours(o: dict, ours: set) -> bool:
    return o["id"] in ours or str(o.get("remarks") or "").startswith(OUR_PREFIX)


def is_stop(o: dict) -> bool:
    return o.get("type") == "SL-LMT" or str(o.get("remarks") or "").startswith("stop/")


def open_qty(placements: list, orders: list, entry_ids: set) -> dict:
    """How much of each filled entry is still open, FIFO per symbol and side:
    every filled closing order (stop, target, Claude's exit, a hand exit)
    consumes the oldest entry first. A trade that has been closed is never
    protected again, even when a later trade opens the same stock."""
    closed: dict = {}
    for o in orders:
        if o["filled"] > 0 and o["id"] not in entry_ids:
            k = (str(o["token"]), opp(o["side"]))         # a SELL closes longs
            closed[k] = closed.get(k, 0) + o["filled"]
    out = {}
    fills = {o["id"]: o["filled"] for o in orders}
    for p in placements:
        f = fills.get(str(p.get("order_id")), 0)
        if f <= 0:
            continue
        k = (str(p["token"]), p.get("side", "B"))
        use = min(f, closed.get(k, 0))
        closed[k] = closed.get(k, 0) - use
        out[str(p["order_id"])] = f - use
    return out


# ------------------------------------------------------------------ tick

def tick(broker, now: datetime, state: dict) -> dict:
    session = sess.session_for(now)
    if session is None or now < session.open - timedelta(minutes=5) or now >= session.close:
        return {"idle": True}

    # 6. flatten passes
    for i, at in enumerate((session.flat_by, session.flat_by + timedelta(minutes=5))):
        key = f"flatten{i}:{session.day}"
        if now >= at and key not in state:
            state[key] = True
            log({"event": "flatten_start", "pass": i + 1})
            flatten_mod.run(broker, now=now)
    if now >= session.flat_by:
        return {"flattening": True}

    out = {"protected": 0, "cancelled": 0, "sold": 0, "moved": 0}
    orders = broker.orders()
    book = {o["id"]: o for o in orders}
    positions = broker.positions()
    held = {p["token"]: p for p in positions if p["qty"] != 0}

    placements = common.events("placed")
    stops = latest_by(common.events("stop_placed"), "parent")
    exits = latest_by(common.events("target_exit"), "parent")
    adjusted = latest_by([r for r in common.events("plan_adjusted") if r.get("target")], "parent")

    resting = [(p, book[str(p["order_id"])]) for p in placements
               if str(p.get("order_id")) in book and book[str(p["order_id"])]["pending"]
               and book[str(p["order_id"])]["filled"] == 0]
    tokens = list(held) + [str(p["token"]) for p, _ in resting]
    for sym in common.symbols():                     # the dashboard's ticker
        try:
            tokens.append(broker.instrument(sym).token)
        except (KeyError, ShoonyaError):
            pass
    ltps = broker.ltps(list(dict.fromkeys(tokens))) if tokens else {}
    try:
        import realtime
        names = {}
        for sym in common.symbols():
            try:
                names[broker.instrument(sym).token] = sym
            except (KeyError, ShoonyaError):
                pass
        realtime.watch_prices({t_: v for t_, v in ltps.items() if t_ in names}, names, now, state)
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"shock watch: {exc}"[:200]})

    ours_all = common.our_order_ids()
    entry_ids = {str(p.get("order_id")) for p in placements}
    remaining = open_qty(placements, orders, entry_ids)

    def closing(tok: str, side: str) -> list:
        """Pending orders that reduce the position on tok (side = position side)."""
        return [o for o in orders if o["pending"] and str(o["token"]) == tok and o["side"] == opp(side)
                and o["id"] not in entry_ids]

    def unfilled(o: dict) -> int:
        return max(0, o["qty"] - o["filled"])

    # The process runs for days: the lock memory belongs to one trading day.
    if state.get("lock_day") != session.day:
        state["lock_day"] = session.day
        state["lock_best"] = profitlock.load()
    lock_best = state["lock_best"]
    lock_dirty = [False]

    # 0. exits in flight (target, Claude's exit, breach exit): chase every tick
    #    until filled. A limit that price ran away from is no exit at all.
    #    An exit that has started (stop being cancelled, exit not yet placed)
    #    counts too, for 20 seconds, unless it was aborted.
    exiting = set()
    starts = {}
    for r in common.events("exit_started") + common.events("exit_aborted"):
        try:
            t_ = datetime.fromisoformat(r["ts"])
        except (KeyError, ValueError):
            continue
        tok_ = str(r.get("token"))
        if r["event"] == "exit_started" and timedelta(0) <= now - t_ <= timedelta(seconds=20):
            starts[tok_] = t_
        elif r["event"] == "exit_aborted" and tok_ in starts and t_ >= starts[tok_]:
            starts.pop(tok_)
    exiting.update(starts)
    for tok, pos in held.items():
        side = "B" if pos["qty"] > 0 else "S"
        for o in closing(tok, side):
            if is_stop(o) or not is_ours(o, ours_all):
                continue
            exiting.add(tok)
            try:
                inst = broker.instrument_for(pos)
                px = broker.marketable(inst, opp(side))
                if px != o["price"]:
                    broker.modify(o, inst, qty=o["qty"], price=px)
                    log({"event": "exit_chased", "symbol": pos["symbol"], "order_id": o["id"],
                         "from": str(o["price"]), "to": str(px)})
            except ShoonyaError as exc:
                log({"event": "modify_failed", "order_id": o["id"], "reason": str(exc)[:200]})

    def breach_exit(p: dict, inst, side: str, qty: int, why: str) -> None:
        """Price is already through the stop: an SL order would be rejected
        or sit untriggered. Close now with a marketable limit."""
        try:
            eid = broker.place(inst, side=opp(side), qty=qty, price=broker.marketable(inst, opp(side)),
                               remarks=f"brk/{p['symbol']}")
            log({"event": "stop_breached_exit", "parent": str(p["order_id"]), "order_id": eid,
                 "symbol": p["symbol"], "qty": qty, "reason": why})
            common.notify(f"Stop breached: {p['symbol']}", why, priority="high")
            exiting.add(str(p["token"]))
            out["sold"] += 1
        except ShoonyaError as exc:
            log({"event": "exit_failed", "symbol": p["symbol"], "reason": str(exc)[:300]})
            common.notify(f"UNPROTECTED {p['symbol']}", f"Breach exit failed: {exc}", priority="urgent")

    stop_rejects: dict = {}
    for st in common.events("stop_placed"):
        so_ = book.get(str(st.get("order_id")))
        if so_ and so_["status"] in ("REJECTED", "REJECT"):
            stop_rejects[str(st.get("parent"))] = stop_rejects.get(str(st.get("parent")), 0) + 1

    # 0b. the day lock: the whole day's net after charges, best kept on disk
    if profitlock.day_enabled() and not lock_best.get("_day_locked"):
        try:
            day_net = metrics.pnl(positions, ltps, orders)["net"]
        except Exception:                               # noqa: BLE001
            day_net = None
        if day_net is not None:
            best_d, why_d = profitlock.day_check(lock_best.get("_day"), day_net)
            if best_d != lock_best.get("_day"):
                lock_best["_day"] = best_d
                lock_dirty[0] = True
            if why_d:
                lock_best["_day_locked"] = Decimal(1)
                lock_dirty[0] = True
                import control
                st = control.today()
                st["halt"] = {"by": "owner", "reason": why_d, "ts": now.isoformat(timespec="seconds"),
                              "trigger": "day_lock"}
                control.write_json(control.day_file(), st)
                log({"event": "day_locked", "net": str(round(day_net, 2)), "best": str(round(best_d, 2)),
                     "reason": why_d})
                common.notify("Day locked: profit protected", why_d + ". Closing all positions; no new entries today. "
                              "Resume from the dashboard if you want.", priority="urgent")
                for o in orders:                         # resting entries first
                    if o["pending"] and o["id"] in entry_ids:
                        try:
                            broker.cancel(o["id"])
                        except ShoonyaError:
                            pass
                for pos in held.values():
                    try:
                        flatten_mod.close_position(broker, pos, remarks=f"lock/{pos['symbol']}")
                        exiting.add(str(pos["token"]))
                    except ShoonyaError as exc:
                        log({"event": "exit_failed", "symbol": pos["symbol"], "reason": str(exc)[:300]})

    # 1-3. every filled auto entry still open: protect, take target or lock profit, trail
    for p in placements:
        pid = str(p.get("order_id"))
        o = book.get(pid)
        if not o or o["filled"] <= 0 or remaining.get(pid, 0) <= 0:
            continue                                      # never filled, or already closed
        side = p.get("side", "B")
        tok = str(p["token"])
        pos = held.get(tok)
        if not pos or (pos["qty"] > 0) != (side == "B"):
            continue                                      # closed (or flipped)
        if tok in exiting:
            continue                                      # an exit is working; it is chased above
        inst = inst_of(p)
        ltp = ltps.get(tok)
        st = stops.get(pid)
        so = book.get(str(st["order_id"])) if st else None
        stop_px = D(st.get("trigger")) if st else D(p["stop"])
        if so is not None and so["filled"] > 0 and not so["pending"]:
            continue                                      # stop executed
        others = sum(unfilled(x) for x in closing(tok, side) if not (so and x["id"] == so["id"]))
        room = abs(pos["qty"]) - others
        want = min(remaining[pid], room)

        if so is None or (so["dead"] and so["filled"] == 0):
            if want <= 0:
                continue                                  # the position is already fully covered
            breached = ltp is not None and (ltp <= stop_px if side == "B" else ltp >= stop_px)
            if breached or stop_rejects.get(pid, 0) >= 2:
                breach_exit(p, inst, side, want,
                            f"price {ltp} is through the stop {stop_px}" if breached
                            else f"the exchange rejected the stop {stop_rejects[pid]} times")
                continue
            try:
                oid = broker.place(inst, side=opp(side), qty=want,
                                   price=stop_limit(stop_px, inst.tick, opp(side)),
                                   trigger=stop_px, remarks=f"stop/{p['symbol']}")
                log({"event": "stop_placed", "parent": pid, "order_id": oid, "symbol": p["symbol"],
                     "side": opp(side), "qty": want, "trigger": str(stop_px)})
                orders.append({"id": str(oid), "token": tok, "side": opp(side), "pending": True, "dead": False,
                               "qty": want, "filled": 0, "type": "SL-LMT", "remarks": f"stop/{p['symbol']}",
                               "price": stop_limit(stop_px, inst.tick, opp(side)), "trigger": stop_px,
                               "status": "OPEN", "symbol": p["symbol"]})
                out["protected"] += 1
            except ShoonyaError as exc:
                log({"event": "stop_failed", "parent": pid, "symbol": p["symbol"], "reason": str(exc)[:300]})
                common.notify(f"UNPROTECTED {p['symbol']}",
                              f"Stop could not be placed: {exc}. Retrying every few seconds.", priority="urgent")
            continue
        if not so["pending"]:
            continue
        want_total = so["filled"] + max(want, 0)
        if want <= 0:
            try:                                          # over-covered: this stop is surplus
                broker.cancel(so["id"])
                log({"event": "stop_cancelled", "order_id": so["id"], "symbol": p["symbol"],
                     "reason": "other closing orders already cover the position"})
            except ShoonyaError as exc:
                log({"event": "cancel_failed", "order_id": so["id"], "reason": str(exc)[:200]})
            continue
        if so["qty"] != want_total:
            try:
                broker.modify(so, inst, qty=want_total, price=so["price"], trigger=so["trigger"])
                log({"event": "stop_resized", "order_id": so["id"], "qty": want_total})
            except ShoonyaError as exc:
                log({"event": "modify_failed", "order_id": so["id"], "reason": str(exc)[:200]})

        if ltp is None:
            continue
        # 2a. a stop that triggered but whose limit price ran away (a fast gap):
        #     re-price it to marketable so it actually fills.
        trig = so["trigger"]
        ran = (ltp <= trig and ltp < so["price"]) if side == "B" else (ltp >= trig and ltp > so["price"])
        if trig > 0 and ran:
            try:
                px = broker.marketable(inst, opp(side))
                px = min(px, trig) if side == "B" else max(px, trig)
                broker.modify(so, inst, qty=so["qty"], price=px, trigger=trig)
                log({"event": "stop_chased", "order_id": so["id"], "symbol": p["symbol"],
                     "trigger": str(trig), "from": str(so["price"]), "to": str(px), "ltp": str(ltp)})
                common.notify(f"Stop chased: {p['symbol']}", f"triggered at {trig}, price {ltp}", priority="high")
            except ShoonyaError as exc:
                log({"event": "modify_failed", "order_id": so["id"], "reason": str(exc)[:200]})
            continue
        target = D(adjusted[pid]["target"]) if pid in adjusted else D(p["target"])
        hit = ltp >= target if side == "B" else ltp <= target
        entry = o["avg"] if o["avg"] > 0 else D(p["entry"])
        open_q = so["qty"] - so["filled"]
        lock_why = None
        if profitlock.enabled() and open_q > 0:
            net = profitlock.net_open(side, entry, open_q, ltp)
            try:
                risk_rs = D(p.get("risk")) * open_q / max(int(p.get("qty") or open_q), 1)
            except (ArithmeticError, TypeError, ValueError):
                risk_rs = None
            best, armed, lock_why = profitlock.check(lock_best.get(pid), net, profitlock.arm_for(risk_rs))
            if best is not None and best != lock_best.get(pid):
                if pid not in lock_best:
                    log({"event": "profit_lock_armed", "parent": pid, "symbol": p["symbol"], "net": str(round(net, 2)),
                         "floor": str(round(profitlock.floor(best), 2))})
                lock_best[pid] = best
                lock_dirty[0] = True
            if armed and not lock_why:
                # protect at the exchange too: stop to breakeven after charges
                be = profitlock.breakeven_price(side, entry, open_q, inst.tick)
                better = be > so["trigger"] if side == "B" else (so["trigger"] <= 0 or be < so["trigger"])
                room = (ltp - be) if side == "B" else (be - ltp)
                if better and room >= inst.tick * 2:
                    try:
                        broker.modify(so, inst, qty=so["qty"], price=stop_limit(be, inst.tick, opp(side)), trigger=be)
                        log({"event": "stop_moved", "parent": pid, "order_id": so["id"], "symbol": p["symbol"],
                             "from": str(so["trigger"]), "to": str(be), "ltp": str(ltp),
                             "reason": "profit lock: breakeven after charges"})
                        out["moved"] += 1
                    except ShoonyaError as exc:
                        log({"event": "modify_failed", "order_id": so["id"], "reason": str(exc)[:200]})
        if hit or lock_why:
            # Book it now: cancel the stop (and any unfilled entry), marketable exit, chased until filled.
            if o["pending"]:
                try:
                    broker.cancel(o["id"])
                except ShoonyaError:
                    pass
            try:
                broker.cancel(so["id"])
            except ShoonyaError as exc:
                log({"event": "target_skip", "symbol": p["symbol"],
                     "reason": f"stop cancel failed ({exc}); it has probably executed"})
                continue
            tag = "tgt" if hit else "lock"
            try:
                eid = broker.place(inst, side=opp(side), qty=open_q, price=broker.marketable(inst, opp(side)),
                                   remarks=f"{tag}/{p['symbol']}")
                log({"event": "target_exit", "parent": pid, "order_id": eid, "symbol": p["symbol"],
                     "qty": open_q, "ltp": str(ltp), "target": str(target),
                     "reason": "target hit" if hit else lock_why})
                if lock_why:
                    common.notify(f"Profit booked: {p['symbol']}", lock_why, priority="high")
                exiting.add(tok)
                out["sold"] += 1
            except ShoonyaError as exc:
                log({"event": "exit_failed", "symbol": p["symbol"], "reason": str(exc)[:300]})
                common.notify(f"UNPROTECTED {p['symbol']}",
                              f"Stop cancelled for the exit but the exit failed: {exc}. The stop is re-placed next tick.",
                              priority="urgent")
            continue
        if common.setting("TRADER_AUTO_TRAIL", "false").lower() not in ("1", "true", "yes"):
            continue                                    # Claude moves stops (adjust); nothing trails on its own
        moved = new_stop(entry, D(p["stop"]), ltp, inst.tick, so["trigger"], side)
        if moved is not None:
            try:
                broker.modify(so, inst, qty=so["qty"], price=stop_limit(moved, inst.tick, opp(side)), trigger=moved)
                be = to_tick(entry, inst.tick, mode="down" if side == "B" else "up")
                log({"event": "stop_moved", "parent": pid, "order_id": so["id"], "symbol": p["symbol"],
                     "from": str(so["trigger"]), "to": str(moved), "ltp": str(ltp),
                     "reason": "breakeven" if moved == be else "trailing"})
                out["moved"] += 1
            except ShoonyaError as exc:
                log({"event": "modify_failed", "order_id": so["id"], "reason": str(exc)[:200]})

    if lock_dirty[0]:
        profitlock.save(lock_best)

    # 3a. our orders the broker rejected: log the reason once (margin, price band...)
    told = {str(r.get("order_id")) for r in common.events("order_rejected")}
    for o in orders:
        if o["id"] in ours_all and o["status"] in ("REJECTED", "REJECT") and o["id"] not in told:
            log({"event": "order_rejected", "order_id": o["id"], "symbol": o["symbol"], "side": o["side"],
                 "qty": o["qty"], "reason": str(o.get("reason") or "")[:300]})
            common.notify(f"Order rejected: {o['symbol']}", str(o.get("reason") or "")[:200], priority="default")

    # 3b. a stop whose position is gone (closed by hand, or by Claude's exit)
    #     must not sit at the exchange: if it triggered it would open a new
    #     position the other way.
    for st in common.events("stop_placed"):
        so = book.get(str(st.get("order_id")))
        if not so or not so["pending"]:
            continue
        pos = held.get(str(so["token"]))
        protects = pos is not None and ((pos["qty"] > 0 and so["side"] == "S") or (pos["qty"] < 0 and so["side"] == "B"))
        if protects:
            continue
        try:
            broker.cancel(so["id"])
            log({"event": "orphan_stop_cancelled", "symbol": st.get("symbol"), "order_id": so["id"],
                 "reason": "its position is closed"})
        except ShoonyaError as exc:
            log({"event": "cancel_failed", "order_id": so["id"], "reason": str(exc)[:200]})

    # 4. prune resting entries
    limits = common.limits()
    cancel_all = None
    if not session.accepts_new_entries(now):
        cancel_all = "past the last-entry time"
    elif len(held) >= limits.max_concurrent_positions:
        cancel_all = f"{len(held)} position(s) open, cap {limits.max_concurrent_positions}"
    exposure = sum((abs(p["avg"] * p["qty"]) for p in held.values()), Decimal(0))
    ceiling = None
    if exposure > 0:
        try:
            ceiling = broker.cash() * limits.max_leverage
        except ShoonyaError:
            ceiling = None
    ttl = int(common.setting("TRADER_RESTING_TTL_MIN", "30"))
    for p, o in resting:
        why = cancel_all
        side = p.get("side", "B")
        ltp = ltps.get(str(p["token"]))
        if not why and str(p["token"]) in held:
            why = f"already in {p['symbol']}: the other side's entry is no longer wanted"
        if not why and ceiling is not None and exposure + D(p["entry"]) * o["qty"] > ceiling:
            why = f"one more fill would hold {exposure + D(p['entry']) * o['qty']:.0f} > {ceiling:.0f}"
        if not why and ltp is not None:
            if side == "B" and ltp <= D(p["stop"]):
                why = f"price {ltp} is at or below the planned stop {p['stop']}: the long idea is dead"
            if side == "S" and ltp >= D(p["stop"]):
                why = f"price {ltp} is at or above the planned stop {p['stop']}: the short idea is dead"
        life = int(p.get("valid_minutes") or ttl)
        if not why and life > 0:
            try:
                age = (now - datetime.fromisoformat(p["ts"])).total_seconds() / 60
                if age >= life:
                    why = (f"rested {age:.0f} min: Claude gave it {life} min" if p.get("valid_minutes")
                           else f"rested {age:.0f} min without a fill (ttl {ttl})")
            except (KeyError, ValueError):
                pass
        if not why:
            continue
        try:
            broker.cancel(o["id"])
            log({"event": "entry_cancelled", "symbol": p["symbol"], "order_id": o["id"], "reason": why})
            out["cancelled"] += 1
        except ShoonyaError as exc:
            log({"event": "cancel_failed", "order_id": o["id"], "reason": str(exc)[:200]})

    # 5. positions this system did not open. Adopted by Claude (it set a stop
    #    and target): exit at Claude's target. Otherwise, with no closing
    #    order of their own: guarded by the manual +/- thresholds.
    pending = {(o["token"], o["side"]) for o in orders if o["pending"]}
    managed = {(str(p["token"]), p.get("side", "B")) for p in placements
               if remaining.get(str(p.get("order_id")), 0) > 0}
    for tok, pos in held.items():
        side = "B" if pos["qty"] > 0 else "S"
        if (tok, side) in managed or tok in exiting:
            continue                    # the bot's own trade (above), or an exit already working
        adopt = adjusted.get(f"manual:{tok}")
        so = next((o for o in orders if o["pending"] and str(o["token"]) == tok and o["side"] == opp(side)
                   and is_stop(o)), None)
        if adopt and so is not None:
            ltp = ltps.get(tok) or pos["ltp"]
            tgt = D(adopt["target"])
            if (side == "B" and ltp >= tgt) or (side == "S" and ltp <= tgt):
                inst = broker.instrument_for(pos)
                try:
                    broker.cancel(so["id"])
                except ShoonyaError:
                    continue                            # the stop probably executed: do not double-exit
                try:
                    eid = broker.place(inst, side=opp(side), qty=abs(pos["qty"]),
                                       price=broker.marketable(inst, opp(side)), remarks=f"tgt/{pos['symbol']}")
                    log({"event": "target_exit", "parent": f"manual:{tok}", "order_id": eid, "symbol": pos["symbol"],
                         "qty": abs(pos["qty"]), "ltp": str(ltp), "target": str(tgt)})
                    out["sold"] += 1
                except ShoonyaError as exc:
                    log({"event": "exit_failed", "symbol": pos["symbol"], "reason": str(exc)[:300]})
                    common.notify(f"UNPROTECTED {pos['symbol']}", f"Target exit failed: {exc}", priority="urgent")
            continue
        if (tok, opp(side)) in pending:
            continue
        ltp = ltps.get(tok) or pos["ltp"]
        why = manual_decision(pos["avg"], ltp, side)
        if not why:
            continue
        inst = broker.instrument_for(pos)
        try:
            oid = broker.place(inst, side=opp(side), qty=abs(pos["qty"]),
                               price=broker.marketable(inst, opp(side)), remarks=f"man/{pos['symbol']}")
            log({"event": "sold" if side == "B" else "covered", "symbol": pos["symbol"], "qty": abs(pos["qty"]),
                 "order_id": oid, "avg": str(pos["avg"]), "ltp": str(ltp), "reason": f"manual position {why}"})
            out["sold"] += 1
        except ShoonyaError as exc:
            log({"event": "sell_failed", "symbol": pos["symbol"], "reason": str(exc)[:200]})

    out["held"] = len(held)
    out["resting"] = len(resting) - out["cancelled"]
    # One read of the broker per tick, shared: the dashboard renders from
    # this instead of polling Shoonya itself.
    state["snap"] = {"positions": positions, "orders": orders, "ltps": ltps}
    return out


def relogin(now: datetime, state: dict, why: str) -> None:
    """No Shoonya session during market hours: nothing new can be managed
    (exchange stops still rest). Alert, and start the auto-login at most every
    10 minutes; it refuses by itself to retry a rejected password."""
    s = sess.session_for(now)
    if s is None or not (s.open - timedelta(minutes=5) <= now < s.close):
        return
    if time.monotonic() - state.get("relogin_at", -1e9) < 600:
        return
    state["relogin_at"] = time.monotonic()
    common.notify("Shoonya logged out", f"{why}. Trying auto-login; stops already at the exchange still work.",
                  priority="urgent")
    try:
        import subprocess
        subprocess.run(["systemctl", "start", "--no-block", "login.service"], timeout=10, capture_output=True)
        log({"event": "relogin_started", "reason": why[:200]})
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"relogin: {exc}"[:200]})


def run() -> int:
    broker = Shoonya()
    state: dict = {}
    log({"event": "fillmonitor_start", "poll_s": POLL_S})
    last = None
    warned_login = None
    while True:
        now = sess.now()
        if common.halted() and not state.get("halt_logged"):
            # A halt stops new entries (the scanner) but NEVER protection:
            # stops, targets and the flatten keep running.
            state["halt_logged"] = True
        try:
            got = tick(broker, now, state)
        except LoginRequired as exc:
            if warned_login != now.date():
                log({"event": "login_required", "reason": str(exc)})
                warned_login = now.date()
            relogin(now, state, str(exc))
            time.sleep(30)
            continue
        except ShoonyaError as exc:
            log({"event": "error", "reason": str(exc)[:300]})
            time.sleep(POLL_S * 2)
            continue
        except Exception as exc:                        # noqa: BLE001
            log({"event": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]})
            time.sleep(POLL_S * 3)
            continue
        if got.get("idle"):
            time.sleep(30)
            continue
        snap = state.pop("snap", None)
        if snap is not None:
            if time.monotonic() - state.get("cash_at", -1e9) > 60:
                try:
                    state["cash"] = broker.cash()
                    state["cash_at"] = time.monotonic()
                except ShoonyaError:
                    pass
            metrics.write_snapshot({**snap, "cash": state.get("cash")})
            minute = now.strftime("%H:%M")
            if state.get("pnl_minute") != minute:
                state["pnl_minute"] = minute
                log({"event": "pnl", **{k: str(v) for k, v in
                                         metrics.pnl(snap["positions"], snap["ltps"], snap["orders"]).items()}})
        key = (got.get("held"), got.get("resting"))
        if key != last or any(got.get(k) for k in ("protected", "cancelled", "sold", "moved")):
            log({"event": "state", **got})
            last = key
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(run())
