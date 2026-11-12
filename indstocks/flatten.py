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
from datetime import timedelta
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
    """Each round reaches further through the touch: 1x, 2x, 4x the base.
    The final round (round_no >= ROUNDS) is the must-close price: the circuit
    band's edge (lower band to sell, upper to buy). The exchange fills it at
    the best price available right now, exactly like a market order, which
    Shoonya's API does not accept here."""
    q = broker.quote(inst.token, inst.exch)
    if round_no >= ROUNDS:
        edge = q["lc"] if side == "S" else q["uc"]
        if edge and edge > 0:
            return edge
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


def close_position(broker, pos: dict, *, remarks: str, sleep=time.sleep) -> str:
    """Exit one position now: clear its pending closing-side orders (the
    stop), then a marketable limit for the whole quantity. Either side."""
    # The caller's row may be a minute old (Claude thinks for a while): the
    # stop or target may have closed it since. Exit what is open NOW, or
    # nothing, so an exit can never open a position the other way.
    side = "S" if pos["qty"] > 0 else "B"
    fresh = next((p for p in broker.positions() if str(p["token"]) == str(pos["token"])), None)
    qty = fresh["qty"] if fresh else 0
    if qty == 0 or (qty > 0) != (side == "S"):
        log({"event": "exit_skipped", "symbol": pos["symbol"], "reason": "already closed"})
        return None
    pos = fresh
    inst = broker.instrument_for(pos)
    for o in broker.orders():
        if o["token"] == pos["token"] and o["pending"] and o["side"] == side:
            try:
                broker.cancel(o["id"])
            except ShoonyaError:
                pass
    sleep(0.5)
    fresh = next((p for p in broker.positions() if str(p["token"]) == str(pos["token"])), None)
    qty = fresh["qty"] if fresh else 0
    if qty == 0 or (qty > 0) != (side == "S"):
        log({"event": "exit_skipped", "symbol": pos["symbol"], "reason": "the stop filled while it was cancelled"})
        return None
    return broker.place(inst, side=side, qty=abs(qty), price=broker.marketable(inst, side),
                        remarks=remarks[:60])


def run(broker=None, *, now=None, sleep=time.sleep) -> int:
    now = now or sess.now()
    session = sess.session_for(now)
    if session is None:
        return 0
    if now < session.flat_by:
        # Only at flatten time. A timer firing on deploy or a clock edge
        # must not close positions early or cry "close manually".
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
    # A late pass (15:10 onward) has no time to work the price: straight to must-close.
    late = now >= session.flat_by + timedelta(minutes=4)
    for rnd in ([ROUNDS] if late else list(range(ROUNDS)) + [ROUNDS]):
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
                     "qty": abs(pos["qty"]), "price": str(px), "order_id": oid, "round": rnd + 1,
                     "must_close": rnd >= ROUNDS})
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
        if o["id"] in ours or o["remarks"].startswith(("auto/", "stop/", "tgt/", "man/", "flat/", "chat/",
                                                        "exit/", "ctl/", "brk/")):
            try:
                broker.cancel(o["id"])
                swept += 1
            except ShoonyaError as exc:
                log({"event": "cancel_failed", "order_id": o["id"], "reason": str(exc)[:200]})
    log({"event": "flatten_done", "still_open": failed, "swept": swept})
    try:
        import metrics
        allpos = broker.positions()
        trades = metrics.closed_trades(allpos)
        summ = metrics.pnl(allpos, {})
        log({"event": "day_summary", "net": str(summ["net"]), "realised": str(summ["realised"]),
             "charges": str(summ["charges"]), "trades": len(trades),
             "wins": sum(1 for x in trades if x["net"] > 0),
             "detail": [{k: str(v) for k, v in x.items()} for x in trades]})
        record_results(trades, broker.orders())
    except ShoonyaError:
        pass
    return 1 if failed else 0


def alignment(side: str, bias) -> str:
    """Did the trade go with the sentiment bias at the time it was placed?"""
    try:
        b = float(bias)
    except (TypeError, ValueError):
        return "unknown"
    if abs(b) < 0.3:
        return "neutral"
    return "with" if (b > 0) == (side == "B") else "against"


def record_results(trades: list, orders: list) -> None:
    """One trade_result per closed symbol, carrying what the system believed
    when it entered (direction, sentiment), so the signals can be scored."""
    filled = {o["id"] for o in orders if o["filled"] > 0}
    placed = [r for r in common.events("placed") if str(r.get("order_id")) in filled]
    for tr in trades:
        p = next((r for r in placed if r.get("symbol") == tr["symbol"]), None)
        if not p:
            continue
        log({"event": "trade_result", "symbol": tr["symbol"], "side": p.get("side", "B"),
             "net": str(tr["net"]), "win": tr["net"] > 0, "order_id": p.get("order_id"),
             "decided_by": p.get("decided_by"), "direction": p.get("direction"),
             "sentiment_bias": p.get("sentiment_bias"), "size_mult": p.get("size_mult"),
             "sentiment": alignment(p.get("side", "B"), p.get("sentiment_bias")),
             "setup": p.get("setup") or "unlisted"})


if __name__ == "__main__":
    raise SystemExit(run())
