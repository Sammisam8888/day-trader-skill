"""
When may Claude use a control command? Only on a named trigger that this
code verifies from the data. Claude's word is not evidence.

  trigger          verified as                                         allows
  ---------------  --------------------------------------------------  ------------------------------
  drawdown         day P&L <= -50% of the daily loss limit              halt, lower
                   (<= -75% for flatten)                                flatten
  stop_streak      3+ stops hit today and no target hit                 halt, lower
  vix_spike        India VIX up >= 15% on the day or >= 25              halt, lower
                   (up >= 25% for flatten)                              flatten
  shock_news       news market score <= -0.7 (<= -0.85 for flatten)     halt, lower, flatten
  chop             after 11:00: 3+ UP/DOWN flips in the last hour,      halt, lower
                   or 12 straight scans MIXED with |score| < 0.2
  data_broken      half or more of the markets failed to load           halt
  event_risk       a timed market event today (halt only within the     halt, lower
                   hour before it)
  sentiment_against  |sentiment bias| >= 0.5                            lower (size / sides only)
  trend_day        last 6 direction readings all UP (or all DOWN)       raise
                   with |score| >= 0.6, realised P&L > 0 and at least
                   one target hit today
  condition_cleared  30+ minutes since the halt and its trigger no     resume
                   longer holds
  official_source  source is an nseindia.com / bseindia.com page        calendar holiday / special session
  sourced          any non-empty source                                 calendar event

The owner (dashboard) needs no trigger.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Optional

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import session as sess                                   # noqa: E402

ALLOWS = {
    "halt_today": {"drawdown", "stop_streak", "vix_spike", "shock_news", "chop", "data_broken", "event_risk"},
    "flatten_now": {"drawdown", "vix_spike", "shock_news"},
    "lower": {"drawdown", "stop_streak", "vix_spike", "shock_news", "chop", "event_risk", "sentiment_against"},
    "raise": {"trend_day"},
    "resume_today": {"condition_cleared"},
    "calendar_official": {"official_source"},
    "calendar_event": {"sourced", "official_source"},
}


def facts(*, day_pnl: Optional[Decimal] = None, realised: Optional[Decimal] = None, orders: Optional[list] = None,
          markets: Optional[list] = None, now: Optional[datetime] = None) -> dict:
    """Everything the triggers check, gathered by code."""
    import sentiment
    now = now or sess.now()
    f: dict = {"now": now, "day_pnl": day_pnl, "realised": realised}
    stops = {str(r.get("order_id")) for r in common.events("stop_placed")}
    f["stops_hit"] = sum(1 for o in (orders or []) if o["id"] in stops and o["filled"] > 0)
    f["targets_hit"] = len(common.events("target_exit"))
    snap = sentiment.load(now)
    pos = (snap or {}).get("positioning") or {}
    f["vix"] = pos.get("vix") or {}
    f["news"] = (((snap or {}).get("news") or {}).get("market") or {}).get("score")
    f["bias"] = ((snap or {}).get("tilt") or {}).get("bias")
    f["events"] = [e for e in (((snap or {}).get("events") or {}).get("market") or []) if e.get("at")]
    try:
        import control
        f["events"] += [e for e in control.calendar_events(now.date()) if e.get("at") and not e.get("symbol")]
    except Exception:                                   # noqa: BLE001
        pass
    f["direction"] = [(r.get("ts"), r.get("state"), r.get("score")) for r in common.events("direction")]
    if markets is not None:
        f["markets_failed"] = (sum(1 for m in markets if "error" in m), len(markets))
    return f


def check(trigger: str, f: dict, *, strict: bool = False, near_event: bool = False) -> tuple:
    """(holds, evidence). strict = the flatten thresholds."""
    lim = common.base_limits()
    now = f["now"]
    if trigger == "drawdown":
        if f.get("day_pnl") is None:
            return False, "day P&L unknown"
        cut = -lim.daily_loss_limit * (Decimal("0.75") if strict else Decimal("0.5"))
        return f["day_pnl"] <= cut, f"day P&L {f['day_pnl']} vs {cut:.0f}"
    if trigger == "stop_streak":
        return f["stops_hit"] >= 3 and f["targets_hit"] == 0, f"{f['stops_hit']} stops, {f['targets_hit']} targets today"
    if trigger == "vix_spike":
        lvl, chg = float(f["vix"].get("level") or 0), float(f["vix"].get("change_pct") or 0)
        ok = chg >= 25 if strict else (chg >= 15 or lvl >= 25)
        return ok, f"VIX {lvl} ({chg:+.1f}%)" if lvl else "no VIX reading"
    if trigger == "shock_news":
        s = f.get("news")
        if s is None:
            return False, "no news read"
        return s <= (-0.85 if strict else -0.7), f"news score {s}"
    if trigger == "chop":
        if now.time() < time(11, 0):
            return False, "too early to call chop (before 11:00)"
        recent = [d for d in f["direction"] if d[0] and datetime.fromisoformat(d[0]) >= now - timedelta(minutes=60)]
        trend = [d[1] for d in recent if d[1] in ("UP", "DOWN")]
        flips = sum(1 for a, b in zip(trend, trend[1:]) if a != b)
        last12 = f["direction"][-12:]
        flat = len(last12) == 12 and all(d[1] == "MIXED" and abs(float(d[2] or 0)) < 0.2 for d in last12)
        return flips >= 3 or flat, f"{flips} flips in the last hour; {'60 min' if flat else 'no'} flat MIXED run"
    if trigger == "data_broken":
        bad, n = f.get("markets_failed") or (0, 0)
        return n > 0 and bad * 2 >= n, f"{bad}/{n} markets failed"
    if trigger == "event_risk":
        evs = f.get("events") or []
        if not evs:
            return False, "no timed event today"
        names = ", ".join(f"{e['what']} {e['at']}" for e in evs)
        if near_event:
            near = []
            for e in evs:
                hh, mm = (int(x) for x in str(e["at"]).split(":"))
                at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if at - timedelta(minutes=60) <= now <= at + timedelta(minutes=40):
                    near.append(e["what"])
            return bool(near), f"events today: {names}"
        return True, f"events today: {names}"
    if trigger == "sentiment_against":
        b = f.get("bias")
        return b is not None and abs(b) >= 0.5, f"sentiment bias {b}"
    if trigger == "trend_day":
        last6 = f["direction"][-6:]
        same = len(last6) == 6 and len({d[1] for d in last6}) == 1 and last6[0][1] in ("UP", "DOWN")
        strong = same and all(abs(float(d[2] or 0)) >= 0.6 for d in last6)
        r = f.get("realised")
        ok = strong and r is not None and r > 0 and f["targets_hit"] >= 1
        return ok, (f"direction {[d[1] for d in last6]} scores {[d[2] for d in last6]}, realised {r}, "
                    f"{f['targets_hit']} target(s)")
    return False, f"unknown trigger {trigger!r}"
