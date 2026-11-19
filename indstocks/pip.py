"""
The owner's PIP scorecard (from 20 Nov 2026): the AI trader runs on paper
(paperbroker.py) and earns real money back only after TRADER_PIP_DAYS (14)
CONSECUTIVE trading days each netting at least TRADER_PIP_DAILY_MIN (Rs 400)
after charges. One red or under-400 day resets the streak to zero.

  day net   the fill monitor's last "pnl" event of the day: realised after
            every charge (the same number the dashboard shows)
  record    STATE_DIR/pip.json: {day: net}; the streak is recomputed from it
  report    a phone note after the close: today's net, the streak, the best
            and worst paper models on the bench
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import session as sess  # noqa: E402

PIP_FILE = common.STATE_DIR / "pip.json"
START = "2026-11-20"


def daily_min() -> Decimal:
    return Decimal(common.setting("TRADER_PIP_DAILY_MIN", "400"))


def days_needed() -> int:
    return int(common.setting("TRADER_PIP_DAYS", "14"))


def day_net(day: str) -> Optional[Decimal]:
    ev = [r for r in common.read_log(day) if r.get("event") == "pnl"]
    return Decimal(str(ev[-1]["net"])) if ev else None


def load() -> dict:
    try:
        return json.loads(PIP_FILE.read_text())
    except (OSError, ValueError):
        return {}


def streak(rec: dict) -> int:
    n = 0
    for day in sorted(rec, reverse=True):
        if Decimal(str(rec[day])) >= daily_min():
            n += 1
        else:
            break
    return n


def record(day: str) -> dict:
    rec = load()
    net = day_net(day)
    if net is None:
        net = Decimal(0)                     # no trade: a zero day, which resets the streak
    rec[day] = str(net.quantize(Decimal("0.01")))
    PIP_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIP_FILE.write_text(json.dumps(dict(sorted(rec.items())), indent=1))
    s = streak(rec)
    out = {"day": day, "net": rec[day], "streak": s, "needed": days_needed(), "min": str(daily_min()),
           "days": len(rec), "total": str(sum(Decimal(v) for v in rec.values())), "passed": s >= days_needed()}
    common.log("pip", {"event": "pip_day", **out})
    return out


def bench_line(day: str) -> str:
    try:
        import paper
        d = json.loads((paper.PAPER_DIR / f"{day}-summary.json").read_text())
        rows = sorted(((m, float(r["net"])) for m, r in d["models"].items() if r.get("trades")), key=lambda x: -x[1])
        return ("bench best: " + ", ".join(f"{m} {n:+.0f}" for m, n in rows[:3])
                + " | worst: " + ", ".join(f"{m} {n:+.0f}" for m, n in rows[-2:]))
    except Exception:                                    # noqa: BLE001
        return ""


def main() -> None:
    now = sess.now()
    day = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or day < START:
        return
    r = record(day)
    ok = Decimal(r["net"]) >= daily_min()
    common.notify(f"PIP day {r['days']}: {Decimal(r['net']):+.0f} net ({'counts' if ok else 'streak reset'})",
                  f"Streak {r['streak']}/{r['needed']} days of Rs {r['min']}+. Paper total {Decimal(r['total']):+.0f}. "
                  + bench_line(day), priority="high")


if __name__ == "__main__":
    main()
