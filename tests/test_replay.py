"""Whole trading days through the REAL fill monitor, scan and flatten code,
against a simulated exchange (tests/_exchange.py), with a scripted decider
that is sometimes sloppy or hostile and an exchange that sometimes fills
partially or drops a request. After every tick: never more pending closing
quantity than the position, no stop resting on a flat stock, every bot
position protected within 30 s; at the end of the day: flat, nothing left."""

import os
import random
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import reset  # noqa: E402

import session as sess  # noqa: E402
from market import Candle, to_tick  # noqa: E402

LIVE_LIKE = {
    "TRADER_DECIDER": "claude", "TRADER_STYLE": "aggressive", "TRADER_ALLOW_SHORT": "true",
    "TRADER_MAX_LEVERAGE": "5.0", "TRADER_MAX_CONCURRENT": "3", "TRADER_MIN_RR": "2.0",
    "TRADER_MAX_TRADES": "15", "TRADER_MAX_TRADES_HARD": "25", "TRADER_MAX_ENTRIES_PER_SYMBOL": "3",
    "TRADER_MAX_RESTING": "8", "TRADER_LAST_ENTRY": "14:45", "TRADER_MIN_POSITION_VALUE": "15000",
    "TRADER_MIN_NET_TARGET": "60", "TRADER_TARGET_R": "4", "TRADER_MAX_LOSS": "600",
    "TRADER_DAILY_LOSS": "2500", "TRADER_MIN_ORDER_VALUE": "5000",
}
SYMS = {"AAA": (D("281.00"), D("0.01")), "BBB": (D("1080.00"), D("0.05")), "CCC": (D("4140.00"), D("0.10"))}


def synthetic_day(day: date, seed: int) -> dict:
    """A random-walk day per symbol with a trend, a shock and a reversal."""
    rng = random.Random(seed)
    out = {}
    for sym, (p0, tick) in SYMS.items():
        px, cs = p0, []
        drift = rng.choice([-1, 1]) * rng.uniform(0.0002, 0.0008)
        t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=sess.IST)
        for k in range(75):
            if k == 40:
                drift = -drift                                   # the reversal
            shock = rng.choice([0, 0, 0, 0, 0.01, -0.01]) if k in (12, 55) else 0
            o = px
            c = o * D(str(1 + drift + rng.gauss(0, 0.0015) + shock))
            h = max(o, c) * D(str(1 + abs(rng.gauss(0, 0.0008))))
            l = min(o, c) * D(str(1 - abs(rng.gauss(0, 0.0008))))
            cs.append(Candle(t, to_tick(o, tick), to_tick(h, tick, mode="up"), to_tick(l, tick, mode="down"),
                             to_tick(c, tick), 10000, 5))
            px = c
            t += timedelta(minutes=5)
        out[sym] = cs
    return out


class Replay(unittest.TestCase):
    def run_one(self, day, seed, chaos, hostile, state=None, fresh=True):
        from _exchange import run_day
        if fresh:
            reset(TRADER_SYMBOLS=",".join(SYMS), **LIVE_LIKE)
        saved = sess.now
        try:
            r = run_day(synthetic_day(day, seed), {s: v[1] for s, v in SYMS.items()},
                        seed=seed, chaos=chaos, hostile=hostile, state=state)
        finally:
            sess.now = saved
        self.assertEqual(r["errors"], [], "the code raised")
        self.assertEqual(r["violations"], [], "a safety invariant broke")
        bad_ticks = [x for x in r["rejects"] if "tick" in x or "band" in x or "limit above" in x or "limit below" in x]
        self.assertEqual(bad_ticks, [], "orders the exchange rejects for a malformed price")
        self.assertGreater(r["orders"], 5, "the day actually traded")
        return r

    def test_a_clean_day(self):
        self.run_one(date(2026, 11, 16), 11, 0.0, 0.0)

    def test_a_messy_day(self):
        self.run_one(date(2026, 11, 17), 23, 0.2, 0.2)

    def test_the_fill_monitor_carries_no_state_into_the_next_day(self):
        import common
        import control
        import profitlock
        state = {"lock_day": date(2026, 11, 13), "lock_best": {"_day": D("5000"), "_day_locked": D(1)}}
        reset(TRADER_SYMBOLS=",".join(SYMS), **LIVE_LIKE)
        self.run_one(date(2026, 11, 16), 11, 0.0, 0.0, state=state, fresh=False)
        self.assertEqual(common.events("day_locked", "2026-11-16"), [], "Friday's best must not lock Monday")
        self.assertIsNone(state["lock_best"].get("_day_locked"))


if __name__ == "__main__":
    unittest.main()
