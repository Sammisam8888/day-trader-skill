"""
Profit lock: once a trade has shown real profit, it is not allowed to give
it back. Checked by the fill monitor on every tick (seconds), with no model
call in the way.

  arm     the trade's open profit AFTER charges reaches TRADER_LOCK_ARM_RS
          (Rs 40). From then on its best net profit is remembered.
  floor   max(TRADER_LOCK_MIN_RS (Rs 5), best x TRADER_LOCK_KEEP_PCT (50%)).
          A trade that showed +70 is closed at about +35; one that showed
          +300 at about +150. An armed trade never ends below +Rs 5.
  exit    net profit at or under the floor: cancel the stop and close with a
          marketable order immediately (the fill monitor chases it to a fill).
  stop    on arming, the exchange stop moves to breakeven after charges, so
          the profit is protected even if this server dies.

13 Nov: three shorts were +214 open at 12:21 and closed at +115 gross /
+17 net on their stops. This exists so that cannot happen again.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

import charges as charges_mod
import common
from market import to_tick


def enabled() -> bool:
    return common.setting("TRADER_LOCK", "true").lower() in ("1", "true", "yes")


def _d(key: str, default: str) -> Decimal:
    try:
        return Decimal(common.setting(key, default))
    except Exception:                                   # noqa: BLE001
        return Decimal(default)


def arm_rs() -> Decimal:
    return _d("TRADER_LOCK_ARM_RS", "40")


def keep() -> Decimal:
    return _d("TRADER_LOCK_KEEP_PCT", "50") / 100


def min_rs() -> Decimal:
    return _d("TRADER_LOCK_MIN_RS", "5")


def net_open(side: str, avg: Decimal, qty: int, ltp: Decimal) -> Decimal:
    """Open profit if closed at ltp now, after every charge of the round trip."""
    gross = (ltp - avg) * qty if side == "B" else (avg - ltp) * qty
    buy, sell = (avg * qty, ltp * qty) if side == "B" else (ltp * qty, avg * qty)
    return gross - charges_mod.round_trip(buy, sell).total


def breakeven_price(side: str, avg: Decimal, qty: int, tick: Decimal) -> Decimal:
    """The exit price at which the round trip nets zero after charges."""
    per_share = charges_mod.round_trip(avg * qty, avg * qty).total / qty
    if side == "B":
        return to_tick(avg + per_share, tick, mode="up")
    return to_tick(avg - per_share, tick, mode="down")


def floor(best: Decimal) -> Decimal:
    return max(min_rs(), best * keep())


def check(best: Optional[Decimal], net: Decimal):
    """(new_best, armed, exit_reason or None) for one trade at this tick."""
    if best is None and net < arm_rs():
        return None, False, None
    best = net if best is None else max(best, net)
    f = floor(best)
    if net <= f:
        return best, True, f"profit lock: net {net:.0f} fell to the floor {f:.0f} (best {best:.0f})"
    return best, True, None


# ------------------------------------------------------------------ memory
# The best net profit per trade, kept on disk so a fill-monitor restart
# does not forget a lock that was armed.

def _path():
    import session as sess
    return common.STATE_DIR / "lock" / f"best-{sess.now().strftime('%Y-%m-%d')}.json"


def load() -> dict:
    try:
        return {k: Decimal(v) for k, v in json.loads(_path().read_text()).items()}
    except (OSError, ValueError, ArithmeticError):
        return {}


def save(best: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: str(v) for k, v in best.items()}))
        tmp.replace(p)
    except OSError:
        pass


# ------------------------------------------------------------------ the day
# The same rule for the whole day: once the day's net (realised + open, after
# charges) has reached TRADER_DAYLOCK_ARM_RS (Rs 150), falling back to
# TRADER_DAYLOCK_KEEP_PCT (50%) of its best closes everything and stops new
# entries for the rest of the day. 13 Nov: +300 net at its best, ended -51.

def day_enabled() -> bool:
    return common.setting("TRADER_DAYLOCK", "true").lower() in ("1", "true", "yes")


def day_check(best: Optional[Decimal], net: Decimal):
    """(new_best, reason to lock the day or None)."""
    arm = _d("TRADER_DAYLOCK_ARM_RS", "150")
    best = net if best is None else max(best, net)
    if best < arm:
        return best, None
    f = max(min_rs(), best * _d("TRADER_DAYLOCK_KEEP_PCT", "50") / 100)
    if net <= f:
        return best, f"day lock: day net {net:.0f} fell to {f:.0f}, half of its best {best:.0f}"
    return best, None
