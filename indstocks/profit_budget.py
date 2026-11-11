"""
Profit-funded extra trades: on a good day, the trade limit grows.

TRADER_MAX_TRADES is the normal daily limit. Past it, a new entry is allowed
only if the day can afford it: the owner gives back at most
TRADER_PROFIT_GIVEBACK_PCT of today's realised net profit (after charges).

  floor      = realised_net * (1 - giveback%)          profit that is kept no matter what
  worst_day  = realised_net
             + every open position closed at its current exchange stop
               (a stop already above entry counts as locked profit)
             - the risk of every resting entry
  room       = worst_day - floor

An extra entry is allowed when its loss at the stop plus charges fits in
the room, and the room shrinks by that much for the next one. So even if
every open, resting and extra trade stops out together, the day still
closes with at least (1 - giveback%) of what it had banked. On a losing or
flat day room is zero and the normal limit holds. TRADER_MAX_TRADES_HARD
caps the total either way.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List

import common
import metrics


def giveback() -> Decimal:
    try:
        return min(max(Decimal(common.setting("TRADER_PROFIT_GIVEBACK_PCT", "50")), Decimal(0)), Decimal(90)) / 100
    except Exception:                                   # noqa: BLE001
        return Decimal("0.5")


def hard_cap(base: int) -> int:
    try:
        return max(int(common.setting("TRADER_MAX_TRADES_HARD", str(base))), base)
    except ValueError:
        return base


def room(positions: List[dict], orders: List[dict], placements: List[dict], max_loss: Decimal) -> Dict:
    closed = metrics.closed_trades(positions)
    realised_net = sum((t["net"] for t in closed), Decimal(0))
    keep = realised_net * (1 - giveback()) if realised_net > 0 else Decimal(0)
    worst = realised_net
    for p in positions:
        q = int(p["qty"])
        if not q:
            continue
        side = "S" if q > 0 else "B"
        stop = next((o["trigger"] for o in orders if o["pending"] and str(o["token"]) == str(p["token"])
                     and o["side"] == side and o.get("trigger")), None)
        if stop is None:
            worst -= max_loss
        else:
            worst += (Decimal(stop) - Decimal(p["avg"])) * q
    ours = {str(r.get("order_id")): r for r in placements}
    for o in orders:
        r = ours.get(o["id"])
        if r and o["pending"] and o["filled"] == 0:
            worst -= Decimal(str(r.get("risk") or 0)) + Decimal(str(r.get("costs") or 0))
    r = max(worst - keep, Decimal(0)) if realised_net > 0 else Decimal(0)
    return {"realised_net": realised_net.quantize(Decimal("0.01")), "keep": keep.quantize(Decimal("0.01")),
            "worst_day": worst.quantize(Decimal("0.01")), "room": r.quantize(Decimal("0.01")),
            "giveback_pct": int(giveback() * 100)}
