"""
What the dashboard's Strategies tab shows (owner, 26 Nov): every strategy's
wallet, today's result, its trades, its log and its learnings, day by day, and
the health of every stage of the pipeline that produces them. Read-only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import journal  # noqa: E402
import session as sess  # noqa: E402

AI = journal.AI


def _read_jsonl(path) -> List[dict]:
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _json(path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def kind_of(name: str) -> str:
    if name == AI:
        return "ai_trader"
    if name == "ai_agent":
        return "ai_agent"
    if name.endswith("~fut"):
        return "futures"
    if name.endswith("~opt"):
        return "options"
    if name.endswith("@ai"):
        return "ai_tuned"
    if name.endswith("_inv"):
        return "inverted"
    return "fixed"


def recipe_of(name: str) -> str:
    base = name.split("~")[0].split("@")[0].replace("_inv", "")
    if base.startswith("orb"):
        return "orb"
    if base.startswith("ema"):
        return "ema_cross"
    return base


def _bench_days() -> List[str]:
    import paper
    try:
        return sorted(f.name[:10] for f in paper.PAPER_DIR.glob("*-summary.json"))
    except OSError:
        return []


def _bench_summary(day: str) -> dict:
    import paper
    return (_json(paper.PAPER_DIR / f"{day}-summary.json") or {}).get("models") or {}


def _ai_day(day: str) -> dict:
    """The AI trader's day from the main log: net after charges, trades, wins."""
    rows = _read_jsonl(common.STATE_DIR / f"{day}.jsonl")
    pnl = [r for r in rows if r.get("event") == "pnl"]
    res, seen = [], set()
    for r in rows:
        if r.get("event") == "trade_result" and r.get("order_id") not in seen:
            seen.add(r.get("order_id"))
            res.append(r)
    return {"net": float(pnl[-1]["net"]) if pnl else 0.0, "trades": len(res),
            "wins": sum(1 for r in res if r.get("win")), "placed": sum(1 for r in rows if r.get("event") == "placed"),
            "target_hit": next((r.get("ts", "")[11:16] for r in rows if r.get("event") == "day_target_reached"
                                or (r.get("event") == "day_locked" and "target" in str(r.get("reason", "")))),
                               None)}


def headline(name: str, day: Optional[str] = None) -> Optional[dict]:
    """The latest learning's headline (or the given day's)."""
    d = journal.base() / journal._safe(name) / "learnings"
    try:
        files = sorted(d.glob("*.md"))
    except OSError:
        return None
    if day:
        files = [f for f in files if f.stem == day]
    if not files:
        return None
    first = next((l for l in files[-1].read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()), "")
    return {"day": files[-1].stem, "text": first.lstrip("# ").strip()[:200]}


def overview(day: Optional[str] = None) -> dict:
    """Every strategy: wallet, target, today's result, and its day-by-day history."""
    import paper
    day = day or sess.now().strftime("%Y-%m-%d")
    wallet = float(paper.capital())
    target = float(paper.day_target())
    days = _bench_days()
    hist: Dict[str, List[dict]] = {}
    for d in days[-30:]:
        for m, r in _bench_summary(d).items():
            hist.setdefault(m, []).append({"day": d, "net": float(r.get("net") or 0), "trades": r.get("trades", 0)})
    today = _bench_summary(day)
    rows = []
    try:
        import fno
        sleeves = fno.models_with_sleeves() if fno.enabled() else []
        fno_wallet = float(fno.capital())
    except Exception:                                    # noqa: BLE001
        sleeves, fno_wallet = [], wallet
    for m in paper.books() + sleeves:
        r = today.get(m) or {}
        rows.append({"name": m, "kind": kind_of(m), "recipe": recipe_of(m),
                     "wallet": fno_wallet if "~" in m else wallet, "target": target,
                     "today": {"net": float(r.get("net") or 0), "trades": r.get("trades", 0), "wins": r.get("wins", 0),
                               "open": r.get("open_trades", 0), "target_hit": r.get("target_hit")},
                     "history": hist.get(m, []), "learning": headline(m)})
    pip = _json(common.STATE_DIR / "pip.json") or {}
    a = _ai_day(day)
    rows.append({"name": AI, "kind": "ai_trader", "recipe": "ai", "wallet": float(common.setting("TRADER_PAPER_CAPITAL", "100000")),
                 "target": float(common.setting("TRADER_DAY_TARGET", "1000")),
                 "today": {"net": a["net"], "trades": a["trades"], "wins": a["wins"], "open": 0, "target_hit": a["target_hit"]},
                 "history": [{"day": d, "net": float(v), "trades": None} for d, v in sorted(pip.items())][-30:],
                 "learning": headline(AI)})
    for r in rows:
        h = r["history"]
        r["days"] = len(h)
        r["total"] = round(sum(x["net"] for x in h), 2)
        r["green_days"] = sum(1 for x in h if x["net"] > 0)
        r["target_days"] = sum(1 for x in h if x["net"] >= r["target"] > 0)
    rows.sort(key=lambda r: -r["today"]["net"])
    return {"day": day, "wallet": wallet, "target": target, "strategies": rows,
            "total_today": round(sum(r["today"]["net"] for r in rows), 2)}


ROW = re.compile(r"^\| (.*) \|$")


def log_rows(name: str, day: str) -> List[dict]:
    """The strategy's journal table for the day as rows."""
    try:
        lines = journal.log_file(name, day).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split(" | ")]
        if len(cells) != len(journal.COLS) or cells[0] in ("time", "---") or set(cells[0]) <= {"-"}:
            continue
        out.append(dict(zip(("time", "event", "stock", "side", "qty", "price", "stop", "target", "net", "note"), cells)))
    return out


def detail(name: str, day: Optional[str] = None) -> dict:
    """One strategy on one day: its trades, log, learning, knobs and history."""
    import paper
    day = day or sess.now().strftime("%Y-%m-%d")
    folder = journal.base() / journal._safe(name)
    try:
        log_days = sorted(f.stem for f in (folder / "logs").glob("*.md"))
    except OSError:
        log_days = []
    trades: List[dict] = []
    if name == AI or "~" in name:
        trades = [r for r in log_rows(name, day)]
    else:
        opens = {}
        for r in _read_jsonl(paper.PAPER_DIR / f"{day}.jsonl"):
            if r.get("model") != name:
                continue
            if r.get("ev") == "open":
                opens[r["symbol"]] = r
            elif r.get("ev") == "close":
                o = opens.pop(r["symbol"], {})
                trades.append({"stock": r["symbol"], "side": "BUY" if r["side"] == "B" else "SELL", "qty": r["qty"],
                               "opened": _hhmm(o.get("t")), "closed": _hhmm(r.get("t")), "entry": r.get("entry"),
                               "exit": r.get("exit"), "stop": o.get("stop"), "target": o.get("target"),
                               "net": float(r.get("net") or 0), "why": r.get("why"), "followed": o.get("src")})
        for sym, o in opens.items():
            trades.append({"stock": sym, "side": "BUY" if o["side"] == "B" else "SELL", "qty": o["qty"],
                           "opened": _hhmm(o.get("t")), "closed": None, "entry": o.get("entry"), "exit": None,
                           "stop": o.get("stop"), "target": o.get("target"), "net": None, "why": "open",
                           "followed": o.get("src")})
    agent = None
    if name == paper.AGENT:
        rows_ = paper.agent_decisions(day)
        agent = {"decisions": [{k: r.get(k) for k in ("at", "day_read", "follow", "exit", "exit_all", "stand_aside", "note")}
                               for r in rows_][-30:]}
    knobs = None
    if name != AI and "~" not in name and name != paper.AGENT:
        r = recipe_of(name)
        if r in paper.RECIPES:
            if name.endswith("@ai"):
                p = _json(paper.PAPER_DIR / f"params-{day}.json") or {}
                set_ = (p.get("models") or {}).get(r) or {}
                knobs = {"values": paper.clamp(r, set_), "why": set_.get("why"), "set_at": p.get("at"),
                         "day_read": p.get("day_read")}
            knobs = knobs or {"values": None}
            knobs["ranges"] = {k: [lo, hi] for k, (d, lo, hi, _) in paper.knobs_for(r).items()}
            knobs["doc"] = (paper.RECIPES[r].__doc__ or "").strip()
    try:
        log_md = journal.log_file(name, day).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_md = ""
    try:
        learn_md = journal.learn_file(name, day).read_text(encoding="utf-8", errors="replace")
    except OSError:
        learn_md = ""
    ov = next((r for r in overview(day)["strategies"] if r["name"] == name), None)
    return {"name": name, "day": day, "kind": kind_of(name), "recipe": recipe_of(name), "summary": ov,
            "trades": trades, "log_rows": log_rows(name, day), "log_md": log_md, "learning_md": learn_md,
            "days": sorted(set(log_days) | {h["day"] for h in (ov or {}).get("history", [])}), "knobs": knobs,
            "agent": agent}


def _hhmm(t) -> Optional[str]:
    try:
        return datetime.fromtimestamp(float(t), sess.IST).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


# ------------------------------------------------------------------ pipeline health

_SYNC = {"t": 0.0, "v": None}


def _data_sync() -> Optional[str]:
    """When the nightly GitHub sync last committed (public API, cached 15 min)."""
    if time.time() - _SYNC["t"] < 900:
        return _SYNC["v"]
    _SYNC["t"] = time.time()
    try:
        import urllib.request
        req = urllib.request.Request("https://api.github.com/repos/Sammisam8888/day-trader-skill/branches/data-sync",
                                     headers={"User-Agent": "indstocks-dashboard", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        _SYNC["v"] = d["commit"]["commit"]["committer"]["date"]
    except Exception:                                    # noqa: BLE001
        pass
    return _SYNC["v"]


def pipeline(now: Optional[datetime] = None) -> dict:
    """Each stage: when it last did its job, and whether that is on time."""
    import paper
    now = now or sess.now()
    day = now.strftime("%Y-%m-%d")
    hm = now.hour * 60 + now.minute
    trading = now.weekday() < 5
    rows = _read_jsonl(common.STATE_DIR / f"{day}.jsonl")

    def last(pred) -> Optional[str]:
        for r in reversed(rows):
            if pred(r):
                return r.get("ts")
        return None

    def age_s(ts: Optional[str]) -> Optional[float]:
        try:
            return (now - datetime.fromisoformat(ts)).total_seconds()
        except (TypeError, ValueError):
            return None

    def stage(key, name, what, ts, *, window=None, max_age=None, due_after=None):
        """window=(start,end) minutes when it should be fresh within max_age seconds;
        due_after=minute after which a once-a-day job should have run today."""
        a = age_s(ts)
        if not trading:
            st = "idle"
        elif window and window[0] <= hm < window[1]:
            st = "ok" if a is not None and a <= max_age else ("down" if a is None else "late")
        elif due_after is not None and hm >= due_after:
            st = "ok" if ts and str(ts)[:10] == day else "late"
        elif window or due_after is not None:
            st = "idle" if not ts or str(ts)[:10] != day else "ok"
        else:
            st = "ok" if ts else "down"
        return {"key": key, "name": name, "what": what, "last": ts, "age_s": a, "status": st}

    try:
        import shoonya
        tok = _json(shoonya.TOKEN_FILE) or {}
    except Exception:                                    # noqa: BLE001
        tok = {}
    live = _json(common.STATE_DIR / "tape" / "live.json") or {}
    tape_ts = datetime.fromtimestamp(live["at"], sess.IST).isoformat(timespec="seconds") if live.get("at") else None
    summ = _json(paper.PAPER_DIR / f"{day}-summary.json") or {}
    bench_ts = datetime.fromtimestamp(summ["at"], sess.IST).isoformat(timespec="seconds") if summ.get("at") else None
    params = _json(paper.PAPER_DIR / f"params-{day}.json") or {}
    comp = _json(common.STATE_DIR / "company" / "latest.json") or {}
    pip = _json(common.STATE_DIR / "pip.json") or {}
    learn_ev = last(lambda r: r.get("event") == "learning_done")
    strat_ev = last(lambda r: r.get("event") == "strategy_learnings")
    sync = _data_sync()
    broker = "paper" if _paper() else "live"
    stages = [
        stage("login", "Login", "Shoonya session for the day", tok.get("at"), due_after=8 * 60 + 45),
        stage("tape", "Tape", "Live ticks + order book, every second", tape_ts, window=(9 * 60 + 6, 15 * 60 + 30), max_age=30),
        stage("company", "Company data", "Deals, results, shareholding (NSE)", comp.get("at"), due_after=8 * 60 + 45),
        stage("ai", "AI scans", "The AI trader decides every 5 min", last(lambda r: r.get("event") == "run_end"),
              window=(9 * 60 + 35, 14 * 60 + 35), max_age=600),
        stage("bench", "Paper bench", "35 rule models + F&O sleeves on live prices", bench_ts, window=(9 * 60 + 16, 15 * 60), max_age=60),
        stage("tuner", "Tuner", "The AI sets the @ai knobs", params.get("at"), window=(9 * 60 + 25, 14 * 60 + 30), max_age=2100),
        stage("agent", "AI agent", "Picks which models its Rs 1L wallet follows, every 15 min",
              ((paper.agent_decisions(day) or [{}])[-1]).get("at"), window=(9 * 60 + 40, 14 * 60 + 30), max_age=1200),
        stage("orders", "Orders and fills", f"Execution ({broker} broker)",
              last(lambda r: r.get("event") in ("paper_order", "placed", "filled")), due_after=None),
        stage("monitor", "Fill monitor", "Stops, trailing lock, target, P&L", last(lambda r: r.get("event") == "pnl"),
              window=(9 * 60 + 15, 15 * 60), max_age=180),
        stage("flatten", "Flatten", "Everything closed by 14:50",
              last(lambda r: r.get("job") == "flatten"), due_after=14 * 60 + 52),
        stage("pip", "PIP scorecard", "Day's net, 14-day streak", (day + "T15:50:00") if day in pip else None,
              due_after=15 * 60 + 55),
        stage("lessons", "AI trader review", "Nightly lessons", learn_ev, due_after=16 * 60 + 5),
        stage("learnings", "Strategy learnings", "Per-strategy learnings (16:10)", strat_ev, due_after=16 * 60 + 40),
        stage("sync", "Data sync", "Nightly copy to GitHub (00:00)", sync, due_after=None),
    ]
    for s in stages:
        if s["key"] == "orders":
            s["status"] = "ok" if s["last"] else ("idle" if not trading or hm < 9 * 60 + 35 else "late")
        if s["key"] == "sync":
            a = s["age_s"]
            s["status"] = "ok" if a is not None and a < 30 * 3600 else ("down" if a is None else "late")
    return {"at": now.isoformat(timespec="seconds"), "broker": broker, "stages": stages}


def _paper() -> bool:
    try:
        import shoonya
        return shoonya.paper_mode()
    except Exception:                                    # noqa: BLE001
        return False
