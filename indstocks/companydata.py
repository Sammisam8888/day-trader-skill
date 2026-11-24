"""
Company data from free, official exchange sources (NSE's public site), for the
AI's context and the Desk: today's bulk and block deals, the latest quarterly
results, and the shareholding pattern (promoter / FII / DII / public) of every
stock on the tape. Pulled a few times a day, not per scan.

Not used, and why (owner's list, 24 Nov): SEC EDGAR and WhaleWisdom cover US
companies only; Yahoo Finance's data needs no second source next to Shoonya's
live feed; Screener.in and Trendlyne do not permit copying their data into
another database, so they stay tools for the owner's own reading.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import session as sess  # noqa: E402

DIR = common.STATE_DIR / "company"


def _dt(s: str) -> Optional[datetime]:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(str(s).strip().title(), fmt)
        except ValueError:
            continue
    return None


def _num(x) -> Optional[float]:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def deals(get: Callable, symbols: List[str]) -> dict:
    """Yesterday's / today's bulk and block deals: ours, and the biggest overall."""
    d = get("snapshot-capital-market-largedeal") or {}
    out = {"as_on": d.get("as_on_date"), "ours": [], "biggest": []}
    rows = []
    for kind, key in (("bulk", "BULK_DEALS_DATA"), ("block", "BLOCK_DEALS_DATA")):
        for r in d.get(key) or []:
            qty, px = _num(r.get("qty")), _num(r.get("watp"))
            val = qty * px / 1e7 if qty and px else None          # Rs crore
            rows.append({"kind": kind, "symbol": str(r.get("symbol") or "").upper(), "side": r.get("buySell"),
                         "client": str(r.get("clientName") or "")[:60], "qty": qty, "price": px,
                         "value_cr": round(val, 2) if val else None})
    out["ours"] = [r for r in rows if r["symbol"] in symbols]
    out["biggest"] = sorted((r for r in rows if r["value_cr"]), key=lambda r: -r["value_cr"])[:8]
    return out


def results(get: Callable, symbols: List[str], now: datetime) -> Dict[str, dict]:
    """The latest quarterly results filing per symbol (date and quarter; NSE's list
    has no figures) and how long ago it was."""
    rows = get("corporates-financial-results?index=equities&period=Quarterly") or []
    best: Dict[str, dict] = {}
    for r in rows if isinstance(rows, list) else []:
        sym = str(r.get("symbol") or "").upper()
        if sym not in symbols:
            continue
        t = _dt(r.get("broadCastDate") or r.get("filingDate") or "")
        if not t:
            continue
        if sym not in best or t > best[sym]["_t"]:
            best[sym] = {"_t": t, "quarter_to": r.get("toDate"), "relating_to": r.get("relatingTo"),
                         "filed": t.strftime("%Y-%m-%d"), "audited": r.get("audited")}
    out = {}
    for sym, v in best.items():
        v.pop("_t")
        v["days_ago"] = (now.replace(tzinfo=None) - datetime.strptime(v["filed"], "%Y-%m-%d")).days
        out[sym] = v
    return out


def holdings(get: Callable, sym: str) -> Optional[dict]:
    """Promoter and public holding, latest quarter and the change from the one before."""
    rows = get(f"corporate-share-holdings-master?index=equities&symbol={sym}") or []
    pts = []
    for r in rows if isinstance(rows, list) else []:
        t, pr, pub = _dt(r.get("date") or ""), _num(r.get("pr_and_prgrp")), _num(r.get("public_val"))
        if t and pr is not None:
            pts.append((t, pr, pub))
    if not pts:
        return None
    pts.sort()
    t, pr, pub = pts[-1]
    prev = pts[-2][1] if len(pts) > 1 else None
    return {"as_of": t.strftime("%Y-%m-%d"), "promoter_pct": pr, "public_pct": pub,
            "promoter_change": round(pr - prev, 2) if prev is not None else None}


def symbols() -> List[str]:
    try:
        import tape
        return tape.symbols()
    except Exception:                                    # noqa: BLE001
        return common.symbols()


def refresh(get: Optional[Callable] = None, now: Optional[datetime] = None, pause: float = 0.4) -> dict:
    if get is None:
        import sentiment
        get = sentiment.nse_json
    now = now or sess.now()
    syms = [s for s in symbols() if s not in ("NIFTYBEES", "BANKBEES")]
    out = {"at": now.isoformat(timespec="seconds"), "deals": None, "results": {}, "holdings": {}, "errors": []}
    for name, fn in (("deals", lambda: deals(get, syms)), ("results", lambda: results(get, syms, now))):
        try:
            out[name] = fn()
        except Exception as e:                           # noqa: BLE001
            out["errors"].append(f"{name}: {str(e)[:120]}")
    for s in syms:
        try:
            h = holdings(get, s)
            if h:
                out["holdings"][s] = h
        except Exception as e:                           # noqa: BLE001
            out["errors"].append(f"holdings {s}: {str(e)[:80]}")
        time.sleep(pause)
    DIR.mkdir(parents=True, exist_ok=True)
    p = DIR / "latest.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, default=str))
    tmp.replace(p)
    common.log("company", {"event": "company_data", "deals_ours": len((out["deals"] or {}).get("ours") or []),
                           "results": len(out["results"]), "holdings": len(out["holdings"]), "errors": out["errors"][:5]})
    return out


def load(max_age_h: float = 36) -> dict:
    try:
        d = json.loads((DIR / "latest.json").read_text())
        if (sess.now() - datetime.fromisoformat(d["at"])).total_seconds() <= max_age_h * 3600:
            return d
    except (OSError, ValueError, KeyError):
        pass
    return {}


def for_symbol(sym: str, d: Optional[dict] = None) -> dict:
    d = d if d is not None else load()
    out = {}
    if (d.get("holdings") or {}).get(sym):
        out["holding"] = d["holdings"][sym]
    if (d.get("results") or {}).get(sym):
        out["results"] = d["results"][sym]
    deal = [x for x in ((d.get("deals") or {}).get("ours") or []) if x["symbol"] == sym]
    if deal:
        out["deals"] = deal
    return out


def for_prompt() -> dict:
    """Compact per-stock facts for the AI: results within 10 days, promoter changes
    of 1 point or more, and any bulk/block deal in our stocks."""
    d = load()
    if not d:
        return {}
    out = {}
    for sym in set(d.get("results") or {}) | set(d.get("holdings") or {}):
        f = for_symbol(sym, d)
        keep = {}
        r = f.get("results")
        if r and r.get("days_ago") is not None and r["days_ago"] <= 10:
            keep["results_filed"] = f"{r['filed']} ({r.get('relating_to')}, {r['days_ago']}d ago)"
        h = f.get("holding")
        if h and h.get("promoter_change") is not None and abs(h["promoter_change"]) >= 1:
            keep["promoter_change_pts"] = h["promoter_change"]
        if f.get("deals"):
            keep["big_deals"] = [f"{x['kind']} {x['side']} {x['value_cr']} cr by {x['client']}" for x in f["deals"]][:3]
        if keep:
            out[sym] = keep
    return {"as_of": d.get("at"), "stocks": out} if out else {"as_of": d.get("at"), "stocks": {}}


PROBE = ("snapshot-capital-market-largedeal",
         "corporates-financial-results?index=equities&period=Quarterly",
         "corporate-share-holdings-master?index=equities&symbol=HDFCBANK")


def probe() -> None:
    import sentiment
    for api in PROBE:
        try:
            d = sentiment.nse_json(api)
        except Exception as e:                           # noqa: BLE001
            print(api, "ERROR", str(e)[:200])
            continue
        if isinstance(d, dict):
            print(api, "dict keys:", list(d)[:12])
            v = next((x for x in d.values() if isinstance(x, list) and x), None)
            print("  row:", json.dumps(v[0])[:600] if v else json.dumps(d)[:600])
        elif isinstance(d, list):
            print(api, "list", len(d))
            print("  keys:", sorted(d[0]) if d else "empty")
            hit = [x for x in d if str(x.get("symbol", "")).upper() in ("HDFCBANK", "RELIANCE", "TCS")][:2]
            print("  ours:", json.dumps(hit)[:700])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        probe()
    else:
        n = sess.now()
        if n.weekday() < 5:
            print(json.dumps({k: (v if k != "holdings" else len(v)) for k, v in refresh().items()}, default=str)[:2000])
