"""
Shadow runner: decide exactly as if trading, place nothing, write it down.

This is the honest way to earn confidence in both halves of the system. The
code half: a week of real mornings exercises resolution, candles, ranges,
sizing, charges and the gate against live data, which is where every bug so
far has actually surfaced -- not in tests. The strategy half: each logged
decision carries the levels, so the outcome can be scored afterwards against
what the stock really did.

One JSON line per decision, appended to a dated file. Nothing here can reach
a write endpoint; there is no import that could place an order.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges as charges_mod
import risk as risk_mod
import session as sess
import strategy as strat
from bars import Bars, opening_range, scrip_code
from client import Client
from instruments import Instruments
from smartorder import SmartOrderError, build_trigger_for_instrument

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = Path(os.environ.get("TRADER_SHADOW_DIR", "/var/lib/indstocks/shadow"))


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


def write(record: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{sess.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return path


def run() -> int:
    now = sess.now()
    session = sess.session_for(now)
    if session is None:
        write({"ts": now.isoformat(), "event": "skipped",
               "reason": "not an NSE trading day"})
        return 0
    if now < session.opening_range_end:
        write({"ts": now.isoformat(), "event": "skipped",
               "reason": f"opening range closes {session.opening_range_end:%H:%M}"})
        return 0

    client = Client()
    cash = available_cash(client)
    if cash is None:
        write({"ts": now.isoformat(), "event": "error",
               "reason": "could not read funds"})
        return 1

    limits = risk_mod.Limits(
        max_loss_per_trade=Decimal(os.environ.get("TRADER_MAX_LOSS", "500")),
        daily_loss_limit=Decimal(os.environ.get("TRADER_DAILY_LOSS", "1500")),
        max_trades_per_day=int(os.environ.get("TRADER_MAX_TRADES", "3")))

    master = Instruments(client=client, source="equity").load()
    bars = Bars(client, "5minute")

    write({"ts": now.isoformat(), "event": "run_start", "cash": str(cash),
           "phase": session.phase(now),
           "limits": {"max_loss": str(limits.max_loss_per_trade),
                      "daily": str(limits.daily_loss_limit),
                      "trades": limits.max_trades_per_day}})

    proposed = 0
    remaining_cash = cash
    for sym in watchlist():
        rec = {"ts": sess.now().isoformat(), "symbol": sym}
        try:
            inst = master.resolve(sym)
        except KeyError:
            write({**rec, "event": "unresolved"})
            continue

        candles = bars.fetch_one(scrip_code(inst), session.open,
                                 min(now, session.close))
        if not candles:
            # A feed gap, not an absence of setups. Recorded as unknown so a
            # quiet day cannot be mistaken for a scanned one.
            write({**rec, "event": "no_data"})
            continue

        rng = opening_range(candles, session)
        if rng is None:
            write({**rec, "event": "no_range"})
            continue
        high, low = rng
        rec.update({"range_high": str(high), "range_low": str(low),
                    "tick": str(inst.tick_size)})

        try:
            ltp = Decimal(str(((client.get(
                "/market/quotes/ltp",
                params={"scrip-codes": scrip_code(inst)}).get("data") or {})
                .get(scrip_code(inst), {}) or {}).get("live_price")))
        except Exception as exc:                        # noqa: BLE001
            write({**rec, "event": "no_quote", "reason": str(exc)[:120]})
            continue
        rec["ltp"] = str(ltp)

        try:
            plan = strat.plan_trigger_exit(high, low, inst.tick_size)
        except strat.PlanError as exc:
            write({**rec, "event": "no_plan", "reason": str(exc)})
            continue

        sizing = risk_mod.size_position(plan, remaining_cash, limits)
        rec.update({"entry": str(plan.entry), "stop": str(plan.stop),
                    "target": str(plan.target), "qty": sizing.qty,
                    "limit_hit": sizing.limit_hit,
                    "loss_at_stop": str(sizing.loss_at_stop),
                    "gain_at_target": str(sizing.gain_at_target),
                    "costs": str(sizing.round_trip_cost),
                    "net_if_target": str(sizing.net_if_target_hits),
                    "breakeven_pct": str(
                        charges_mod.breakeven_pct(plan.entry, max(sizing.qty, 1)))})

        if not sizing.ok:
            write({**rec, "event": "unsized"})
            continue

        verdict = risk_mod.check_entry_order(
            symbol=sym, plan=plan, sizing=sizing, session=session, now=now,
            day=risk_mod.DayState(), limits=limits)
        rec["risk"] = "PASS" if verdict.passed else "; ".join(verdict.reasons)

        try:
            order = build_trigger_for_instrument(
                inst, qty=sizing.qty, range_high=high, stop=plan.stop,
                target=plan.target, cmp=ltp,
                remarks=f"shadow/{sym}/{now:%Y-%m-%d}")
            rec["payload"] = order.payload()
        except SmartOrderError as exc:
            write({**rec, "event": "rejected_locally", "reason": str(exc)})
            continue

        rec["remaining_cash"] = str(remaining_cash)
        write({**rec, "event": "would_place" if verdict.passed else "blocked"})
        if verdict.passed:
            proposed += 1
            remaining_cash -= sizing.position_value

    path = write({"ts": sess.now().isoformat(), "event": "run_end",
                  "would_have_placed": proposed})
    print(f"shadow run complete: {proposed} would have been placed -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
