"""The backtest replays the live rules on synthetic candles."""
import os
import sys
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402

_b = load("indstocks", "backtest")
_s = load("indstocks", "session")
_bars = load("indstocks", "market")

DAY = date(2026, 11, 4)


def candles(path):
    """path: list of (hh, mm, o, h, l, c)."""
    out = []
    for hh, mm, o, h, l, c in path:
        out.append(_bars.Candle(datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=_s.IST),
                                Decimal(o), Decimal(h), Decimal(l), Decimal(c), 1000, 5))
    return out


def day(extra):
    base = [(9, 15, "100", "101", "99", "100.5"), (9, 20, "100.5", "101", "99.5", "100"),
            (9, 25, "100", "100.8", "99", "100.2")]   # range 99-101, entry 101.05, stop 100, tgt 103.15
    return candles(base + extra)


class TestVariants(unittest.TestCase):
    def setUp(self):
        self.session = _s.session_for(datetime(2026, 11, 4, 10, 0, tzinfo=_s.IST))
        _b.NOTIONAL = Decimal("10000")

    def test_breakout_fills_on_the_thrust_and_hits_target(self):
        cs = day([(9, 30, "101", "101.5", "100.9", "101.4"), (9, 35, "101.4", "103.5", "101.3", "103.2")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="breakout", last_entry=time(12, 30))
        self.assertTrue(r["filled"])
        self.assertEqual(r["how"], "target")
        self.assertGreater(r["net"], 0)

    def test_retest_waits_for_the_pullback(self):
        # thrust, then a pullback to the range high, then the move
        cs = day([(9, 30, "101", "101.5", "100.9", "101.4"), (9, 35, "101.4", "101.5", "100.9", "101.0"),
                  (9, 40, "101.0", "103.5", "100.95", "103.2")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="retest", last_entry=time(12, 30))
        self.assertTrue(r["filled"])
        self.assertEqual(r["fill_px"], Decimal("101"))

    def test_retest_misses_a_runaway(self):
        cs = day([(9, 30, "101", "101.5", "100.9", "101.4"), (9, 35, "101.4", "103.5", "101.3", "103.2")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="retest", last_entry=time(12, 30))
        self.assertTrue(r["setup"])
        self.assertFalse(r["filled"])

    def test_stop_is_honoured(self):
        cs = day([(9, 30, "101", "101.5", "100.9", "101.4"), (9, 35, "101.4", "101.5", "99.5", "99.8")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="breakout", last_entry=time(12, 30))
        self.assertEqual(r["how"], "stop")
        self.assertLess(r["net"], 0)

    def test_no_entry_after_cutoff(self):
        cs = day([(13, 0, "101", "101.5", "100.9", "101.4"), (13, 5, "101.4", "103.5", "101.3", "103.2")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="breakout", last_entry=time(12, 30))
        self.assertFalse(r["filled"])

    def test_flatten_closes_what_is_left(self):
        cs = day([(9, 30, "101", "101.5", "100.9", "101.4"), (12, 0, "101.4", "102", "101", "101.5"),
                  (15, 5, "101.6", "101.7", "101.5", "101.6"), (15, 10, "101.6", "101.7", "101.5", "101.6")])
        r = _b.simulate_day(cs, self.session, Decimal("0.05"), variant="breakout", last_entry=time(12, 30))
        self.assertEqual(r["how"], "flatten")


if __name__ == "__main__":
    unittest.main()
