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
    bot_tokens = {str(p["token"]) for p in placements
                  if book.get(str(p.get("order_id")), {}).get("filled", 0) > 0}

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

    # 1-3. every filled auto entry: protect, take target, trail
    for p in placements:
        o = book.get(str(p.get("order_id")))
        if not o or o["filled"] <= 0:
            continue
        side = p.get("side", "B")
        pos = held.get(str(p["token"]))
        if not pos or (pos["qty"] > 0) != (side == "B"):
            continue                                      # closed (or flipped)
        inst = inst_of(p)
        pid = str(p["order_id"])
        ex = exits.get(pid)
        if ex:
            eo = book.get(str(ex.get("order_id")))
            if eo and eo["pending"]:                       # exit not filled yet: re-price
                try:
                    broker.modify(eo, inst, qty=eo["qty"], price=broker.marketable(inst, opp(side)))
                except ShoonyaError as exc:
                    log({"event": "modify_failed", "order_id": eo["id"], "reason": str(exc)[:200]})
            continue

        want = min(o["filled"], abs(pos["qty"]))
        st = stops.get(pid)
        so = book.get(str(st["order_id"])) if st else None
        stop_px = D(st.get("trigger")) if st else D(p["stop"])
        if so is None or (so["dead"] and so["filled"] == 0):
            try:
                oid = broker.place(inst, side=opp(side), qty=want,
                                   price=stop_limit(stop_px, inst.tick, opp(side)),
                                   trigger=stop_px, remarks=f"stop/{p['symbol']}")
                log({"event": "stop_placed", "parent": pid, "order_id": oid, "symbol": p["symbol"],
                     "side": opp(side), "qty": want, "trigger": str(stop_px)})
                out["protected"] += 1
            except ShoonyaError as exc:
                log({"event": "stop_failed", "parent": pid, "symbol": p["symbol"], "reason": str(exc)[:300]})
                common.notify(f"UNPROTECTED {p['symbol']}",
                              f"Stop could not be placed: {exc}. Retrying every few seconds.", priority="urgent")
            continue
        if not so["pending"]:
            continue                                       # stop executed
        if so["qty"] != want:
            try:
                broker.modify(so, inst, qty=want, price=so["price"], trigger=so["trigger"])
                log({"event": "stop_resized", "order_id": so["id"], "qty": want})
            except ShoonyaError as exc:
                log({"event": "modify_failed", "order_id": so["id"], "reason": str(exc)[:200]})

        ltp = ltps.get(str(p["token"]))
        if ltp is None:
            continue
        target = D(adjusted[pid]["target"]) if pid in adjusted else D(p["target"])
        hit = ltp >= target if side == "B" else ltp <= target
        if hit:
            try:
                broker.cancel(so["id"])
            except ShoonyaError as exc:
                log({"event": "target_skip", "symbol": p["symbol"],
                     "reason": f"stop cancel failed ({exc}); it has probably executed"})
                continue
            try:
                eid = broker.place(inst, side=opp(side), qty=want, price=broker.marketable(inst, opp(side)),
                                   remarks=f"tgt/{p['symbol']}")
                log({"event": "target_exit", "parent": pid, "order_id": eid, "symbol": p["symbol"],
                     "qty": want, "ltp": str(ltp), "target": str(target)})
                out["sold"] += 1
            except ShoonyaError as exc:
                log({"event": "exit_failed", "symbol": p["symbol"], "reason": str(exc)[:300]})
                common.notify(f"UNPROTECTED {p['symbol']}",
                              f"Stop cancelled for target but the exit failed: {exc}", priority="urgent")
            continue
        entry = o["avg"] if o["avg"] > 0 else D(p["entry"])
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

    # 5. positions this system did not open, with no closing order of their
    #    own: guarded by the manual +/- thresholds, long or short
    pending = {(o["token"], o["side"]) for o in orders if o["pending"]}
    for tok, pos in held.items():
        side = "B" if pos["qty"] > 0 else "S"
        if tok in bot_tokens or (tok, opp(side)) in pending:
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
                                         metrics.pnl(snap["positions"], snap["ltps"]).items()}})
        key = (got.get("held"), got.get("resting"))
        if key != last or any(got.get(k) for k in ("protected", "cancelled", "sold", "moved")):
            log({"event": "state", **got})
            last = key
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(run())
