"""
Nightly learning per strategy (owner, 26 Nov). After the close, for every
strategy folder with a trade log today (journal.py), the AI reads that day's
log and writes strategies/<strategy>/learnings/DAY.md:

    # <headline>
    Date, the day's numbers, then the learnings: what worked, what failed and
    why, and what to try next (as knob values the strategy actually has).

These files, with the logs, are the data set for the owner's own models later.
A strategy with no trades gets a one-line note and no AI call. The AI never
touches the strategies' code or settings here; it writes notes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import journal  # noqa: E402
import session as sess  # noqa: E402

PROMPT = """You are reviewing ONE trading strategy's day on NSE (paper money, real prices, real charges).
Strategy: {name}
{about}

Its trade log for {day} (every open and close, in order):

{log}

Day numbers: {numbers}

Write the day's learning for this strategy as Markdown, in exactly this shape:

# <one-line headline: the single most important thing today taught>

Date: {day}

## What happened
<2-4 sentences: the day's market and how this strategy did, with numbers>

## Learnings
- <3-6 bullets: what worked, what failed and WHY, tied to specific trades by time and stock>

## Try next
- <1-3 concrete changes, as values of this strategy's own knobs where it has them ({knobs}), or a filter>

Be specific and honest; no generic trading advice. Plain ASCII.
"""


def about(name: str) -> tuple:
    if name == journal.AI:
        return ("The AI trader: an AI picks setups (orb, failed_break, day_break) on 5-minute scans; code manages "
                "stops, a trailing profit lock and a Rs 1,000 day target.", "size, stop, target, setups, direction")
    sleeve = ""
    if name.endswith("~fut"):
        sleeve = (" This is its FUTURES sleeve: every stock signal of the strategy is traded as 1 lot of the stock's "
                  "near-month future (own Rs 1 lakh wallet).")
    elif name.endswith("~opt"):
        sleeve = (" This is its OPTIONS sleeve: every BUY signal buys the stock's at-the-money call, every SELL signal "
                  "its at-the-money put (own Rs 1 lakh wallet); exits follow the stock trade's exits.")
    name = name.split("~")[0]
    try:
        import paper
        base = name.split("@")[0]
        for r in paper.RECIPES:
            if base == r or base.startswith(r) or (r == "orb" and base.startswith("orb")) or \
                    (r == "ema_cross" and base.startswith("ema_9_21")):
                ks = ", ".join(f"{k} {lo}..{hi}" for k, (d, lo, hi, _) in paper.knobs_for(r).items())
                doc = (paper.RECIPES[r].__doc__ or "").strip().split("\n")[0]
                inv = " This copy takes the OPPOSITE side of every signal." if name.endswith("_inv") else ""
                ai = " Its knobs are set by the AI during the day (tuner)." if name.endswith("@ai") else ""
                return (f"A rule-based model, recipe '{r}'. {doc}{inv}{ai}{sleeve}", ks)
    except Exception:                                    # noqa: BLE001
        pass
    return ("A rule-based model.", "its knobs")


def numbers(log_text: str) -> str:
    nets = [float(m) for m in re.findall(r"\| close \|[^\n]*?\| (-?\d+\.\d+) \|", log_text)]
    if not nets:
        return "no closed trades"
    wins = sum(1 for n in nets if n > 0)
    return f"{len(nets)} closed trades, {wins} won, net after charges Rs {sum(nets):.0f}, best {max(nets):.0f}, worst {min(nets):.0f}"


def run(day: Optional[str] = None, ask: Optional[Callable[[str], str]] = None, limit_chars: int = 14000) -> dict:
    day = day or sess.now().strftime("%Y-%m-%d")
    if ask is None:
        import brain
        ask = lambda p: brain.run_cli(p, timeout=300)            # noqa: E731
    done, skipped, failed = [], [], []
    try:
        folders = sorted(d for d in journal.base().iterdir() if d.is_dir())
    except OSError:
        folders = []
    for d in folders:
        name = d.name
        log_f = d / "logs" / f"{day}.md"
        out_f = d / "learnings" / f"{day}.md"
        if not log_f.exists() or out_f.exists():
            continue
        text = log_f.read_text(encoding="utf-8", errors="replace")
        out_f.parent.mkdir(parents=True, exist_ok=True)
        if "| open |" not in text and "| entry order |" not in text:
            out_f.write_text(f"# No trades\n\nDate: {day}\n\n{name} took no trades today.\n")
            skipped.append(name)
            continue
        if len(text) > limit_chars:
            text = text[:limit_chars // 2] + "\n...\n" + text[-limit_chars // 2:]
        desc, knobs = about(name)
        try:
            md = ask(PROMPT.format(name=name, about=desc, day=day, log=text, numbers=numbers(text), knobs=knobs))
            md = md.strip()
            if not md.startswith("#"):
                md = f"# Review\n\nDate: {day}\n\n{md}"
            out_f.write_text(md + "\n")
            done.append(name)
        except Exception as exc:                             # noqa: BLE001
            failed.append(f"{name}: {str(exc)[:120]}")
        time.sleep(1)
    common.log("strategylearn", {"event": "strategy_learnings", "day": day, "written": len(done),
                                 "no_trades": len(skipped), "failed": failed[:5]})
    return {"written": done, "no_trades": skipped, "failed": failed}


if __name__ == "__main__":
    n = sess.now()
    if n.weekday() < 5:
        print(json.dumps(run(), default=str)[:2000])
