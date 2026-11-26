"""
The AI agent (owner, 26 Nov): one more paper strategy with its own Rs 1 lakh
wallet, run by the AI as a manager of the other strategies. It does not have a
recipe of its own. Every 15 minutes it reads the market and how every bench
model is doing (today, the last hour, the last days), and decides:

  follow      which models' NEW signals to copy into its wallet, with a risk
              multiplier (0.25..2 x TRADER_AGENT_RISK) and a side filter
  exit        close its positions that followed these models, now
  exit_all    close everything now
  stand_aside take no new trades until the next decision

paper.py applies each decision from its timestamp on (agent-DAY.jsonl), so a
restart replays the day exactly. The agent never touches the real account,
never changes another model, and every decision is kept with its reasons:
whether managing the bench beats every single model is the question it answers.

Runs 09:35 and every 15 minutes to 14:20 (agent.timer).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402

MAX_FOLLOW = 8

PROMPT = """You are the AI agent: a paper-money portfolio manager on NSE intraday, with your OWN wallet of
Rs 1,00,000 (5x intraday leverage, real charges, every position flat by 14:50, no new entries after 14:35).
You do not invent trades. You choose which of the bench's rule-based models to FOLLOW: when a model you
follow fires a new signal, the same trade (same stock, side, stop and target) is opened in your wallet,
sized so the stop loses Rs {risk} x your risk multiplier for that model. You can also close your positions.

Your goal: the best day net after charges. The day target is Rs {target}, and it is a milestone, not a stop:
keep making money after it. Losses are real lessons: no daily loss limit on paper.

How to think:
- The models are evidence. Follow what is working TODAY in this market (and has worked in recent hours),
  drop what keeps losing, size up the ones with an edge you can explain, size down the doubtful.
- "_inv" models take the opposite side of their recipe; "@ai" models are tuned by the AI during the day.
- Fewer, better trades: charges are about Rs 24 per Rs 50,000 round trip. Following many models that
  disagree only pays charges.
- Close a position (exit) when the reason you followed its model is gone; exit_all when the day turns
  against your whole book. Stand aside when nothing has an edge.
- Follow at most {max_follow} models. Use model names exactly as listed in STATE.bench_today.

Reply with ONLY this JSON:
{{"day_read": "<one or two sentences: what kind of day this is and why>",
  "follow": {{"<model>": {{"risk": <0.25..2>, "side": "both"|"long"|"short", "why": "<short reason>"}}, ...}},
  "exit": ["<model whose followed positions to close now>", ...],
  "exit_all": false,
  "stand_aside": false,
  "note": "<one line for your log: what you changed and why>"}}

STATE:
{state}
"""


def _ledger(day: str) -> List[dict]:
    out = []
    try:
        for line in (paper.PAPER_DIR / f"{day}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def last_hour(day: str, now_epoch: float) -> Dict[str, dict]:
    """Closed trades per model in the last 60 minutes."""
    out: Dict[str, dict] = {}
    for r in _ledger(day):
        if r.get("ev") != "close" or (r.get("t") or 0) < now_epoch - 3600:
            continue
        m = out.setdefault(r["model"], {"net": 0.0, "trades": 0, "wins": 0})
        n = float(r.get("net") or 0)
        m["net"] = round(m["net"] + n)
        m["trades"] += 1
        m["wins"] += 1 if n > 0 else 0
    return out


def my_book(day: str) -> dict:
    """The agent's own positions and closed trades today."""
    opens, closed = {}, []
    for r in _ledger(day):
        if r.get("model") != paper.AGENT:
            continue
        if r.get("ev") == "open":
            opens[r["symbol"]] = {"side": r["side"], "qty": r["qty"], "entry": r["entry"], "stop": r["stop"],
                                  "target": r["target"], "followed": r.get("src")}
        elif r.get("ev") == "close":
            o = opens.pop(r["symbol"], {})
            closed.append({"symbol": r["symbol"], "followed": o.get("followed"), "net": round(float(r.get("net") or 0)),
                           "why": r.get("why")})
    return {"open": opens, "closed_today": closed[-15:], "realised": round(sum(c["net"] for c in closed))}


def bench_today(day: str) -> Dict[str, dict]:
    try:
        d = json.loads((paper.PAPER_DIR / f"{day}-summary.json").read_text())
    except (OSError, ValueError):
        return {}
    return {m: {"net": round(float(r["net"])), "trades": r.get("trades", 0), "wins": r.get("wins", 0),
                "open": r.get("open_trades", 0)}
            for m, r in (d.get("models") or {}).items() if m in paper.MODELS}


def current(day: str) -> Optional[dict]:
    rows = paper.agent_decisions(day)
    return rows[-1] if rows else None


def state(now) -> dict:
    import tuner
    day = now.strftime("%Y-%m-%d")
    st = {"time": now.strftime("%H:%M"), "bench_today": bench_today(day),
          "bench_last_hour": last_hour(day, now.timestamp()), "bench_past_days": tuner._past_days(day),
          "your_book": my_book(day), "your_decision_now": current(day),
          "nifty": {"tape": tuner._tape("NIFTYBEES"), "today": tuner._day_open_move("NIFTYBEES", day)},
          "banknifty": {"tape": tuner._tape("BANKBEES"), "today": tuner._day_open_move("BANKBEES", day)},
          "tuner_day_read": tuner.current(day).get("day_read")}
    try:
        import brain
        st["internals"] = brain._internals()
        st["sentiment"] = brain.sentiment_view(now)
    except Exception:                                    # noqa: BLE001
        pass
    return st


def _num(v, lo: float, hi: float, default: float) -> float:
    try:
        return round(min(max(float(v), lo), hi), 2)
    except (TypeError, ValueError):
        return default


def validate(reply: dict) -> dict:
    """Keep only real model names, clamp risk, cap the count."""
    follow = {}
    for m, f in (reply.get("follow") or {}).items():
        if m not in paper.MODELS or len(follow) >= MAX_FOLLOW:
            continue
        f = f if isinstance(f, dict) else {}
        side = str(f.get("side", "both")).lower()
        follow[m] = {"risk": _num(f.get("risk", 1), *paper.AGENT_RISK_MULT, 1.0),
                     "side": side if side in ("both", "long", "short") else "both",
                     "why": str(f.get("why", ""))[:160]}
    ex = [m for m in (reply.get("exit") or []) if isinstance(m, str) and m in paper.MODELS]
    return {"day_read": str(reply.get("day_read", ""))[:400], "follow": follow, "exit": ex,
            "exit_all": bool(reply.get("exit_all")), "stand_aside": bool(reply.get("stand_aside")),
            "note": str(reply.get("note", ""))[:300]}


def run(now=None, ask: Optional[Callable[[str], str]] = None) -> Optional[dict]:
    now = now or sess.now()
    day = now.strftime("%Y-%m-%d")
    prompt = PROMPT.format(risk=f"{paper.agent_risk():.0f}", target=f"{paper.day_target():.0f}",
                           max_follow=MAX_FOLLOW, state=json.dumps(state(now), separators=(",", ":"), default=str))
    if ask is None:
        import brain
        ask = lambda p: brain.run_cli(p, timeout=240)            # noqa: E731
    t0 = time.monotonic()
    try:
        import brain
        reply = brain.parse_obj(ask(prompt))
    except Exception as exc:                                     # noqa: BLE001
        common.log("agent", {"event": "agent_failed", "reason": str(exc)[:300]})
        return None
    out = validate(reply)
    out["t"] = time.time()
    out["at"] = now.isoformat(timespec="seconds")
    paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with open(paper.PAPER_DIR / f"agent-{day}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, separators=(",", ":")) + "\n")
    common.log("agent", {"event": "agent_decision", "follow": {m: f["risk"] for m, f in out["follow"].items()},
                         "exit": out["exit"], "exit_all": out["exit_all"], "stand_aside": out["stand_aside"],
                         "day_read": out["day_read"], "latency_s": round(time.monotonic() - t0, 1)})
    return out


if __name__ == "__main__":
    n = sess.now()
    if n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) < (14, 40) and paper.agent_on():
        run(n)
