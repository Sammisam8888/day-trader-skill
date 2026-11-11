"""
Day-level layers on top of Claude's trade-by-trade decisions (V3 + the flip).

  window        no new entries inside TRADER_NO_ENTRY_WINDOWS (12:00-13:00:
                the lunch chop lost money in the back-test)
  losers        today's losing trades: our exchange stops that filled worse
                than the entry
  mirror        a paper trade opposite every real fill: same price, other
                side, stop one R away, target TRADER_TARGET_R x R. Valued on
                the same 5-minute candles (stop first), so at any moment we
                know whether "the opposite of what we did today" is winning
  flip          after TRADER_FLIP_AFTER (3) losers, if the mirror is in profit
                and ahead of the real book, the day flips: Claude is shown the
                exact opposite of each setup (named *_flip) and may only take
                those. After TRADER_FLIP_HALT (2) losing flips the day stops.
  stop          not flipped and 3 losers: no new entries today

Back-test (3 months, V3): the flip was roughly neutral (+Rs 2,855 vs +Rs
2,816); flipping after 2 losers was clearly worse. It is on by owner's
choice (TRADER_FLIP=true) and measured on every day it fires.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Dict, List, Optional

import common

FLIP_SUFFIX = "_flip"


def target_r() -> Decimal:
    try:
        return Decimal(common.setting("TRADER_TARGET_R", "2"))
    except Exception:                                   # noqa: BLE001
        return Decimal(2)


def allowed_setups() -> Optional[set]:
    raw = common.setting("TRADER_SETUPS", "")
    return {s.strip() for s in raw.split(",") if s.strip()} or None


def use_book() -> bool:
    return common.setting("TRADER_USE_BOOK", "true").lower() in ("1", "true", "yes")


def direction_enforced() -> bool:
    return common.setting("TRADER_DIRECTION_GATE", "advisory").lower() == "enforce" or \
        common.setting("TRADER_GATES", "advisory").lower() == "enforce"


def no_entry_window(now: datetime) -> Optional[str]:
    for part in common.setting("TRADER_NO_ENTRY_WINDOWS", "").split(","):
        if "-" not in part:
            continue
        a, b = part.strip().split("-", 1)
        try:
            ta = time(*(int(x) for x in a.split(":")))
            tb = time(*(int(x) for x in b.split(":")))
        except ValueError:
            continue
        if ta <= now.time() < tb:
            return f"no new entries {a}-{b} (the lunch chop lost money in the back-test)"
    return None


def _int(key: str, default: int) -> int:
    try:
        return int(common.setting(key, str(default)))
    except ValueError:
        return default


def losers(orders: List[dict], placements: List[dict]) -> Dict[str, int]:
    """Losing trades today (our filled stops worse than the entry), split into
    normal and flipped."""
    book = {o["id"]: o for o in orders}
    plans = {str(p.get("order_id")): p for p in placements}
    out = {"normal": 0, "flip": 0}
    for st in common.events("stop_placed"):
        so = book.get(str(st.get("order_id")))
        parent = plans.get(str(st.get("parent")))
        po = book.get(str(st.get("parent")))
        if not so or not parent or not po or so["filled"] <= 0 or not so.get("avg") or not po.get("avg"):
            continue
        long_ = parent.get("side", "B") == "B"
        pnl = (Decimal(str(so["avg"])) - Decimal(str(po["avg"]))) * (1 if long_ else -1)
        if pnl < 0:
            out["flip" if str(parent.get("setup") or "").endswith(FLIP_SUFFIX) else "normal"] += 1
    return out


def ensure_mirrors(orders: List[dict], placements: List[dict], now: datetime) -> None:
    """Open a paper mirror for every filled real entry that has none."""
    have = {str(r.get("parent")) for r in common.events("mirror_open")}
    book = {o["id"]: o for o in orders}
    tr = target_r()
    for p in placements:
        oid = str(p.get("order_id"))
        o = book.get(oid)
        if oid in have or not o or o["filled"] <= 0 or not o.get("avg"):
            continue
        if str(p.get("setup") or "").endswith(FLIP_SUFFIX):
            continue
        fill = Decimal(str(o["avg"]))
        r = abs(Decimal(str(p["entry"])) - Decimal(str(p["stop"])))
        if r <= 0:
            continue
        side = "S" if p.get("side", "B") == "B" else "B"
        stop = fill + r if side == "S" else fill - r
        target = fill - r * tr if side == "S" else fill + r * tr
        common.log("daymode", {"event": "mirror_open", "parent": oid, "symbol": p["symbol"], "side": side,
                               "qty": int(o["filled"]), "entry": str(fill), "stop": str(stop), "target": str(target),
                               "opened": str(o.get("time") or now.isoformat(timespec="seconds"))})


def _opened(rec: dict, day) -> Optional[datetime]:
    raw = str(rec.get("opened") or "")
    for fmt in ("%H:%M:%S %d-%m-%Y", "%d-%m-%Y %H:%M:%S"):
        try:
            import session as sess
            return datetime.strptime(raw, fmt).replace(tzinfo=sess.IST)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.fromisoformat(rec["ts"])
        except (KeyError, ValueError):
            return None


def mirror_value(insts: dict, day) -> Dict[str, Decimal]:
    """Closed and open value of today's mirrors, from the same candles."""
    import charges
    closed, open_ = Decimal(0), Decimal(0)
    for m in common.events("mirror_open"):
        sym = m["symbol"]
        if sym not in insts:
            continue
        _, candles, q = insts[sym]
        t0 = _opened(m, day)
        side, qty = m["side"], int(m["qty"])
        entry, stop, target = Decimal(m["entry"]), Decimal(m["stop"]), Decimal(m["target"])
        exit_px = None
        for c in candles:
            if c.opens_at.date() != day or (t0 and c.closes_at <= t0):
                continue
            if side == "B":
                if c.low <= stop:
                    exit_px = min(stop, c.open); break
                if c.high >= target:
                    exit_px = target; break
            else:
                if c.high >= stop:
                    exit_px = max(stop, c.open); break
                if c.low <= target:
                    exit_px = target; break
        px = exit_px if exit_px is not None else Decimal(str(q.get("ltp") or entry))
        gross = (px - entry) * qty * (1 if side == "B" else -1)
        buy, sell = (entry, px) if side == "B" else (px, entry)
        net = gross - charges.round_trip(buy * qty, sell * qty).total
        if exit_px is not None:
            closed += net
        else:
            open_ += net
    return {"closed": closed, "open": open_, "total": closed + open_}


def status(orders: List[dict], placements: List[dict], insts: dict, now: datetime, day_pnl: Decimal) -> dict:
    """The day mode Claude and the code both see this scan."""
    ensure_mirrors(orders, placements, now)
    lost = losers(orders, placements)
    mirror = mirror_value(insts, now.date())
    flipped_ev = common.events("flip_on")
    flipped = bool(flipped_ev)
    flip_on = common.setting("TRADER_FLIP", "false").lower() in ("1", "true", "yes")
    after, halt_after = _int("TRADER_FLIP_AFTER", 3), _int("TRADER_FLIP_HALT", 2)
    stop_after = _int("TRADER_STOP_AFTER_LOSSES", 0)
    if flip_on and not flipped and lost["normal"] >= after and mirror["total"] > 0 and mirror["total"] > day_pnl:
        common.log("daymode", {"event": "flip_on", "losers": lost["normal"], "mirror": str(mirror["total"]),
                               "day_pnl": str(day_pnl)})
        common.notify("trader: FLIP MODE", f"{lost['normal']} losing trades; the opposite side is up "
                      f"{mirror['total']:.0f}. Claude now trades the flips.", priority="high")
        flipped = True
    halted = None
    if flipped and lost["flip"] >= halt_after:
        halted = f"flip mode lost {lost['flip']} trades: no new entries today"
    elif not flipped and stop_after and lost["normal"] >= stop_after:
        halted = (f"{lost['normal']} losing trades today: no new entries"
                  + (f" (the opposite side is not winning either: {mirror['total']:+.0f})" if flip_on else ""))
    return {"flipped": flipped, "halted": halted, "losers": lost, "mirror_pnl": str(mirror["total"].quantize(Decimal("0.01"))),
            "flip_enabled": flip_on, "flip_after": after, "flip_halt_after": halt_after, "stop_after": stop_after}
