"""
Per-strategy journals on the server's data disk (owner, 26 Nov): every strategy
has its own folder that no deploy overwrites,

  /var/lib/indstocks/strategies/<strategy>/
      logs/YYYY-MM-DD.md        every trade of the day, appended as it happens
      learnings/YYYY-MM-DD.md   the AI's review of that day (strategylearn.py)

Strategies: every paper-bench model (paper.py) and "ai_trader" (the AI's own
trades on the paper broker). The data set these build up is what future models
will be trained on, so rows are plain, consistent and never rewritten except
when the bench replays a day it is rebuilding.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AI = "ai_trader"
COLS = ("time", "event", "stock", "side", "qty", "price", "stop", "target", "net Rs", "note")


def base() -> Path:
    # on the server TRADER_STRATEGY_DIR=/var/lib/indstocks/strategies (set by deploy)
    return Path(common.setting("TRADER_STRATEGY_DIR", str(common.STATE_DIR / "strategies")))


def log_file(strategy: str, day: str) -> Path:
    return base() / _safe(strategy) / "logs" / f"{day}.md"


def learn_file(strategy: str, day: str) -> Path:
    return base() / _safe(strategy) / "learnings" / f"{day}.md"


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _cell(x) -> str:
    return "" if x is None else str(x).replace("|", "/").replace("\n", " ")[:160]


def header(strategy: str, day: str) -> str:
    wallet = common.setting("TRADER_BENCH_CAPITAL" if strategy != AI else "TRADER_PAPER_CAPITAL", "100000")
    target = common.setting("TRADER_BENCH_TARGET" if strategy != AI else "TRADER_DAY_TARGET", "1000")
    return (f"# {strategy}: trade log {day}\n\n"
            f"Paper wallet Rs {wallet}, day target Rs {target}, no daily loss limit. "
            f"Prices are live NSE; fills simulated; charges are Shoonya's real intraday charges.\n\n"
            "| " + " | ".join(COLS) + " |\n|" + "---|" * len(COLS) + "\n")


def row(strategy: str, day: str, *, time: str, event: str, stock: str = "", side: str = "", qty=None,
        price=None, stop=None, target=None, net=None, note: str = "") -> None:
    try:
        f = log_file(strategy, day)
        f.parent.mkdir(parents=True, exist_ok=True)
        new = not f.exists()
        with open(f, "a", encoding="utf-8") as fh:
            if new:
                fh.write(header(strategy, day))
            fh.write("| " + " | ".join(_cell(v) for v in (time, event, stock, side, qty, price, stop, target, net, note)) + " |\n")
    except OSError:
        pass


def note(strategy: str, day: str, text: str) -> None:
    """A free-text line under the table (day summary, target hit)."""
    try:
        f = log_file(strategy, day)
        f.parent.mkdir(parents=True, exist_ok=True)
        if not f.exists():
            f.write_text(header(strategy, day))
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"\n> {text}\n\n")
    except OSError:
        pass


def reset_bench_day(day: str) -> None:
    """The bench rebuilds a day from the tape after a restart: clear its logs for that day first.
    The AI trader's log and the F&O sleeves' logs are not replayed, so they are kept."""
    try:
        for d in base().iterdir():
            if d.name != AI and not d.name.endswith(("_fut", "_opt")):
                f = d / "logs" / f"{day}.md"
                if f.exists():
                    f.unlink()
    except OSError:
        pass


def on_event(rec: dict) -> None:
    """The AI trader's own trades, from the main log, as they happen."""
    ev = rec.get("event")
    if ev not in ("placed", "entry_cancelled", "stop_moved", "target_exit", "trade_result", "profit_lock_armed",
                  "day_locked", "momentum_exit_shadow"):
        return
    ts = str(rec.get("ts", ""))
    day, t = ts[:10], ts[11:19]
    if not day:
        return
    side = {"B": "BUY", "S": "SELL"}.get(str(rec.get("side", "")), rec.get("side", ""))
    if ev == "placed":
        row(AI, day, time=t, event="entry order", stock=rec.get("symbol"), side=side, qty=rec.get("qty"),
            price=rec.get("trigger") or rec.get("entry"), stop=rec.get("stop"), target=rec.get("target"),
            note=f"{rec.get('setup') or ''} {rec.get('why') or ''}".strip())
    elif ev == "trade_result":
        oid = str(rec.get("order_id") or "")
        try:
            seen = oid and oid in log_file(AI, day).read_text(encoding="utf-8")
        except OSError:
            seen = False
        if seen:
            return                                       # flatten reports a result on every pass
        row(AI, day, time=t, event="closed", stock=rec.get("symbol"), side=side, net=rec.get("net"),
            note=f"{rec.get('setup') or ''}; direction {rec.get('direction')}; "
                 f"{'win' if rec.get('win') else 'loss'}; order {oid}")
    elif ev == "stop_moved":
        row(AI, day, time=t, event="stop moved", stock=rec.get("symbol"), price=rec.get("ltp"), stop=rec.get("to"),
            note=rec.get("reason"))
    elif ev == "target_exit":
        row(AI, day, time=t, event="exit", stock=rec.get("symbol"), qty=rec.get("qty"), price=rec.get("ltp"),
            note=rec.get("reason"))
    else:
        row(AI, day, time=t, event=ev.replace("_", " "), stock=rec.get("symbol"), note=rec.get("reason") or rec.get("net"))
