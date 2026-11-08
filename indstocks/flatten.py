"""
Close every intraday position before the broker does, and leave nothing
resting into the next day.

Run by the fill monitor at the session's flat-by time and five minutes
later; the flatten timers run it too, as a backup. Idempotent: with nothing
open and nothing pending it does nothing.

Order of work, per position:
  1. cancel every pending SELL in that symbol (the stop), or the exchange
     rejects the exit for selling more than is held
  2. sell the whole quantity with a marketable LIMIT (Shoonya has no market
     order), priced TRADER_MARKETABLE_PCT through the bid
  3. wait, re-read, and re-price anything still open more aggressively,
     up to three rounds
Then cancel every remaining pending intraday BUY entry placed by this system.

Manual intraday positions are closed too (the owner asked for all intraday
exposure to be managed). Delivery (CNC) holdings are never touched.
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import session as sess                                   # noqa: E402
from market import to_tick                               # noqa: E402
from shoonya import LoginRequired, Shoonya, ShoonyaError  # noqa: E402

JOB = "flatten"
ROUNDS = 3
WAIT_S = float(os.environ.get("TRADER_FLATTEN_WAIT_S", "6"))


def log(rec: dict) -> dict:
    return common.log(JOB, rec)


def price_for(broker, inst, side: str, round_no: int) -> Decimal:
    """Each round reaches further through the touch: 1x, 2x, 4x the base."""
    q = broker.quote(inst.token, inst.exch)
    base = broker.marketable(inst, side, q)
    if round_no == 0:
        return base
    ref = q["bid"] if side == "S" and q["bid"] > 0 else (q["ask"] if q["ask"] > 0 else q["ltp"])
    pct = Decimal(common.setting("TRADER_MARKETABLE_PCT", "0.3")) * (2 ** round_no) / 100
    if side == "S":
        px = to_tick(ref * (1 - pct), inst.tick, mode="down")
        return max(px, q["lc"]) if q["lc"] > 0 else px
    px = to_tick(ref * (1 + pct), inst.tick, mode="up")
    return min(px, q["uc"]) if q["uc"] > 0 else px


def run(broker=None, *, now=None, sleep=time.sleep) -> int:
    now = now or sess.now()
    if sess.session_for(now) is None:
        return 0
    broker = broker or Shoonya()
    try:
        positions = [p for p in broker.positions() if p["qty"] != 0]
        orders = broker.orders()
    except LoginRequired as exc:
        log({"event": "login_required", "reason": str(exc),
             "action": "CLOSE POSITIONS MANUALLY in the Shoonya app before the broker square-off"})
        common.notify("FLATTEN BLOCKED", "Not logged in to Shoonya. Close intraday positions by hand.",
                      priority="urgent")
        return 1

    failed = 0
    for rnd in range(ROUNDS):
        if not positions:
            break
        for pos in positions:
            inst = broker.instrument_for(pos)
            side = "S" if pos["qty"] > 0 else "B"
            # 1. clear every pending order on this symbol's closing side
            for o in orders:
                if o["token"] == pos["token"] and o["pending"] and o["side"] == side:
                    try:
                        broker.cancel(o["id"])
                        log({"event": "cancelled", "symbol": pos["symbol"], "order_id": o["id"],
                             "reason": "clearing the way for the exit"})
                    except ShoonyaError as exc:
                        log({"event": "cancel_failed", "order_id": o["id"], "reason": str(exc)[:200]})
            # 2. exit
            try:
                px = price_for(broker, inst, side, rnd)
                oid = broker.place(inst, side=side, qty=abs(pos["qty"]), price=px,
                                   remarks=f"flat/{pos['symbol']}")
                log({"event": "sold" if side == "S" else "covered", "symbol": pos["symbol"],
                     "qty": abs(pos["qty"]), "price": str(px), "order_id": oid, "round": rnd + 1})
            except ShoonyaError as exc:
                log({"event": "sell_failed", "symbol": pos["symbol"], "qty": pos["qty"],
                     "reason": str(exc)[:300], "round": rnd + 1})
        # 3. verify
        sleep(WAIT_S)
        try:
            positions = [p for p in broker.positions() if p["qty"] != 0]
            orders = broker.orders()
        except ShoonyaError as exc:
            log({"event": "error", "reason": f"could not re-read positions: {exc}"})
            break
        # anything still open: pull our unfilled exits before re-pricing
        for o in orders:
            if o["pending"] and o["remarks"].startswith("flat/"):
                try:
                    broker.cancel(o["id"])
                except ShoonyaError:
                    pass
        if positions:
            try:
                orders = broker.orders()
            except ShoonyaError:
                pass

    if positions:
        failed = len(positions)
        names = ", ".join(f"{p['symbol']} {p['qty']}" for p in positions)
        log({"event": "flatten_incomplete", "open": names,
             "action": "CLOSE THESE MANUALLY before the broker square-off"})
        common.notify("FLATTEN INCOMPLETE", f"Still open: {names}. Close by hand.", priority="urgent")

    # sweep: every pending entry this system placed, and any orphan stop
    swept = 0
    ours = common.our_order_ids()
    for o in orders:
        if not o["pending"]:
            continue
        if o["id"] in ours or o["remarks"].startswith(("auto/", "stop/", "tgt/", "man/", "flat/", "chat/")):
            try:
                broker.cancel(o["id"])
                swept += 1
            except ShoonyaError as exc:
                log({"event": "cancel_failed", "order_id": o["id"], "reason": str(exc)[:200]})
    log({"event": "flatten_done", "still_open": failed, "swept": swept})
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
