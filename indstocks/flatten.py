"""
Close every intraday position before the broker does it for you.

The entry side had no counterpart: five timers that open positions and
nothing that closes them. If neither OCO leg fills, the position sits until
the broker's own auto-square-off, which costs Rs 50 + GST per position and
fills at whatever the market gives at that moment.

Runs at 15:05 by default -- before the broker's 15:20 cutoff, with room for
a market order to fill and for a second attempt if the first is rejected.

Cancels the resting OCO legs first. A sell while they are live is rejected:
the shares are already committed to them.

This file can place orders. It is deliberately narrow: it only ever SELLS,
only what the broker says is held, and never more.
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session as sess
from client import Client

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))


def log(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": sess.now().isoformat(), "job": "flatten", **record}
    with (STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(json.dumps(record, default=str), flush=True)


def bot_filled_today(client) -> dict:
    """security_id -> shares the bot actually bought today.

    The flatten must sell only what this system opened. A position the
    account owner bought by hand in the same name belongs to them, and a
    flatten that sells it is a bug with a bill attached. Read fills from the
    order book rather than our log: the log says what was placed, the book
    says what traded.
    """
    filled = {}
    try:
        rows = (client.get("/order-book") or {}).get("data") or []
    except Exception:                                   # noqa: BLE001
        return filled
    for r in rows if isinstance(rows, list) else []:
        if not str(r.get("remarks", "")).startswith("auto/"):
            continue
        if r.get("parent_order_id"):
            continue                                    # a leg, not an entry
        if str(r.get("txn_type", "")).upper() != "BUY":
            continue
        try:
            traded = int(r.get("traded_qty") or 0)
        except (TypeError, ValueError):
            continue
        if traded > 0:
            sid = str(r.get("security_id"))
            filled[sid] = filled.get(sid, 0) + traded
    return filled


def open_intraday(client) -> list:
    """Positions the broker says are open, with a net quantity."""
    try:
        data = (client.get("/portfolio/positions") or {}).get("data") or {}
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"positions unreadable: {exc}"})
        return []

    rows = data if isinstance(data, list) else (
        data.get("positions") or data.get("net_positions") or [])
    out = []
    for r in rows or []:
        qty = r.get("net_qty", r.get("quantity", r.get("qty")))
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            continue
        if qty > 0 and str(r.get("product", "")).upper() in ("INTRADAY", "MIS", ""):
            out.append({"security_id": str(r.get("security_id")),
                        "name": r.get("name") or r.get("trading_symbol") or "?",
                        "qty": qty,
                        "avg_price": r.get("avg_price") or r.get("buy_avg_price")
                        or r.get("average_price") or 0,
                        "exchange": r.get("exchange") or "NSE"})
    return out


def cancel_resting_legs(client, security_id: str) -> list:
    """Cancel OCO/GTT legs holding these shares, or the sell is rejected."""
    cancelled = []
    try:
        rows = (client.get("/order-book") or {}).get("data") or []
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"order book unreadable: {exc}"})
        return cancelled

    for r in rows if isinstance(rows, list) else []:
        if str(r.get("security_id")) != str(security_id):
            continue
        status = str(r.get("status", "")).upper()
        if status not in ("PENDING", "SL-PENDING", "O-PENDING", "QUEUED",
                          "CREATED", "INITIATED", "MODIFIED"):
            continue
        oid = r.get("id") or r.get("order_id")
        if not oid:
            continue
        try:
            client.post("/smart/order/cancel", body={"order_id": oid})
            cancelled.append(oid)
        except Exception as exc:                        # noqa: BLE001
            log({"event": "cancel_failed", "order_id": oid,
                 "reason": str(exc)[:200]})
    return cancelled


def cancel_all_unfilled(client) -> dict:
    """Cancel every resting entry we placed that never triggered.

    DAY validity was supposed to expire these at the close. The broker's
    order book at 23:32 on 2026-11-04 still showed six of them as open, so
    it does not, or not reliably. An unfilled trigger from yesterday carries
    yesterday's levels into today, and the dedupe check reads our log rather
    than the broker, so it would not see it. Cancel explicitly.

    Returns counts. Only orders tagged auto/ -- nothing of the account
    owner's.
    """
    cancelled, failed = [], []
    try:
        rows = (client.get("/order-book") or {}).get("data") or []
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"order book unreadable: {exc}"})
        return {"cancelled": cancelled, "failed": failed}

    for r in rows if isinstance(rows, list) else []:
        if not str(r.get("remarks", "")).startswith("auto/"):
            continue
        status = str(r.get("status", "")).upper()
        if status not in ("PENDING", "SL-PENDING", "O-PENDING", "QUEUED",
                          "CREATED", "INITIATED", "MODIFIED"):
            continue
        oid = r.get("id") or r.get("order_id")
        if not oid:
            continue
        try:
            client.post("/smart/order/cancel", body={"order_id": oid})
            cancelled.append(oid)
        except Exception as exc:                        # noqa: BLE001
            failed.append({"order_id": oid, "reason": str(exc)[:160]})
        time.sleep(0.3)
    return {"cancelled": cancelled, "failed": failed}


def sell(client, pos: dict) -> dict:
    body = {
        "txn_type": "SELL",
        "exchange": pos["exchange"],
        "segment": "EQUITY",
        "product": "INTRADAY",
        "order_type": "MARKET",
        "validity": "DAY",
        "security_id": pos["security_id"],
        "qty": pos["qty"],
        "algo_id": "99999",
        "remarks": f"flat/{sess.now():%Y-%m-%d}"[:100],
    }
    return client.post("/order", body=body)


def run() -> int:
    now = sess.now()
    if sess.session_for(now) is None:
        log({"event": "skipped", "reason": "not a trading day"})
        return 0

    client = Client()
    positions = open_intraday(client)
    if not positions:
        log({"event": "nothing_open"})
        sweep = cancel_all_unfilled(client)
        log({"event": "cancelled_unfilled", "count": len(sweep["cancelled"]),
             "order_ids": sweep["cancelled"], "failed": sweep["failed"]})
        log({"event": "flatten_done", "failed": 0,
             "cancel_failed": len(sweep["failed"])})
        return 1 if sweep["failed"] else 0

    # Every INTRADAY position goes, manual ones included: the account owner
    # asked for all intraday exposure to be managed and closed before the
    # broker's own square-off (which charges Rs 50 + GST). Delivery (CNC)
    # holdings are not positions here and are never touched. Set
    # TRADER_FLATTEN_ALL=false to restore "only what the bot bought".
    if os.environ.get("TRADER_FLATTEN_ALL", "true").lower() not in ("1", "true", "yes"):
        ours = bot_filled_today(client)
        trimmed = []
        for pos in positions:
            bot_qty = ours.get(pos["security_id"], 0)
            if bot_qty <= 0:
                log({"event": "left_alone", "name": pos["name"], "qty": pos["qty"],
                     "reason": "not opened by this system"})
                continue
            if bot_qty < pos["qty"]:
                log({"event": "trimmed", "name": pos["name"], "held": pos["qty"],
                     "selling": bot_qty, "reason": "rest was bought manually"})
            trimmed.append({**pos, "qty": min(bot_qty, pos["qty"])})
        positions = trimmed
    if not positions:
        log({"event": "nothing_ours"})
        sweep = cancel_all_unfilled(client)
        log({"event": "cancelled_unfilled", "count": len(sweep["cancelled"]),
             "order_ids": sweep["cancelled"], "failed": sweep["failed"]})
        log({"event": "flatten_done", "failed": 0,
             "cancel_failed": len(sweep["failed"])})
        return 1 if sweep["failed"] else 0

    log({"event": "flattening", "count": len(positions),
         "positions": [(p["name"], p["qty"]) for p in positions]})

    failed = 0
    for pos in positions:
        cancelled = cancel_resting_legs(client, pos["security_id"])
        if cancelled:
            log({"event": "cancelled_legs", "name": pos["name"],
                 "order_ids": cancelled})
            time.sleep(1)       # let the cancels settle before selling

        try:
            resp = sell(client, pos)
            oid = ((resp or {}).get("data") or {}).get("order_id")
            log({"event": "sold", "name": pos["name"], "qty": pos["qty"],
                 "order_id": oid})
        except Exception as exc:                        # noqa: BLE001
            failed += 1
            log({"event": "sell_failed", "name": pos["name"],
                 "qty": pos["qty"], "reason": str(exc)[:300],
                 "action": "CLOSE THIS MANUALLY before the broker squares it off"})
        time.sleep(1)

    # Sweep: every resting auto/ order that never triggered, and the legs
    # of anything just sold. Runs even when nothing was open -- the orders
    # that matter most here are the ones that never became positions.
    sweep = cancel_all_unfilled(client)
    log({"event": "cancelled_unfilled", "count": len(sweep["cancelled"]),
         "order_ids": sweep["cancelled"], "failed": sweep["failed"]})

    log({"event": "flatten_done", "failed": failed,
         "cancel_failed": len(sweep["failed"])})
    return 1 if (failed or sweep["failed"]) else 0


if __name__ == "__main__":
    raise SystemExit(run())
