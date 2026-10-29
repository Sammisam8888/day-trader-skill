"""
The ticket layer: the only path from a plan to a live order.

Three gates, every time, for every order:
  1. the risk gate passed THIS order today (matched by fingerprint)
  2. the user typed an explicit yes for THIS one order
  3. the MCP tool permission card is answered

This module owns 1 and 2 and refuses to produce a payload without them.
Gate 3 belongs to the host and is outside this code. Nothing here places an
order on its own; `place()` must be handed an approved ticket.

A smart order is a parent plus a child GTT that only arms once the parent
executes. So a fill is not the end of the job: until the child is confirmed
live, the position is unprotected. `confirm_protection` is what answers that,
and it is the question that matters most in this file.

State lives in trader/state/broker.json, which .gitignore excludes.
"""

from __future__ import annotations

import json
import os
import sys
import time as _time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session import IST, now as ist_now  # noqa: E402
from smartorder import SmartOrder  # noqa: E402

STATE_DIR = Path(os.environ.get("INDSTOCKS_STATE_DIR", "/var/lib/indstocks/state"))

# Terminal and in-flight states, from the documented status table.
FILLED = {"SUCCESS"}
PARTIAL = {"PARTIALLY FILLED"}
DEAD = {"CANCELLED", "REJECTED", "FAILED", "EXPIRED", "ABORTED",
        "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"}
WORKING = {"QUEUED", "PROCESSING", "INITIATED", "PENDING", "CREATED",
           "MODIFIED", "O-PENDING", "SL-PENDING"}


class TicketError(RuntimeError):
    pass


class NotApproved(TicketError):
    """No valid yes for this exact order. Never bypassed."""


def _yes(reply: str) -> bool:
    """Strict. 'sure', 'ok', 'yes to all' and a yes naming something else all
    fail. Turning an ambiguous reply into consent is the one thing this
    module must never do."""
    return (reply or "").strip().lower() in {"yes", "y", "yes."}


@dataclass
class Ticket:
    symbol: str
    payload: dict
    fingerprint: str
    risk_passed: bool
    risk_reason: str
    max_loss: str
    created_at: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    approved_reply: Optional[str] = None
    approved_at: Optional[str] = None
    parent_order_id: Optional[str] = None
    child_order_id: Optional[str] = None
    status: str = "new"

    @property
    def ready(self) -> bool:
        return self.risk_passed and self.status == "new"

    @property
    def approved(self) -> bool:
        return bool(self.approved_at) and _yes(self.approved_reply or "")

    def human(self) -> str:
        p = self.payload
        return "\n".join([
            f"ORDER  {self.symbol}  [{self.id}]",
            f"  BUY {p['qty']} @ {p.get('limit_price', 'market')}  "
            f"({p['product']}, {p['validity']})",
            f"  stop-loss  trigger {p.get('sl_trigger_price')} limit {p.get('sl_limit_price')}",
            f"  target     trigger {p.get('tgt_trigger_price')} limit {p.get('tgt_limit_price')}",
            f"  max loss at the stop: about {self.max_loss} "
            f"(a stop can fill worse in a fast market)",
            f"  RISK: {'PASS' if self.risk_passed else 'BLOCK'} {self.risk_reason}",
            "  Reply exactly 'yes' to place this one order.",
        ])


def fingerprint(payload: dict) -> str:
    """Identifies this exact order. An approval for one payload must never
    carry over to a different quantity or price."""
    keys = ("security_id", "txn_type", "qty", "order_type", "limit_price",
            "sl_trigger_price", "sl_limit_price", "tgt_trigger_price",
            "tgt_limit_price", "product")
    return "|".join(f"{k}={payload.get(k)}" for k in keys)


def build_ticket(symbol: str, order: SmartOrder, *, risk_verdict,
                 max_loss: Decimal, clock=ist_now) -> Ticket:
    payload = order.payload()
    return Ticket(
        symbol=symbol,
        payload=payload,
        fingerprint=fingerprint(payload),
        risk_passed=bool(risk_verdict.passed),
        risk_reason="" if risk_verdict.passed else "; ".join(risk_verdict.reasons),
        max_loss=str(max_loss),
        created_at=clock().isoformat(),
    )


def approve(ticket: Ticket, reply: str, *, clock=ist_now) -> Ticket:
    """Record the user's reply. Anything but an explicit yes is not consent."""
    if not _yes(reply):
        raise NotApproved(
            f"{reply!r} is not an explicit yes for this order. "
            "Ask again; do not interpret."
        )
    if not ticket.risk_passed:
        raise NotApproved("the risk gate blocked this order; approval cannot override it")
    ticket.approved_reply = reply
    ticket.approved_at = clock().isoformat()
    return ticket


def place(client, ticket: Ticket) -> Ticket:
    """Send an APPROVED ticket. Refuses anything else."""
    if not ticket.approved:
        raise NotApproved("no recorded yes for this ticket")
    if not ticket.risk_passed:
        raise NotApproved("risk gate did not pass this order")
    if ticket.status != "new":
        raise TicketError(f"ticket {ticket.id} is already {ticket.status}; "
                          "a new order needs a new ticket and a new yes")
    if fingerprint(ticket.payload) != ticket.fingerprint:
        raise NotApproved("the payload changed after approval; re-approve it")

    resp = client.post("/smart/order", body=ticket.payload)
    data = (resp or {}).get("data") or {}
    orders = data.get("order_data") or []
    if not orders:
        ticket.status = "failed"
        raise TicketError(f"no order_data in the response: {str(resp)[:200]}")

    first = orders[0]
    ticket.parent_order_id = first.get("order_id")
    child = first.get("child_order_details") or {}
    ticket.child_order_id = child.get("order_id")
    ticket.status = "placed"
    save(ticket)
    return ticket


def order_book(client) -> List[dict]:
    resp = client.get("/order-book")
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("orders", "order_data", "order_book"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def find_order(client, order_id: str) -> Optional[dict]:
    for row in order_book(client):
        if row.get("id") == order_id or row.get("order_id") == order_id:
            return row
    return None


@dataclass(frozen=True)
class Protection:
    parent_status: str
    child_status: Optional[str]
    filled_qty: int
    protected: bool
    message: str


def confirm_protection(client, ticket: Ticket) -> Protection:
    """Is the position actually protected?

    The child GTT arms only after the parent executes, so between fill and
    arming there is a window where shares are held with no stop. This is the
    state worth alarming on -- a filled parent with no live child is an
    unprotected position, and it is the reason this is checked rather than
    assumed.
    """
    parent = find_order(client, ticket.parent_order_id or "")
    if parent is None:
        return Protection("unknown", None, 0, False,
                          f"parent {ticket.parent_order_id} not in the order book")

    pstatus = (parent.get("status") or "").upper()
    filled = int(parent.get("traded_qty") or 0)

    child_id = ticket.child_order_id or parent.get("child_order_id")
    child = find_order(client, child_id) if child_id else None
    cstatus = (child.get("status") or "").upper() if child else None

    if pstatus in DEAD:
        return Protection(pstatus, cstatus, filled, True,
                          f"entry did not fill ({pstatus}); nothing is at risk")

    if pstatus in WORKING and filled == 0:
        return Protection(pstatus, cstatus, filled, True,
                          f"entry still working ({pstatus}); nothing filled yet")

    if filled > 0 and (cstatus is None or cstatus in DEAD):
        return Protection(
            pstatus, cstatus, filled, False,
            f"UNPROTECTED: {filled} shares filled but the stop/target leg is "
            f"{cstatus or 'missing'}. Place a stop manually or flatten now.")

    return Protection(pstatus, cstatus, filled, True,
                      f"{filled} filled, stop/target leg {cstatus}")


def wait_for_fill(client, ticket: Ticket, *, timeout_s: int = 120,
                  poll_s: int = 5, sleep=_time.sleep) -> Protection:
    """Poll until the parent resolves. Returns the protection state."""
    deadline = _time.monotonic() + timeout_s
    last = confirm_protection(client, ticket)
    while _time.monotonic() < deadline:
        if last.parent_status in FILLED or last.filled_qty > 0 \
                or last.parent_status in DEAD:
            return last
        sleep(poll_s)
        last = confirm_protection(client, ticket)
    return last


def build_flatten(instrument, qty: int, *, remarks: str = "flatten") -> dict:
    """A plain market sell to close. No legs -- this IS the exit."""
    return {
        "txn_type": "SELL",
        "exchange": getattr(instrument, "exchange", "NSE") or "NSE",
        "segment": "EQUITY",
        "product": "INTRADAY",
        "order_type": "MARKET",
        "validity": "DAY",
        "security_id": instrument.security_id,
        "qty": int(qty),
        "algo_id": "99999",
        "remarks": remarks[:100],
    }


# ---------------------------------------------------------------- state

def _state_file() -> Path:
    return STATE_DIR / f"broker-{ist_now():%Y-%m-%d}.json"


def save(ticket: Ticket) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_file()
    rows = load_all()
    rows = [r for r in rows if r.get("id") != ticket.id] + [asdict(ticket)]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)
    os.replace(tmp, path)


def load_all() -> List[dict]:
    try:
        with _state_file().open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def day_state_from_log() -> dict:
    """Trades taken and risk committed today, for the risk gate."""
    rows = [r for r in load_all() if r.get("status") == "placed"]
    return {
        "trades_taken": len(rows),
        "risk_committed": sum(Decimal(r.get("max_loss") or 0) for r in rows),
        "open_symbols": tuple(r["symbol"] for r in rows),
    }
