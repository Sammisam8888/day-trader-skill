"""
Real-time layer: react within seconds instead of waiting for the next
5-minute scan.

  price shock   the fill monitor reads every symbol's price every few
                seconds; a move of TRADER_SHOCK_PCT (1%) or more within 5
                minutes logs a price_shock, alerts the phone and starts a
                trading scan at once. It catches news-driven moves even
                before the headline is out (L&T, 12 Nov 14:00).
  news alert    newswatch.py logs a news_alert for material headlines and
                starts a scan the same way.
  breaking      what Claude is shown each scan: the last 30 minutes of shocks
                and alerts.
  internals     NSE's all-indices snapshot: Nifty 50 advances/declines and
                every sector index's move, cached for two minutes.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional

import common
import session as sess

WINDOW = timedelta(minutes=5)
COOLDOWN = timedelta(minutes=15)
_last_kick = {"t": 0.0}


def shock_pct() -> Decimal:
    try:
        return Decimal(common.setting("TRADER_SHOCK_PCT", "1.0"))
    except Exception:                                   # noqa: BLE001
        return Decimal("1.0")


def kick_scan(reason: str) -> bool:
    """Start a trading scan now (systemd one-shot), at most once a minute."""
    if common.setting("TRADER_KICK_SCAN", "true").lower() not in ("1", "true", "yes"):
        return False
    if time.monotonic() - _last_kick["t"] < 60:
        return False
    s = sess.session_for(sess.now())
    if s is None or not s.accepts_new_entries(sess.now()):
        return False
    _last_kick["t"] = time.monotonic()
    try:
        subprocess.run(["systemctl", "start", "--no-block", "auto.service"], timeout=10,
                       capture_output=True)
        common.log("realtime", {"event": "scan_kicked", "reason": reason[:200]})
        return True
    except Exception:                                   # noqa: BLE001
        return False


def watch_prices(ltps: Dict[str, Decimal], names: Dict[str, str], now: datetime, state: dict) -> list:
    """Feed one round of prices; returns the shocks found (also logged)."""
    hist = state.setdefault("px_hist", {})
    alerted = state.setdefault("shock_at", {})
    found = []
    for tok, px in ltps.items():
        if not px:
            continue
        q = hist.setdefault(tok, deque())
        q.append((now, Decimal(str(px))))
        while q and now - q[0][0] > WINDOW + timedelta(seconds=30):
            q.popleft()
        old = next((p for t, p in q if now - t <= WINDOW), None)
        if old is None or old == 0:
            continue
        move = (Decimal(str(px)) / old - 1) * 100
        if abs(move) < shock_pct():
            continue
        last = alerted.get(tok)
        if last and now - last < COOLDOWN:
            continue
        alerted[tok] = now
        sym = names.get(tok, tok)
        rec = {"event": "price_shock", "symbol": sym, "move_pct": str(round(move, 2)),
               "from": str(old), "to": str(px), "minutes": 5}
        common.log("realtime", rec)
        common.notify(f"Price shock: {sym} {move:+.2f}% in 5 min", f"{old} -> {px}", priority="high")
        kick_scan(f"{sym} moved {move:+.2f}% in 5 minutes")
        found.append(rec)
    return found


def breaking(now: datetime, minutes: int = 30) -> list:
    """Shocks and news alerts from the last `minutes`, newest first, for Claude."""
    out = []
    for ev in ("price_shock", "news_alert"):
        for r in common.events(ev):
            try:
                t = datetime.fromisoformat(r["ts"])
            except (KeyError, ValueError):
                continue
            if now - t <= timedelta(minutes=minutes):
                out.append({k: r.get(k) for k in ("ts", "event", "symbol", "move_pct", "score", "headline",
                                                  "source", "why") if r.get(k) is not None})
    return sorted(out, key=lambda r: r["ts"], reverse=True)[:12]


SECTORS = ("NIFTY 50", "NIFTY BANK", "NIFTY IT", "NIFTY AUTO", "NIFTY METAL", "NIFTY FMCG", "NIFTY PHARMA",
           "NIFTY REALTY", "NIFTY ENERGY", "NIFTY INFRASTRUCTURE", "NIFTY FINANCIAL SERVICES", "NIFTY PSU BANK",
           "NIFTY PRIVATE BANK", "INDIA VIX")


def internals(get=None, max_age_s: int = 120) -> Optional[dict]:
    """Nifty 50 breadth and sector moves from NSE's all-indices feed, cached."""
    if common.setting("TRADER_INTERNALS", "true").lower() not in ("1", "true", "yes"):
        return None
    path = common.STATE_DIR / "sentiment" / "internals.json"
    try:
        cached = json.loads(path.read_text())
        if time.time() - cached.get("_t", 0) < max_age_s:
            return cached
    except (OSError, ValueError):
        pass
    if get is None:
        import sentiment
        get = sentiment.nse_json
    try:
        data = get("allIndices")
    except Exception:                                   # noqa: BLE001
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    out = {"_t": time.time(), "sectors": {}}
    for r in rows:
        name = str(r.get("index") or r.get("indexSymbol") or "").upper()
        if name in SECTORS:
            try:
                out["sectors"][name] = round(float(r.get("percentChange") or r.get("variation") or 0), 2)
            except (TypeError, ValueError):
                pass
        if name == "NIFTY 50":
            for k in ("advances", "declines", "unchanged"):
                try:
                    out[k] = int(float(r.get(k) or 0))
                except (TypeError, ValueError):
                    pass
    if out.get("advances") is not None and out.get("declines") is not None:
        a, d_ = out["advances"], out["declines"]
        out["breadth"] = f"{a} up / {d_} down" + (" (narrow: most stocks falling)" if d_ > a else "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out))
    except OSError:
        pass
    return out
