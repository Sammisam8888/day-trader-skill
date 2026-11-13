"""
Numbers the dashboard and the logs agree on: today's P&L split into
realised, open and charges; per-trade results; win rate; the snapshot the
fill monitor writes so the dashboard never has to call the broker itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges as charges_mod                           # noqa: E402
import common                                           # noqa: E402

SNAPSHOT = Path(os.environ.get("TRADER_SNAPSHOT", str(common.STATE_DIR.parent / "snapshot.json")))


def D(x) -> Decimal:
    try:
        return Decimal(str(x if x not in (None, "") else 0))
    except Exception:                                   # noqa: BLE001
        return Decimal(0)


def pnl(positions: List[dict], ltps: Dict[str, Decimal], orders: Optional[List[dict]] = None) -> dict:
    """Realised (closed), open (marked to LTP), charges (closed round trips
    plus the exit still owed on open positions), net.

    With `orders`, brokerage is charged per executed ORDER as the broker
    does: a stock traded twice is four orders, not two (13 Nov: the
    per-stock count showed -51 net where the real figure was about -74)."""
    realised = sum((D(p["realised"]) for p in positions), Decimal(0))
    open_, fees, brok_sym, brok_owed = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    for p in positions:
        qty = int(p["qty"])
        avg = D(p["avg"])
        if qty:
            ltp = D(ltps.get(str(p["token"])) or p.get("ltp") or avg)
            open_ += (ltp - avg) * qty
            c = charges_mod.round_trip(avg * abs(qty), ltp * abs(qty))
            fees += c.total
            brok_sym += c.brokerage
            brok_owed += charges_mod.brokerage(ltp * abs(qty))          # the exit not yet sent
        q = min(int(p.get("buy_qty") or 0), int(p.get("sell_qty") or 0))
        if q:
            c = charges_mod.round_trip(D(p["buy_avg"]) * q, D(p["sell_avg"]) * q)
            fees += c.total
            brok_sym += c.brokerage
    if orders is not None:
        brok_ord = sum((charges_mod.brokerage(D(o.get("avg")) * int(o.get("filled") or 0))
                        for o in orders if int(o.get("filled") or 0) > 0
                        and str(o.get("product") or "I") in ("I", "")), Decimal(0)) + brok_owed
        fees += ((brok_ord - brok_sym) * (1 + charges_mod.GST_RATE)).quantize(Decimal("0.01"))
    return {"realised": realised, "open": open_, "charges": fees,
            "net": realised + open_ - fees}


def closed_trades(positions: List[dict]) -> List[dict]:
    """One row per symbol with a completed round trip today."""
    out = []
    for p in positions:
        q = min(int(p.get("buy_qty") or 0), int(p.get("sell_qty") or 0))
        if not q:
            continue
        fee = charges_mod.round_trip(D(p["buy_avg"]) * q, D(p["sell_avg"]) * q).total
        gross = (D(p["sell_avg"]) - D(p["buy_avg"])) * q
        out.append({"symbol": p["symbol"], "token": str(p["token"]), "qty": q,
                    "buy_avg": D(p["buy_avg"]), "sell_avg": D(p["sell_avg"]),
                    "gross": gross, "charges": fee, "net": gross - fee})
    return out


def _enc(x):
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, dict):
        return {k: _enc(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_enc(v) for v in x]
    return x


def write_snapshot(data: dict) -> None:
    rec = {"ts": time.time(), **_enc(data)}
    try:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        tmp = SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, SNAPSHOT)
    except OSError:
        pass


NUM_KEYS = {"avg", "ltp", "realised", "unrealised", "buy_avg", "sell_avg", "tick",
            "price", "trigger"}


def read_snapshot(max_age: float = 20) -> Optional[dict]:
    try:
        rec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(rec.get("ts", 0)) > max_age:
        return None
    for key in ("positions", "orders"):
        rec[key] = [{k: (D(v) if k in NUM_KEYS else v) for k, v in r.items()} for r in rec.get(key, [])]
    rec["ltps"] = {k: D(v) for k, v in (rec.get("ltps") or {}).items()}
    if rec.get("cash") is not None:
        rec["cash"] = D(rec["cash"])
    return rec


def history(days: int = 30) -> List[dict]:
    """Per-day results from the day_summary events the flatten writes."""
    out = []
    try:
        files = sorted(common.STATE_DIR.glob("20??-??-??.jsonl"))[-days:]
    except OSError:
        return out
    for f in files:
        summ = None
        for line in f.read_text(encoding="utf-8").splitlines():
            if '"day_summary"' in line:
                try:
                    summ = json.loads(line)
                except ValueError:
                    pass
        if summ:
            out.append({"day": f.stem, "net": summ.get("net"), "trades": summ.get("trades", 0),
                        "wins": summ.get("wins", 0)})
    return out
