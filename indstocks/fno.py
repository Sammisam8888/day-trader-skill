"""
F&O sleeves for the paper bench (owner, 26 Nov): every strategy's stock signal is
also traded in the stock's near-month FUTURE and its at-the-money OPTION, each
sleeve with its own paper wallet. Paper only. (Built in stages; this is stage 1:
finding the contracts.)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def probe(sym: str = "RELIANCE") -> None:
    from shoonya import Shoonya
    b = Shoonya(data_only=True)
    rows = b.search(sym, "NFO")
    print("NFO search", sym, len(rows))
    for r in rows[:12]:
        print(" ", json.dumps(r)[:300])
    fut = [r for r in rows if str(r.get("instname", "")).startswith("FUT")]
    if fut:
        q = b.call("GetQuotes", {"exch": "NFO", "token": str(fut[0]["token"])})
        print("fut quote keys:", sorted(q)[:40])
        print("fut quote:", json.dumps({k: q.get(k) for k in ("tsym", "lp", "ls", "exd", "bp1", "sp1", "oi", "ti")}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        probe(*(sys.argv[2:3] or []))
