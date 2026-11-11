"""
The strategy book: which setups to trade, re-decided every evening.

After the close, every setup in the playbook and its exact opposite (the
"fade": same price, other side, same risk and 2R target) is replayed over
the last WINDOW sessions on real candles at the live position size with
real charges. Each variant gets:

  trades, win rate, net after charges, profit factor, net per trade

and a status:

  active   net > 0, profit factor >= MIN_PF, at least MIN_TRADES trades
  off      anything else

A setup and its fade are opposites, so at most one of the pair is active:
the better one. Tomorrow Claude is shown only the active variants. When a
setup has been losing and its fade has been winning, Claude is shown the
fade instead. When neither earns, the setup is not shown at all. Each night
re-decides, so a setup that recovers comes back.

The window is weeks, not one day: a single day's loss is mostly noise, so
flipping on it would chase noise. Closed live trades are listed alongside.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import session as sess                                   # noqa: E402

WINDOW = int(os.environ.get("TRADER_BOOK_WINDOW", "20"))
MIN_TRADES = 8
MIN_PF = Decimal("1.15")


def path() -> Path:
    return common.STATE_DIR / "strategy_book.json"


def key(setup: str, side: str) -> str:
    return f"{setup} {'long' if side in ('BUY', 'B') else 'short'}"


def stats(rows: List[dict]) -> dict:
    net = sum((Decimal(str(r["net"])) for r in rows), Decimal(0))
    wins = [r for r in rows if Decimal(str(r["net"])) > 0]
    gain = sum((Decimal(str(r["net"])) for r in wins), Decimal(0))
    loss = -sum((Decimal(str(r["net"])) for r in rows if Decimal(str(r["net"])) < 0), Decimal(0))
    pf = (gain / loss) if loss else (Decimal(99) if gain else Decimal(0))
    return {"trades": len(rows), "wins": len(wins), "win_pct": round(len(wins) / len(rows) * 100) if rows else 0,
            "net": str(net.quantize(Decimal("0.01"))), "pf": str(pf.quantize(Decimal("0.01"))),
            "per_trade": str((net / len(rows)).quantize(Decimal("0.01")) if rows else Decimal(0))}


def build(results: List[dict], days: int) -> dict:
    groups: Dict[str, list] = collections.defaultdict(list)
    for r in results:
        groups[key(r["setup"], r["side"])].append(r)
    book = {k: {**stats(v), "status": "off"} for k, v in groups.items()}
    for k, s in book.items():
        if s["trades"] >= MIN_TRADES and Decimal(s["net"]) > 0 and Decimal(s["pf"]) >= MIN_PF:
            s["status"] = "active"
    # a setup and its fade are opposites: keep only the better one active
    for k in list(book):
        if "_fade" in k:
            continue
        name, side = k.rsplit(" ", 1)
        other = "short" if side == "long" else "long"
        fk = f"{name}_fade {other}"
        if fk in book and book[k]["status"] == book[fk]["status"] == "active":
            loser = k if Decimal(book[k]["net"]) < Decimal(book[fk]["net"]) else fk
            book[loser]["status"] = "off"
    return {"built": sess.now().isoformat(timespec="seconds"), "sessions": days,
            "rule": f"active: net > 0, profit factor >= {MIN_PF}, {MIN_TRADES}+ trades over {days} sessions",
            "variants": dict(sorted(book.items()))}


def load() -> Optional[dict]:
    try:
        return json.loads(path().read_text())
    except (OSError, ValueError):
        return None


def apply(found: List[dict], tick, book: Optional[dict]) -> List[dict]:
    """Detected setups -> what Claude is shown: active originals as they are,
    the fade where the fade is the active one, nothing where neither earns.
    Without a book (first run), everything is shown as untested."""
    if not book:
        return [{**s, "edge": "untested"} for s in found]
    from backtest import invert
    v = book.get("variants", {})
    out = []
    for s in found:
        k = key(s["setup"], s["side"])
        if v.get(k, {}).get("status") == "active":
            out.append({**s, "edge": _edge(v[k])})
            continue
        f = invert(s, tick)
        fk = key(f["setup"], f["side"])
        if v.get(fk, {}).get("status") == "active":
            out.append({**f, "edge": _edge(v[fk])})
    return out


def _edge(s: dict) -> str:
    return f"{s['trades']} trades, {s['win_pct']}% wins, net {s['net']}, PF {s['pf']}"


def main() -> int:
    """Nightly: rebuild the book from the last WINDOW sessions."""
    now = sess.now()
    if "--force" not in sys.argv and (sess.session_for(now) is None or now.time() < time(15, 35)):
        return 0
    import backtest
    from shoonya import Shoonya
    notional = Decimal(common.setting("TRADER_BOOK_NOTIONAL", "26000"))
    last = sess._last_entry() if hasattr(sess, "_last_entry") else time(14, 45)
    out = backtest.run_setups(Shoonya(), common.symbols(), WINDOW, last, notional)
    book = build(out["results"], out["days"])
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(book, indent=1))
    active = [k for k, s in book["variants"].items() if s["status"] == "active"]
    common.log("book", {"event": "strategy_book", "sessions": out["days"], "active": active})
    common.notify("trader: strategy book for tomorrow", ("active: " + ", ".join(active)) if active else
                  "no setup is profitable over the window: Claude trades only its own reads", priority="default")
    print(json.dumps(book, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
