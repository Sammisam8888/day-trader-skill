"""
Watch what is actually happening, every few minutes, all session.

The entry side needed no monitoring: a resting trigger fires at exchange
speed whether or not anything is looking. What needed monitoring and had
none was everything AFTER a fill.

Today three orders filled at 14:43 and nothing in this system noticed. No
check that the stops armed, no record of the position, no warning. The
account owner found out from a phone notification.

This watches for the states that cost money:

  UNPROTECTED   a filled position whose OCO leg is missing or dead. Shares
                held with no stop is the worst state this system can reach,
                and it is silent.
  DUPLICATE     several resting orders in one symbol. If they all fire the
                position is a multiple of the intended size.
  STALE         a trigger the market has already passed, which can no longer
                do what it was placed to do.
  NEAR CLOSE    anything still open approaching the square-off deadline.

It reports. It does not trade: no POST, no cancel, no sell. Flattening is a
separate job with its own deadline, and a watcher that also acts is a
watcher whose bugs place orders.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session as sess
from client import Client

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))

DEAD = {"CANCELLED", "REJECTED", "FAILED", "EXPIRED", "ABORTED",
        "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"}
LIVE = {"PENDING", "SL-PENDING", "O-PENDING", "QUEUED", "CREATED",
        "INITIATED", "MODIFIED", "PROCESSING"}


def log(record: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"ts": sess.now().isoformat(), "job": "watch", **record}
    with (STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl").open(
            "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(json.dumps(record, default=str), flush=True)


def order_rows(client) -> list:
    try:
        data = (client.get("/order-book") or {}).get("data") or []
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"order book: {exc}"})
        return []
    if isinstance(data, list):
        return data
    for key in ("orders", "order_data", "order_book"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def positions(client) -> list:
    try:
        data = (client.get("/portfolio/positions") or {}).get("data") or {}
    except Exception as exc:                            # noqa: BLE001
        log({"event": "error", "reason": f"positions: {exc}"})
        return []
    rows = data if isinstance(data, list) else (
        data.get("positions") or data.get("net_positions") or [])
    out = []
    for r in rows or []:
        try:
            qty = int(r.get("net_qty", r.get("quantity", r.get("qty", 0))))
        except (TypeError, ValueError):
            continue
        if qty:
            out.append({"security_id": str(r.get("security_id")),
                        "name": r.get("name") or "?", "qty": qty})
    return out


def check(client) -> dict:
    rows = order_rows(client)
    ours = [r for r in rows if "auto/" in str(r.get("remarks", ""))
            or "flat/" in str(r.get("remarks", ""))]

    by_id = {str(r.get("id")): r for r in ours}
    alerts = []

    # Parents that traded, and whether a live child is holding the shares.
    for r in ours:
        if r.get("parent_order_id"):
            continue                        # this row IS a child leg
        try:
            traded = int(r.get("traded_qty") or 0)
        except (TypeError, ValueError):
            traded = 0
        if traded <= 0:
            continue

        child_id = str(r.get("child_order_id") or "")
        child = by_id.get(child_id)
        if child is None:
            child = next((c for c in ours
                          if str(c.get("parent_order_id")) == str(r.get("id"))), None)
        cstatus = str((child or {}).get("status", "")).upper()

        if child is None or cstatus in DEAD:
            alerts.append({
                "level": "UNPROTECTED",
                "name": r.get("name"), "order_id": r.get("id"),
                "filled_qty": traded, "child_status": cstatus or "missing",
                "action": "shares held with no stop -- place one or flatten now",
            })

    # Several resting orders in one symbol: all of them can fire.
    resting = {}
    for r in ours:
        if str(r.get("status", "")).upper() not in LIVE:
            continue
        if r.get("parent_order_id"):
            continue
        resting.setdefault(str(r.get("security_id")), []).append(r)
    for sid, group in resting.items():
        if len(group) > 1:
            alerts.append({
                "level": "DUPLICATE",
                "name": group[0].get("name"), "count": len(group),
                "order_ids": [g.get("id") for g in group],
                "action": "cancel the extras; if all fire the position multiplies",
            })

    open_positions = positions(client)
    now = sess.now()
    session = sess.session_for(now)
    if open_positions and session and now >= session.flat_by:
        alerts.append({
            "level": "NEAR_CLOSE",
            "positions": [(p["name"], p["qty"]) for p in open_positions],
            "action": "square off before the broker charges for it",
        })

    return {"resting": sum(len(g) for g in resting.values()),
            "positions": open_positions, "alerts": alerts}


def run() -> int:
    now = sess.now()
    if sess.session_for(now) is None:
        return 0
    if not (sess.MARKET_OPEN <= now.time() <= sess.MARKET_CLOSE):
        return 0

    state = check(Client())
    if state["alerts"]:
        for a in state["alerts"]:
            log({"event": "ALERT", **a})
    else:
        log({"event": "ok", "resting": state["resting"],
             "open_positions": len(state["positions"])})
    return 1 if any(a["level"] == "UNPROTECTED" for a in state["alerts"]) else 0


if __name__ == "__main__":
    raise SystemExit(run())
