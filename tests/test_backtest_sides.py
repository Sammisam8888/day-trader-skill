import os
import sys
import unittest
from datetime import date, datetime, time
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import candle  # noqa: E402

import backtest  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)


def day(path):
    """Opening range 100-101 (09:15-09:30), then 5-minute bars through `path`."""
    cs = [candle(DAY, 9, 15, 100.5, 101, 100.5, 100.6), candle(DAY, 9, 20, 100.6, 100.7, 100, 100.4),
          candle(DAY, 9, 25, 100.4, 100.6, 100.3, 100.5)]
    px, t = 100.5, 9 * 60 + 30
    for nxt in path:
        hh, mm = divmod(t, 60)
        cs.append(candle(DAY, hh, mm, px, max(px, nxt) + 0.01, min(px, nxt) - 0.01, nxt))
        px, t = nxt, t + 5
    return cs


class Sides(unittest.TestCase):
    def setUp(self):
        self.s = sess.session_for(datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST))

    def test_breakdown_short_hits_target(self):
        r = backtest.simulate_sides("X", day([100.2, 99.8, 99.2, 98.5, 97.5, 97.0]), self.s, D("0.01"),
                                    sides=("B", "S"), last_entry=time(12, 30))
        self.assertEqual((r["side"], r["how"]), ("S", "target"))
        self.assertGreater(r["net"], 0)

    def test_long_only_ignores_the_breakdown(self):
        r = backtest.simulate_sides("X", day([100.2, 99.8, 99.2, 98.5]), self.s, D("0.01"),
                                    sides=("B",), last_entry=time(12, 30))
        self.assertFalse(r["filled"])

    def test_gate_blocks_a_side(self):
        gate = lambda t: {"market": {"state": "UP", "allowed": ["BUY"]}, "stocks": {}}  # noqa: E731
        r = backtest.simulate_sides("X", day([100.2, 99.8, 99.2, 98.5]), self.s, D("0.01"),
                                    sides=("B", "S"), gate=gate, last_entry=time(12, 30))
        self.assertFalse(r["filled"])


if __name__ == "__main__":
    unittest.main()


class Playbook(unittest.TestCase):
    def test_a_breakdown_short_is_replayed_to_its_target(self):
        s = sess.session_for(datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST))
        path = [100.2, 99.8, 99.2, 98.5, 97.5, 97.0, 96.5, 96.0]
        out = backtest.simulate_setups(day(path), [], s, D("0.01"), last_entry=time(12, 30), notional=D("26000"))
        orb_short = [r for r in out if r["setup"] == "orb" and r["side"] == "SELL"]
        self.assertEqual(len(orb_short), 1)
        self.assertEqual(orb_short[0]["how"], "target")
        self.assertGreater(orb_short[0]["net"], 0)
        self.assertGreater(orb_short[0]["charges"], 0)
