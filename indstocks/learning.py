"""
Learning loop: every closed trade is reviewed by Claude after the close,
and what it teaches is carried into every later decision.

  1. dossier   each trade rebuilt from the order book (the truth): entry fill,
               every exit fill and how it exited (stop, target, profit lock,
               Claude's exit, flatten), the plan and Claude's reason for it,
               every stop/target move and why, and from the candles the best
               and worst price while it was open: how much profit it showed
               and how much it gave back.
  2. review    Claude, as a trading coach, writes a post-mortem per trade and
               proposes lessons: concrete, conditional rules ("when X, do Y"),
               each tied to the trades that taught it.
  3. lessons   a store with evidence counts. A lesson confirmed again gains
               support; one the day contradicts loses it and is retired when
               the evidence turns against it. At most MAX_ACTIVE are active.
  4. use       active lessons are in STATE.lessons at every scan; Claude cites
               the ids it applied (decision field "lessons"), so the next
               review can judge whether following them paid.

Lessons are advice to Claude, never permissions: the owner's limits and the
code's checks still decide what is allowed.

Run nightly (learning.timer, 15:50) and with --day YYYY-MM-DD to review a
past day. With an empty store it first reviews the earlier logged days.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import charges as charges_mod                            # noqa: E402
import common                                            # noqa: E402
import session as sess                                   # noqa: E402

JOB = "learning"
MAX_ACTIVE = 20
MAX_NEW_PER_DAY = 4

EXIT_KIND = {"stop/": "stop", "tgt/": "target", "lock/": "profit lock", "exit/": "Claude exit",
             "flat/": "flatten", "brk/": "stop breached", "man/": "manual guard", "ctl/": "control",
             "chat/": "chat"}


def D(x, default="0") -> Decimal:
    try:
        return Decimal(str(x if x not in (None, "") else default))
    except ArithmeticError:
        return Decimal(default)


def store_dir() -> Path:
    return common.STATE_DIR.parent / "learning"


def lessons_path() -> Path:
    return store_dir() / "lessons.json"


def journal_path(day: str) -> Path:
    return store_dir() / f"journal-{day}.json"


# ------------------------------------------------------------------ dossiers

def _t(o: dict) -> Optional[datetime]:
    try:
        return datetime.strptime(o.get("time") or "", "%H:%M:%S %d-%m-%Y").replace(tzinfo=sess.IST)
    except ValueError:
        return None


def kind_of(remarks: str) -> str:
    for k, v in EXIT_KIND.items():
        if str(remarks or "").startswith(k):
            return v
    return "by hand"


def trades_from_book(placements: List[dict], orders: List[dict]) -> List[dict]:
    """One record per filled entry, its exits matched first-in first-out
    against the filled closing orders on the same stock."""
    entry_ids = {str(p.get("order_id")) for p in placements}
    book = {o["id"]: o for o in orders}
    closes: Dict[tuple, list] = {}
    for o in sorted((o for o in orders if o["filled"] > 0 and o["id"] not in entry_ids),
                    key=lambda o: _t(o) or datetime.min.replace(tzinfo=sess.IST)):
        k = (str(o["token"]), "B" if o["side"] == "S" else "S")       # a SELL closes a long
        closes.setdefault(k, []).append({"left": o["filled"], "o": o})
    out = []
    for p in placements:
        e = book.get(str(p.get("order_id")))
        if not e or e["filled"] <= 0:
            continue
        side = p.get("side", "B")
        need, fills = e["filled"], []
        for c in closes.get((str(p["token"]), side), []):
            if need <= 0:
                break
            use = min(need, c["left"])
            if use <= 0:
                continue
            c["left"] -= use
            need -= use
            fills.append({"qty": use, "price": c["o"]["avg"], "how": kind_of(c["o"]["remarks"]),
                          "time": (_t(c["o"]) or datetime.min).strftime("%H:%M")})
        qty = e["filled"] - need
        if qty <= 0:
            continue
        entry = e["avg"]
        exit_value = sum((f["price"] * f["qty"] for f in fills), Decimal(0))
        exit_avg = exit_value / qty
        gross = (exit_avg - entry) * qty if side == "B" else (entry - exit_avg) * qty
        buy, sell = (entry * qty, exit_value) if side == "B" else (exit_value, entry * qty)
        fee = charges_mod.round_trip(buy, sell).total
        out.append({
            "order_id": str(p["order_id"]), "symbol": p["symbol"], "side": side, "token": str(p["token"]),
            "setup": p.get("setup"), "qty": qty, "entry": entry, "entry_time": (_t(e) or datetime.min).strftime("%H:%M"),
            "plan": {"stop": p.get("stop"), "target": p.get("target"), "risk": p.get("risk"),
                     "net_if_target": p.get("net_if_target"), "valid_minutes": p.get("valid_minutes")},
            "why": p.get("why"), "direction": p.get("direction"), "advisory": p.get("advisory"),
            "lessons_cited": p.get("lessons") or [],
            "exits": fills, "exit_avg": exit_avg.quantize(Decimal("0.01")),
            "gross": gross.quantize(Decimal("0.01")), "charges": fee, "net": (gross - fee).quantize(Decimal("0.01")),
        })
    return out


def excursion(trade: dict, candles: list) -> dict:
    """Best and worst open profit (Rs, gross) while the trade was open."""
    try:
        t0 = datetime.strptime(trade["entry_time"], "%H:%M").time()
        t1 = max(datetime.strptime(f["time"], "%H:%M").time() for f in trade["exits"])
    except (ValueError, KeyError):
        return {}
    bars = [c for c in candles if t0 <= c.opens_at.time() <= t1]
    if not bars:
        return {}
    q, e = trade["qty"], trade["entry"]
    if trade["side"] == "B":
        best, worst = max(c.high for c in bars), min(c.low for c in bars)
        mfe, mae = (best - e) * q, (worst - e) * q
    else:
        best, worst = min(c.low for c in bars), max(c.high for c in bars)
        mfe, mae = (e - best) * q, (e - worst) * q
    return {"best_open_profit": mfe.quantize(Decimal("1")), "worst_open_loss": mae.quantize(Decimal("1")),
            "gave_back": (mfe - trade["gross"]).quantize(Decimal("1"))}


def dossiers(day: str, broker=None, *, log_rows: Optional[list] = None, orders: Optional[list] = None) -> List[dict]:
    rows = log_rows if log_rows is not None else common.read_log(day)
    placements = [r for r in rows if r.get("event") == "placed"]
    if orders is None:
        orders = broker.orders() if broker else []
    trades = trades_from_book(placements, orders)
    adjust = [r for r in rows if r.get("event") == "plan_adjusted"]
    moves = [r for r in rows if r.get("event") == "stop_moved"]
    for tr in trades:
        tr["adjustments"] = [{"time": r["ts"][11:16], "stop": r.get("stop"), "target": r.get("target"),
                              "why": str(r.get("reason", ""))[:200]}
                             for r in adjust if str(r.get("parent")) == tr["order_id"]][:12]
        tr["auto_stop_moves"] = [{"time": r["ts"][11:16], "to": r.get("to"), "why": r.get("reason")}
                                 for r in moves if str(r.get("parent")) == tr["order_id"]][:6]
        if broker is not None:
            try:
                d = date.fromisoformat(day)
                start = datetime.combine(d, datetime.min.time().replace(hour=9, minute=15), tzinfo=sess.IST)
                tr.update(excursion(tr, broker.candles(tr["token"], start, start + timedelta(hours=6, minutes=15))))
            except Exception:                           # noqa: BLE001
                pass
    return trades


# ------------------------------------------------------------------ review

PROMPT = """You are the trading coach of an automated intraday NSE equity system. You (Claude) made
every decision in these trades: the entry, the stop, the target and every adjustment. Review
today's closed trades honestly and turn them into lessons your future self will follow.

For each trade judge: was the entry right (setup, timing, direction of the market), was the stop
placed well, and was the exit managed well? best_open_profit vs net shows profit given back;
worst_open_loss shows heat taken. Charges are real: a trade that netted less than its charges
was not worth taking.

Then write LESSONS: concrete, conditional, checkable rules of the form "when <situation>, do
<action>" that would have improved these results, e.g. "When an open trade shows Rs 100+ and the
stock stalls at VWAP, tighten the stop to lock half instead of moving the target". Not vague
advice ("be careful"). Each lesson names the trades (symbol + time) that taught it.

You also get the existing lessons. For each one today's trades bear on, say "reinforce" (today
confirms it) or "contradict" (following it lost money today). Propose at most {max_new} new
lessons; do not restate an existing lesson as new, reinforce it instead.

Reply with ONLY this JSON:
{{"day_summary": "<2 sentences>",
  "trades": [{{"order_id": "...", "verdict": "good|bad entry|bad stop|bad exit|gave back profit|bad luck",
              "post_mortem": "<2-3 sentences>"}}],
  "lessons": [{{"id": "<existing id, or NEW>", "action": "reinforce|contradict|new",
               "rule": "<for new: the rule>", "scope": "entry|stop|exit|sizing|timing|market",
               "evidence": "<which trades and why>"}}]}}

EXISTING LESSONS:
{lessons}

TODAY ({day}) TRADES:
{trades}
"""


def review(day: str, trades: List[dict], lessons: dict, run: Optional[Callable] = None) -> dict:
    import brain
    active = [{"id": k, "rule": v["rule"], "support": v["support"], "against": v["against"]}
              for k, v in lessons.items() if v.get("status") == "active"]
    prompt = PROMPT.format(max_new=MAX_NEW_PER_DAY, day=day,
                           lessons=json.dumps(active, indent=1) if active else "(none yet)",
                           trades=json.dumps(trades, indent=1, default=str))
    raw = (run or (lambda p: brain.run_cli(p, timeout=240)))(prompt)
    return brain.parse_obj(raw)


# ------------------------------------------------------------------ store

def load() -> dict:
    try:
        return json.loads(lessons_path().read_text())
    except (OSError, ValueError):
        return {}


def save(lessons: dict) -> None:
    p = lessons_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(lessons, indent=1))
    tmp.replace(p)


def _next_id(lessons: dict) -> str:
    n = max([int(k[1:]) for k in lessons if re.fullmatch(r"L\d+", k)] + [0]) + 1
    return f"L{n:03d}"


def merge(lessons: dict, result: dict, day: str) -> dict:
    """Apply one review to the store. Returns {"new": [...], "reinforced": [...], "retired": [...]}."""
    out = {"new": [], "reinforced": [], "contradicted": [], "retired": []}
    new_count = 0
    for r in result.get("lessons") or []:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action", "")).lower()
        ev = str(r.get("evidence", ""))[:300]
        lid = str(r.get("id", "")).strip().upper()
        if action == "new" or lid in ("", "NEW"):
            rule = str(r.get("rule", "")).strip()
            if not rule or new_count >= MAX_NEW_PER_DAY:
                continue
            new_count += 1
            lid = _next_id(lessons)
            lessons[lid] = {"rule": rule[:400], "scope": str(r.get("scope", ""))[:20], "created": day,
                            "updated": day, "support": 1, "against": 0, "status": "active",
                            "evidence": [f"{day}: {ev}"]}
            out["new"].append(lid)
            continue
        les = lessons.get(lid)
        if not les:
            continue
        les["updated"] = day
        les["evidence"] = (les.get("evidence", []) + [f"{day} ({action}): {ev}"])[-6:]
        if action == "reinforce":
            les["support"] += 1
            out["reinforced"].append(lid)
        elif action == "contradict":
            les["against"] += 1
            out["contradicted"].append(lid)
            if les["against"] >= max(2, les["support"]) and les.get("status") == "active":
                les["status"] = "retired"
                out["retired"].append(lid)
    # keep the strongest, most recent MAX_ACTIVE active
    active = sorted((k for k, v in lessons.items() if v.get("status") == "active"),
                    key=lambda k: (lessons[k]["support"] - lessons[k]["against"], lessons[k]["updated"]),
                    reverse=True)
    for k in active[MAX_ACTIVE:]:
        lessons[k]["status"] = "retired"
        out["retired"].append(k)
    return out


def for_prompt(limit: int = MAX_ACTIVE) -> list:
    """Active lessons, strongest first, compact, for STATE.lessons."""
    lessons = load()
    act = [(k, v) for k, v in lessons.items() if v.get("status") == "active"]
    act.sort(key=lambda kv: (kv[1]["support"] - kv[1]["against"], kv[1]["updated"]), reverse=True)
    return [{"id": k, "rule": v["rule"], "seen": v["support"], "failed": v["against"]} for k, v in act[:limit]]


# ------------------------------------------------------------------ job

def run_day(day: str, broker=None, run: Optional[Callable] = None, orders: Optional[list] = None) -> dict:
    trades = dossiers(day, broker, orders=orders)
    if not trades:
        common.log(JOB, {"event": "learning_skipped", "day": day, "reason": "no closed trades"})
        return {"day": day, "trades": 0}
    lessons = load()
    try:
        result = review(day, trades, lessons, run)
    except Exception as exc:                            # noqa: BLE001
        common.log(JOB, {"event": "learning_failed", "day": day, "reason": str(exc)[:300]})
        return {"day": day, "error": str(exc)}
    changes = merge(lessons, result, day)
    save(lessons)
    journal = {"day": day, "summary": result.get("day_summary"), "trades": trades,
               "reviews": result.get("trades"), "lesson_changes": changes}
    p = journal_path(day)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(journal, indent=1, default=str))
    common.log(JOB, {"event": "learning_done", "day": day, "trades": len(trades),
                     "net": str(sum((t["net"] for t in trades), Decimal(0))), **changes})
    if changes["new"] or changes["retired"]:
        common.notify("Lessons updated",
                      f"{day}: {len(changes['new'])} new, {len(changes['reinforced'])} confirmed, "
                      f"{len(changes['retired'])} retired. {str(result.get('day_summary') or '')[:150]}")
    return {"day": day, "trades": len(trades), **changes}


def archived_book(day: str) -> Optional[list]:
    try:
        rows = json.loads((store_dir() / f"book-{day}.json").read_text())
    except (OSError, ValueError):
        return None
    for o in rows:
        for k in ("avg", "price", "trigger"):
            o[k] = D(o.get(k))
    return rows


def main(argv: List[str]) -> int:
    from shoonya import Shoonya, ShoonyaError
    today = sess.now().strftime("%Y-%m-%d")
    day = argv[argv.index("--day") + 1] if "--day" in argv else today
    if journal_path(day).exists() and "--force" not in argv:
        common.log(JOB, {"event": "learning_skipped", "day": day, "reason": "already reviewed"})
        return 0
    orders = archived_book(day)
    broker = None
    try:
        broker = Shoonya()
        if orders is None:
            if day != today:
                common.log(JOB, {"event": "learning_skipped", "day": day, "reason": "no archived order book"})
                return 0
            orders = broker.orders()                     # Shoonya's book is today's only
    except ShoonyaError as exc:
        if orders is None:
            common.log(JOB, {"event": "learning_failed", "day": day, "reason": f"broker: {exc}"[:200]})
            return 1
        broker = None                                    # still review, without candles
    run_day(day, broker, orders=orders)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
