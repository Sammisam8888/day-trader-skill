#!/usr/bin/env python3
"""
MCP server over stdio. The thing Claude Desktop talks to.

Tool split is deliberate and is the security boundary:

  READ-ONLY  account, positions, scan, plan   -> safe to allow always
  WRITE      place_order, flatten, cancel     -> must be "Needs approval"

place_order cannot be reached without a prior propose_order in the same
process: it takes a ticket id, and a ticket only exists if the risk gate
passed it. There is no tool that takes a raw payload, so no prompt can talk
this server into sending an arbitrary order.

No third-party dependencies: JSON-RPC 2.0 over stdio is a hundred lines and
a dependency in a process holding broker credentials is a liability.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from decimal import Decimal
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import risk as risk_mod  # noqa: E402
import session as sess  # noqa: E402
import strategy as strat  # noqa: E402
import ticket as tk_mod  # noqa: E402
from bars import Bars, scrip_code  # noqa: E402
from client import Client  # noqa: E402
from instruments import Instruments  # noqa: E402
from smartorder import SmartOrderError, build_for_instrument  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
HERE = os.path.dirname(os.path.abspath(__file__))

_state = {"client": None, "master": None, "tickets": {}}


def client() -> Client:
    if _state["client"] is None:
        _state["client"] = Client()
    return _state["client"]


def master() -> Instruments:
    if _state["master"] is None:
        _state["master"] = Instruments(client=client(), source="equity").load()
    return _state["master"]


def limits_from_env() -> risk_mod.Limits:
    return risk_mod.Limits(
        max_loss_per_trade=Decimal(os.environ.get("TRADER_MAX_LOSS", "500")),
        daily_loss_limit=Decimal(os.environ.get("TRADER_DAILY_LOSS", "1500")),
        max_trades_per_day=int(os.environ.get("TRADER_MAX_TRADES", "3")),
    )


def watchlist() -> list:
    path = os.environ.get("TRADER_WATCHLIST", os.path.join(HERE, "watchlist.txt"))
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line.upper())
    return out


# ------------------------------------------------------------------ tools

def available_cash(client_obj) -> Optional[Decimal]:
    """Real intraday buying power from /funds.

    detailed_avl_balance.eq_mis is the equity-intraday figure, which is what
    an INTRADAY order draws on. Sizing off a configured number instead of
    this is how a 100,000 default silently sizes a 30,000 account more than
    three times too large.
    """
    try:
        funds = client_obj.get("/funds")
    except Exception:                                   # noqa: BLE001
        return None
    data = (funds or {}).get("data") or {}
    bal = data.get("detailed_avl_balance") or {}
    for key in ("eq_mis", "eq_cnc"):
        if bal.get(key) is not None:
            return Decimal(str(bal[key]))
    return None


def t_account(_args):
    funds = client().get("/funds")
    return {"funds": funds.get("data", funds)}


def t_positions(_args):
    pos = client().get("/portfolio/positions")
    return {"positions": pos.get("data", pos)}


def t_scan(args):
    now = sess.now()
    session = sess.session_for(now)
    if session is None:
        return {"trading_day": False, "message": f"{now.date()} is not an NSE trading day"}

    symbols = [s.upper() for s in (args.get("symbols") or watchlist())]
    m = master()
    resolved, missing = {}, []
    for s in symbols:
        try:
            resolved[s] = m.resolve(s)
        except KeyError:
            missing.append(s)

    codes = {scrip_code(i): s for s, i in resolved.items()}
    candles = Bars(client(), "5minute").fetch(
        list(codes), session.open, min(now, session.close))

    triggered, waiting = [], []
    for code, cs in candles.items():
        sig = strat.check_entry(cs, session, now) if cs else None
        if sig is None:
            continue
        row = {"symbol": codes[code], "state": sig.state,
               "range_high": str(sig.range_high) if sig.range_high else None,
               "range_low": str(sig.range_low) if sig.range_low else None,
               "price": str(sig.price) if sig.price else None,
               "reason": sig.reason}
        (triggered if sig.triggered else waiting).append(row)

    return {"trading_day": True, "phase": session.phase(now),
            "as_of": now.isoformat(), "triggered": triggered,
            "waiting": waiting, "unknown_symbols": missing}


def t_propose_order(args):
    """Build a plan, size it, run the risk gate, return a ticket to show the
    user. Places nothing."""
    symbol = (args.get("symbol") or "").upper()
    now = sess.now()
    session = sess.session_for(now)
    if session is None:
        return {"error": f"{now.date()} is not an NSE trading day"}

    inst = master().resolve(symbol)
    candles = Bars(client(), "5minute").fetch_one(
        scrip_code(inst), session.open, min(now, session.close))
    sig = strat.check_entry(candles, session, now)
    if not sig.triggered:
        return {"proposed": False, "state": sig.state, "reason": sig.reason}

    try:
        plan = strat.plan_exit(sig.price, sig.range_high, sig.range_low, inst.tick_size)
    except strat.PlanError as exc:
        return {"proposed": False, "reason": str(exc)}

    limits = limits_from_env()
    if args.get("cash"):
        cash, cash_source = Decimal(str(args["cash"])), "supplied"
    else:
        live = available_cash(client())
        if live is None:
            return {"proposed": False,
                    "reason": "could not read available funds; refusing to size "
                              "a position against a guess"}
        cash, cash_source = live, "live eq_mis balance"
    sizing = risk_mod.size_position(plan, cash, limits)

    log = tk_mod.day_state_from_log()
    verdict = risk_mod.check_entry_order(
        symbol=symbol, plan=plan, sizing=sizing, session=session, now=now,
        day=risk_mod.DayState(trades_taken=log["trades_taken"],
                              risk_committed=log["risk_committed"],
                              open_symbols=log["open_symbols"]),
        limits=limits)

    if not sizing.ok:
        return {"proposed": False, "reason": f"sizing refused: {sizing.limit_hit}"}

    try:
        order = build_for_instrument(
            inst, qty=sizing.qty, entry=plan.entry, stop=plan.stop,
            target=plan.target, remarks=f"orb-v1/{symbol}/{now:%Y-%m-%d}")
    except SmartOrderError as exc:
        return {"proposed": False, "reason": f"order rejected locally: {exc}"}

    tk = tk_mod.build_ticket(symbol, order, risk_verdict=verdict,
                             max_loss=sizing.loss_at_stop)
    _state["tickets"][tk.id] = tk
    return {"proposed": True, "ticket_id": tk.id, "ready": tk.ready,
            "ticket": tk.human(), "payload": tk.payload,
            "cash_used": str(cash), "cash_source": cash_source,
            "risk": "PASS" if verdict.passed else f"BLOCK: {verdict}"}


def t_place_order(args):
    """Send a ticket the user has said yes to. WRITE -- needs approval."""
    tk = _state["tickets"].get(args.get("ticket_id"))
    if tk is None:
        return {"error": "unknown ticket id; propose the order first"}
    try:
        tk_mod.approve(tk, args.get("reply") or "")
        tk_mod.place(client(), tk)
    except tk_mod.NotApproved as exc:
        return {"placed": False, "error": str(exc)}
    except tk_mod.TicketError as exc:
        return {"placed": False, "error": str(exc)}

    prot = tk_mod.confirm_protection(client(), tk)
    return {"placed": True, "parent_order_id": tk.parent_order_id,
            "child_order_id": tk.child_order_id,
            "protected": prot.protected, "status": prot.message}


def t_check_protection(args):
    tk = _state["tickets"].get(args.get("ticket_id"))
    if tk is None:
        return {"error": "unknown ticket id"}
    p = tk_mod.confirm_protection(client(), tk)
    return {"protected": p.protected, "parent_status": p.parent_status,
            "child_status": p.child_status, "filled_qty": p.filled_qty,
            "message": p.message}


def t_flatten(args):
    """Close a position with a market sell. WRITE -- needs approval."""
    symbol = (args.get("symbol") or "").upper()
    qty = int(args.get("qty") or 0)
    if not tk_mod._yes(args.get("reply") or ""):
        return {"error": "reply must be an explicit yes for this exact exit"}

    held = 0
    pos = client().get("/portfolio/positions")
    for row in (pos.get("data") or []):
        if str(row.get("security_id")) == master().resolve(symbol).security_id:
            held = int(row.get("net_qty") or row.get("qty") or 0)

    v = risk_mod.check_exit_order(symbol=symbol, qty=qty, held_qty=held)
    if not v.passed:
        return {"error": str(v)}

    body = tk_mod.build_flatten(master().resolve(symbol), qty)
    resp = client().post("/order", body=body)
    return {"flattened": True, "response": resp}


TOOLS = [
    ("account", "Account funds and margin. Read-only.", t_account,
     {"type": "object", "properties": {}}),
    ("positions", "Open positions. Read-only.", t_positions,
     {"type": "object", "properties": {}}),
    ("scan", "Scan the watchlist for opening-range breakouts. Read-only.", t_scan,
     {"type": "object", "properties": {
         "symbols": {"type": "array", "items": {"type": "string"},
                     "description": "override the watchlist"}}}),
    ("propose_order", "Build a risk-checked ticket for one symbol. Places nothing.",
     t_propose_order,
     {"type": "object", "required": ["symbol"], "properties": {
         "symbol": {"type": "string"},
         "cash": {"type": "string", "description": "account cash for sizing"}}}),
    ("place_order",
     "PLACES A REAL ORDER. Requires a ticket_id from propose_order and the "
     "user's literal reply. Never call this without showing the ticket first.",
     t_place_order,
     {"type": "object", "required": ["ticket_id", "reply"], "properties": {
         "ticket_id": {"type": "string"},
         "reply": {"type": "string",
                   "description": "the user's reply, word for word, not paraphrased"}}}),
    ("check_protection",
     "Is a filled position actually covered by its stop/target leg?",
     t_check_protection,
     {"type": "object", "required": ["ticket_id"], "properties": {
         "ticket_id": {"type": "string"}}}),
    ("flatten", "PLACES A REAL ORDER. Market-sell to close a position.", t_flatten,
     {"type": "object", "required": ["symbol", "qty", "reply"], "properties": {
         "symbol": {"type": "string"}, "qty": {"type": "integer"},
         "reply": {"type": "string"}}}),
]

HANDLERS = {name: fn for name, _d, fn, _s in TOOLS}


# ------------------------------------------------------------------ jsonrpc

def respond(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req):
    method, rid = req.get("method"), req.get("id")

    if method == "initialize":
        return respond(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "indstocks", "version": "0.1.0"},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return respond(rid, {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, d, _f, s in TOOLS]})

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        fn = HANDLERS.get(name)
        if fn is None:
            return respond(rid, error={"code": -32601, "message": f"no tool {name!r}"})
        try:
            out = fn(params.get("arguments") or {})
            return respond(rid, {"content": [
                {"type": "text", "text": json.dumps(out, indent=2, default=str)}]})
        except Exception as exc:                      # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            return respond(rid, {"content": [
                {"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True})

    if rid is not None:
        respond(rid, error={"code": -32601, "message": f"unknown method {method!r}"})
    return None


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        handle(req)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
