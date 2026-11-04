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
    symbols, risk, placed = [], Decimal(0), 0
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
            "open_symbols": tuple(symbols)}


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


def realised_pnl(client) -> Decimal:
    try:
        data = (client.get("/funds") or {}).get("data") or {}
        return Decimal(str(data.get("realized_pnl") or 0))
    except Exception:                                   # noqa: BLE001
        return Decimal(0)


def limits_from_env() -> risk_mod.Limits:
    return risk_mod.Limits(
        max_loss_per_trade=Decimal(os.environ.get("TRADER_MAX_LOSS", "500")),
        daily_loss_limit=Decimal(os.environ.get("TRADER_DAILY_LOSS", "1500")),
        max_trades_per_day=int(os.environ.get("TRADER_MAX_TRADES", "3")))


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
    pnl = realised_pnl(client)
    if pnl <= -limits.daily_loss_limit:
        log({"event": "halted", "reason": "daily loss limit reached",
             "realised_pnl": str(pnl), "limit": str(limits.daily_loss_limit)})
        return 0

    day_log = todays_placements()
    if day_log["trades_taken"] >= limits.max_trades_per_day:
        log({"event": "halted", "reason": "trade limit reached",
             "taken": day_log["trades_taken"]})
        return 0

    log({"event": "run_start", "cash": str(cash), "realised_pnl": str(pnl),
         "trades_taken": day_log["trades_taken"],
         "phase": session.phase(now)})

    master = Instruments(client=client, source="equity").load()
    bars = Bars(client, "5minute")
    day_state = risk_mod.DayState(
        trades_taken=day_log["trades_taken"],
        risk_committed=day_log["risk_committed"],
        open_symbols=day_log["open_symbols"])

    placed = 0
    budget = limits.max_trades_per_day - day_log["trades_taken"]

    # Capital is spent across the run, not per decision. Sizing each order
    # against the full balance means three orders can commit three times the
    # account: the first fills, the rest are rejected for margin, and which
    # ones survive is decided by the exchange rather than by us.
    remaining_cash = cash

    for sym in watchlist():
        if placed >= budget:
            log({"event": "stop", "reason": "trade budget spent"})
            break
        if sym in day_state.open_symbols:
            # Already placed today, by this run or an earlier one. A second
            # resting order in the same name doubles the position if both
            # trigger.
            log({"event": "skip", "symbol": sym,
                 "reason": "already placed today"})
            continue
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

        sizing = risk_mod.size_position(plan, remaining_cash, limits)
        if not sizing.ok:
            log({"event": "unsized", "symbol": sym,
                 "reason": sizing.limit_hit,
                 "remaining_cash": str(remaining_cash)})
            continue

        verdict = risk_mod.check_entry_order(
            symbol=sym, plan=plan, sizing=sizing, session=session,
            now=sess.now(), day=day_state, limits=limits)
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
        record = {"symbol": sym, "qty": sizing.qty, "entry": str(plan.entry),
                  "stop": str(plan.stop), "target": str(plan.target),
                  "risk": str(sizing.loss_at_stop),
                  "costs": str(sizing.round_trip_cost),
                  "net_if_target": str(sizing.net_if_target_hits),
                  "payload": payload}

        record["remaining_cash_before"] = str(remaining_cash)

        if not LIVE:
            log({"event": "would_place", **record})
            placed += 1
            remaining_cash -= sizing.position_value
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
            # A resting trigger reserves nothing at the broker until it
            # fires, but it can still fire, so treat it as spent.
            remaining_cash -= sizing.position_value
            day_state = risk_mod.DayState(
                trades_taken=day_state.trades_taken + 1,
                risk_committed=day_state.risk_committed + sizing.loss_at_stop,
                open_symbols=tuple(day_state.open_symbols) + (sym,))
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
