"""
Autonomous runner: scan, size, check, place, and stop when the day is done.

Runs without a human in the loop. Everything that protects the account is
therefore IN THIS FILE, not in a person watching, so each guard is explicit
and each one halts the session rather than warning:

  LIVE flag        off by default; a deliberate switch, not a default state
  kill switch      a file on disk stops the run before any order is sent
  daily loss cap   realised losses past the cap end the day
  trade cap        a hard count, not a soft preference
  loss streak      consecutive stops halt the day; a strategy that is wrong
                   today tends to stay wrong today
  cost gate        a trade that cannot clear its own charges is not taken
  flat-by          nothing is left open into the close

Loud by design: every decision, placement and refusal is logged. An
autonomous system that fails quietly is worse than one that does not run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import risk as risk_mod
import session as sess
import strategy as strat
import ticket as tk_mod
from bars import Bars, opening_range, scrip_code
from client import Client
from instruments import Instruments
from smartorder import SmartOrderError, build_trigger_for_instrument

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
KILL_SWITCH = Path(os.environ.get("TRADER_KILL_SWITCH", "/etc/indstocks.halt"))

# Smallest order worth placing. Below this the Rs 10 brokerage a side plus
# statutory charges eat the whole move. Rs 10k at a 0.5% target is Rs 50
# gross against ~Rs 24 of charges: thin, but a trade.
MIN_ORDER_VALUE = Decimal(os.environ.get("TRADER_MIN_ORDER_VALUE", "10000"))

# Off unless explicitly switched on. A misread config must fail closed.
LIVE = os.environ.get("TRADER_LIVE", "").strip().lower() in ("1", "true", "yes")

MAX_CONSECUTIVE_LOSSES = int(os.environ.get("TRADER_MAX_LOSS_STREAK", "2"))


def log(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": sess.now().isoformat(), "live": LIVE, **record}
    with (STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(json.dumps(record, default=str), flush=True)


DEAD_AT_BROKER = {"CANCELLED", "REJECTED", "FAILED", "EXPIRED", "ABORTED"}


def reconcile_with_broker(client, placements: dict) -> dict:
    """Drop placements the broker says never happened.

    The log records what was ATTEMPTED. A parent order that the account
    owner cancelled by hand, or that the exchange rejected, with nothing
    traded, is not a trade taken and not an open symbol -- yet the log
    alone would count it as both, consume the day's budget on it, and
    refuse to re-place the symbol for the rest of the session.

    Reads the order book once. If that fails, the log stands: a
    conservative error.
    """
    try:
        rows = (client.get("/order-book") or {}).get("data") or []
    except Exception:                                   # noqa: BLE001
        return {**placements, "resting_symbols": ()}
    if not isinstance(rows, list):
        return {**placements, "resting_symbols": ()}

    status_by_id = {}
    for r in rows:
        oid = str(r.get("id") or r.get("order_id") or "")
        if oid:
            try:
                traded = int(r.get("traded_qty") or 0)
            except (TypeError, ValueError):
                traded = 0
            status_by_id[oid] = (str(r.get("status", "")).upper(), traded)

    # Resting orders cost nothing and may never fire. Only FILLS are trades,
    # only fills hold capital, only fills count toward the concurrent cap.
    # Resting symbols are remembered so they are not placed twice, and the
    # fill monitor cancels them once the cap is reached.
    resting, filled, risk = [], [], Decimal(0)
    for rec in placements.get("records", []):
        oid = str(rec.get("parent_order_id") or "")
        status, traded = status_by_id.get(oid, ("", 0))
        sym = rec.get("symbol")
        if status in DEAD_AT_BROKER and traded == 0:
            log({"event": "reconciled_out", "symbol": sym,
                 "parent_order_id": oid, "broker_status": status,
                 "reason": "dead at the broker with nothing traded"})
            continue
        if traded > 0:
            if sym:
                filled.append(sym)
            try:
                risk += Decimal(str(rec.get("risk") or 0))
            except Exception:                           # noqa: BLE001
                pass
        elif sym:
            resting.append(sym)
    return {"trades_taken": len(filled), "risk_committed": risk,
            "open_symbols": tuple(filled), "resting_symbols": tuple(resting)}


def todays_placements() -> dict:
    """What THIS runner already placed today, from its own log.

    Each timer firing is a fresh process. Without reading its own history a
    run starts from zero, re-places the symbols the previous run placed, and
    the per-day trade limit is enforced five times a day instead of once.

    Read from our log rather than ticket.py's: that file belongs to the
    interactive path and this runner never writes it, which is exactly how
    the duplicates happened.
    """
    path = STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl"
    symbols, risk, placed, records = [], Decimal(0), 0, []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") != "placed":
                continue
            placed += 1
            records.append(rec)
            sym = rec.get("symbol")
            if sym:
                symbols.append(sym)
            try:
                risk += Decimal(str(rec.get("risk") or 0))
            except Exception:                           # noqa: BLE001
                pass
    except OSError:
        pass
    return {"trades_taken": placed, "risk_committed": risk,
            "open_symbols": tuple(symbols), "records": records}


def halted() -> str | None:
    """A file anyone can touch to stop the next run before it trades."""
    if KILL_SWITCH.exists():
        try:
            reason = KILL_SWITCH.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        return reason or "kill switch present"
    return None


def watchlist() -> list:
    path = os.environ.get("TRADER_WATCHLIST", os.path.join(HERE, "watchlist.txt"))
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line.upper())
    return out


def available_cash(client) -> Decimal | None:
    try:
        data = (client.get("/funds") or {}).get("data") or {}
    except Exception:                                   # noqa: BLE001
        return None
    bal = data.get("detailed_avl_balance") or {}
    for key in ("eq_mis", "eq_cnc"):
        if bal.get(key) is not None:
            return Decimal(str(bal[key]))
    return None


def day_pnl(client) -> tuple:
    """(realised, unrealised). Both matter.

    The daily loss limit previously read realised only, so an account down
    several thousand on open positions still looked flat and kept trading.
    An unrealised loss is the only kind you have while a position is open.
    """
    try:
        data = (client.get("/funds") or {}).get("data") or {}
        return (Decimal(str(data.get("realized_pnl") or 0)),
                Decimal(str(data.get("unrealized_pnl") or 0)))
    except Exception:                                   # noqa: BLE001
        return Decimal(0), Decimal(0)


def held_security_ids(client) -> set:
    """security_ids of every open intraday position, manual ones included."""
    out = set()
    try:
        data = (client.get("/portfolio/positions") or {}).get("data") or {}
    except Exception:                                   # noqa: BLE001
        return out
    rows = data if isinstance(data, list) else (
        data.get("positions") or data.get("net_positions") or [])
    for r in rows or []:
        try:
            qty = int(r.get("net_qty", r.get("quantity", r.get("qty", 0))) or 0)
        except (TypeError, ValueError):
            continue
        if qty > 0 and str(r.get("product", "")).upper() in ("INTRADAY", "MIS", ""):
            out.add(str(r.get("security_id")))
    return out


def held_exposure(client) -> Decimal:
    """Position value already held, for the leverage cap."""
    try:
        data = (client.get("/portfolio/positions") or {}).get("data") or {}
    except Exception:                                   # noqa: BLE001
        return Decimal(0)
    rows = data if isinstance(data, list) else (
        data.get("positions") or data.get("net_positions") or [])
    total = Decimal(0)
    for r in rows or []:
        try:
            qty = abs(int(r.get("net_qty", r.get("quantity", r.get("qty", 0)))))
            price = Decimal(str(r.get("avg_price") or r.get("buy_avg_price") or 0))
        except (TypeError, ValueError):
            continue
        total += price * qty
    return total


def limits_from_env() -> risk_mod.Limits:
    return risk_mod.Limits(
        max_loss_per_trade=Decimal(os.environ.get("TRADER_MAX_LOSS", "500")),
        daily_loss_limit=Decimal(os.environ.get("TRADER_DAILY_LOSS", "1500")),
        max_trades_per_day=int(os.environ.get("TRADER_MAX_TRADES", "3")),
        max_leverage=Decimal(os.environ.get("TRADER_MAX_LEVERAGE", "1.0")),
        max_concurrent_positions=int(os.environ.get("TRADER_MAX_CONCURRENT", "2")),
        # Gain at target must clear charges by this multiple. 1.0 means
        # "any net-positive trade"; below 1.0 is refused in risk.py.
        min_reward_to_cost=Decimal(os.environ.get("TRADER_MIN_REWARD_TO_COST", "2")),
        min_reward_to_risk=Decimal(os.environ.get("TRADER_MIN_RR", "1.5")))


def run() -> int:
    now = sess.now()
    session = sess.session_for(now)

    reason = halted()
    if reason:
        log({"event": "halted", "reason": reason})
        return 0

    if session is None:
        log({"event": "skipped", "reason": "not an NSE trading day"})
        return 0
    if not session.accepts_new_entries(now):
        log({"event": "skipped", "reason": f"phase {session.phase(now)}"})
        return 0

    client = Client()
    limits = limits_from_env()

    cash = available_cash(client)
    if cash is None:
        log({"event": "abort", "reason": "could not read funds"})
        return 1

    # Losses already taken today, from the broker rather than our own log:
    # a position closed outside this system still spends the daily budget.
    realised, unrealised = day_pnl(client)
    combined = realised + unrealised
    if combined <= -limits.daily_loss_limit:
        log({"event": "halted", "reason": "daily loss limit reached",
             "realised_pnl": str(realised), "unrealised_pnl": str(unrealised),
             "combined": str(combined), "limit": str(limits.daily_loss_limit)})
        return 0

    exposure = held_exposure(client)
    if cash > 0 and exposure >= cash * limits.max_leverage:
        log({"event": "halted", "reason": "leverage ceiling already reached",
             "exposure": str(exposure), "cash": str(cash),
             "ratio": str(round(exposure / cash, 2)),
             "limit": str(limits.max_leverage)})
        return 0

    day_log = reconcile_with_broker(client, todays_placements())
    held_ids = held_security_ids(client)
    if day_log["trades_taken"] >= limits.max_trades_per_day:
        log({"event": "halted", "reason": "trade limit reached",
             "taken": day_log["trades_taken"]})
        return 0

    log({"event": "run_start", "cash": str(cash), "realised_pnl": str(realised),
         "unrealised_pnl": str(unrealised), "exposure": str(exposure),
         "max_leverage": str(limits.max_leverage),
         "trades_taken": day_log["trades_taken"],
         "phase": session.phase(now)})

    master = Instruments(client=client, source="equity").load()
    bars = Bars(client, "5minute")
    # Anything the account holds intraday -- bot-filled or bought by hand --
    # occupies a slot: it is capital at risk whoever placed it.
    held_syms = set(day_log["open_symbols"])
    for sym in watchlist():
        try:
            if str(master.resolve(sym).security_id) in held_ids:
                held_syms.add(sym)
        except KeyError:
            pass
    held_syms |= set(held_ids)       # unknown names still fill a slot
    day_state = risk_mod.DayState(
        trades_taken=day_log["trades_taken"],
        risk_committed=day_log["risk_committed"],
        open_symbols=tuple(held_syms),
        exposure=exposure,
        unrealised_pnl=unrealised)

    placed = 0
    # How many resting orders may sit at once. Not the trade cap -- resting
    # is free -- but a ceiling so a bug cannot paper the book.
    budget = int(os.environ.get("TRADER_MAX_RESTING", "15")) - len(day_log.get("resting_symbols", ()))

    # Resting orders are free and may never fire, so the book can hold many
    # of them. What must never exceed cash is the FILLED total, and the fill
    # monitor cancels the rest the moment the concurrent cap is reached. So
    # each order is sized at cash / max_concurrent: if the cap's worth fill
    # at once, the position still fits inside cash at 1.0x leverage. The
    # only overshoot is the race between a fill and the cancel, a few
    # seconds, which intraday margin absorbs.
    # ...but a slot has to be big enough to be a trade. On 2026-11-05 a
    # max_concurrent of 20 split Rs 26k into Rs 1,387 per order: under one
    # share of TITAN, and under the Rs 24 charge floor for everything else,
    # so every name sized to zero. The slot count bounds the COUNT; the
    # per-order cash is floored at TRADER_MIN_ORDER_VALUE, and the fill
    # monitor prunes resting orders by exposure once fills land.
    held_cash = day_state.exposure
    free_cash = max(cash - held_cash, Decimal(0))
    free_slots = max(limits.max_concurrent_positions - len(set(day_state.open_symbols)), 0)
    viable_slots = max(int(free_cash / MIN_ORDER_VALUE), 1) if free_cash > 0 else 0
    slots = min(free_slots, viable_slots)
    per_order_cash = (free_cash / slots) if slots else Decimal(0)
    log({"event": "sizing", "cash": str(cash), "held": str(held_cash),
         "free_slots": free_slots, "viable_slots": viable_slots,
         "per_order": str(round(per_order_cash, 2))})

    for sym in watchlist():
        if placed >= budget:
            log({"event": "stop", "reason": "resting-order ceiling reached"})
            break
        if sym in day_state.open_symbols or sym in day_log.get("resting_symbols", ()):
            # Already held or already resting. A second order in the same
            # name doubles the position if both trigger.
            continue
        if free_slots <= 0:
            log({"event": "stop", "reason": "concurrent cap reached; "
                 "no free slots for new resting orders"})
            break
        if halted():
            log({"event": "halted", "reason": "kill switch during run"})
            break

        try:
            inst = master.resolve(sym)
        except KeyError:
            continue

        candles = bars.fetch_one(scrip_code(inst), session.open,
                                 min(sess.now(), session.close))
        if not candles:
            log({"event": "no_data", "symbol": sym})
            continue

        rng = opening_range(candles, session)
        if rng is None:
            continue
        high, low = rng

        try:
            code = scrip_code(inst)
            ltp = Decimal(str(((client.get(
                "/market/quotes/ltp", params={"scrip-codes": code})
                .get("data") or {}).get(code, {}) or {}).get("live_price")))
            plan = strat.plan_trigger_exit(high, low, inst.tick_size)
        except (strat.PlanError, Exception) as exc:     # noqa: BLE001
            log({"event": "skip", "symbol": sym, "reason": str(exc)[:160]})
            continue

        sizing = risk_mod.size_position(plan, per_order_cash, limits)
        if not sizing.ok:
            log({"event": "unsized", "symbol": sym,
                 "reason": sizing.limit_hit,
                 "per_order_cash": str(round(per_order_cash, 2))})
            continue

        verdict = risk_mod.check_entry_order(
            symbol=sym, plan=plan, sizing=sizing, session=session,
            now=sess.now(), day=day_state, limits=limits,
            cash_for_leverage=cash,
            upper_limit=getattr(inst, "upper_limit", None),
            lower_limit=getattr(inst, "lower_limit", None))
        if not verdict.passed:
            log({"event": "blocked", "symbol": sym,
                 "reasons": verdict.reasons})
            continue

        try:
            order = build_trigger_for_instrument(
                inst, qty=sizing.qty, range_high=high, stop=plan.stop,
                target=plan.target, cmp=ltp,
                remarks=f"auto/{sym}/{now:%Y-%m-%d}")
        except SmartOrderError as exc:
            log({"event": "rejected_locally", "symbol": sym,
                 "reason": str(exc)})
            continue

        payload = order.payload()
        value = sizing.position_value
        record = {"symbol": sym, "qty": sizing.qty, "entry": str(plan.entry),
                  "stop": str(plan.stop), "target": str(plan.target),
                  "risk": str(sizing.loss_at_stop),
                  "costs": str(sizing.round_trip_cost),
                  "gross_if_target": str(sizing.gain_at_target),
                  "net_if_target": str(sizing.net_if_target_hits),
                  "position_value": str(value),
                  "leverage": str(round(value / cash, 2)) if cash else "0",
                  "range_high": str(high), "range_low": str(low),
                  "tick": str(inst.tick_size),
                  "ltp_at_placement": str(ltp),
                  "why": (f"opening range {session.open:%H:%M}-"
                          f"{session.opening_range_end:%H:%M} was {low}-{high}; "
                          f"buy if price breaks above {plan.entry} (range high + 1 tick); "
                          f"stop {plan.stop} at the range midpoint, target {plan.target} = 2R"),
                  "payload": payload}

        if not LIVE:
            log({"event": "would_place", **record})
            placed += 1
            continue

        try:
            resp = client.post("/smart/order", body=payload)
            data = (resp or {}).get("data") or {}
            first = (data.get("order_data") or [{}])[0]
            parent = first.get("order_id")
            child = (first.get("child_order_details") or {}).get("order_id")
            log({"event": "placed", "parent_order_id": parent,
                 "child_order_id": child, **record})
            placed += 1
            # Resting, not filled: it holds no capital and is not a trade
            # until it fires. The fill monitor handles what happens then.
        except Exception as exc:                        # noqa: BLE001
            # A failed order is never retried without a fresh check: the
            # failure may be margin, a band, or a position already open.
            log({"event": "order_failed", "symbol": sym,
                 "reason": str(exc)[:300]})
            continue

        time.sleep(1)       # stay well under the 10/sec order budget

    log({"event": "run_end", "placed": placed, "live": LIVE})
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
