"""The long/short playbook: each setup fires on its pattern, with a stop
where the idea is wrong and a 2R target, from closed bars only."""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import candle  # noqa: E402

import session as sess  # noqa: E402
import setups  # noqa: E402

DAY = date(2026, 11, 11)
TICK = D("0.05")


def bars(path, start=(9, 15), vol=10000):
    """(o, h, l, c) tuples as consecutive 5-minute candles."""
    out, (hh, mm) = [], start
    for o, h, l, c in path:
        out.append(candle(DAY, hh, mm, o, h, l, c, vol))
        mm += 5
        hh, mm = hh + mm // 60, mm % 60
    return out


OPEN = [(1000, 1004, 999, 1002), (1002, 1003, 998, 1000), (1000, 1003, 999, 1001)]   # range 998-1004


def names(found, side=None):
    return sorted({f["setup"] for f in found if side is None or f["side"] == side})


class Orb(unittest.TestCase):
    def test_both_sides_inside_the_range_before_eleven(self):
        f = setups.detect(bars(OPEN + [(1001, 1002, 1000, 1001)]), DAY, TICK, 0.0)
        long_ = next(x for x in f if x["setup"] == "orb" and x["side"] == "BUY")
        short = next(x for x in f if x["setup"] == "orb" and x["side"] == "SELL")
        self.assertEqual((long_["entry"], long_["stop"], long_["target"]), ("1004.05", "1001.00", "1010.15"))
        self.assertEqual((short["entry"], short["stop"]), ("997.95", "1001.00"))

    def test_trend_picks_the_side(self):
        f = setups.detect(bars(OPEN + [(1001, 1002, 1000, 1001)]), DAY, TICK, 0.6)
        self.assertEqual([x["side"] for x in f if x["setup"] == "orb"], ["BUY"])


class Pullback(unittest.TestCase):
    def test_long_on_a_green_bounce_off_vwap_in_an_uptrend(self):
        up = OPEN + [(1001, 1006, 1001, 1005), (1005, 1009, 1004, 1008), (1008, 1010, 1006, 1009),
                     (1004, 1007, 1003.5, 1006.5)]
        f = setups.detect(bars(up), DAY, TICK, 0.8)
        self.assertIn("vwap_pullback", names(f, "BUY"))
        self.assertNotIn("vwap_pullback", names(f, "SELL"))

    def test_short_on_a_red_rejection_at_vwap_in_a_downtrend(self):
        dn = OPEN + [(1001, 1001, 995, 996), (996, 997, 991, 992), (992, 994, 990, 991),
                     (996, 997.5, 990.5, 993)]
        f = setups.detect(bars(dn), DAY, TICK, -0.8)
        self.assertIn("vwap_pullback", names(f, "SELL"))


class FailedBreak(unittest.TestCase):
    def test_a_break_above_that_closes_back_inside_is_a_short(self):
        f = setups.detect(bars(OPEN + [(1001, 1003, 1000, 1002), (1002, 1006, 1001, 1005), (1005, 1005.5, 1000, 1001)]),
                          DAY, TICK, 0.0)
        s = next(x for x in f if x["setup"] == "failed_break")
        self.assertEqual((s["side"], s["stop"]), ("SELL", "1006.05"))


class DayBreak(unittest.TestCase):
    def test_after_ten_thirty_an_uptrend_at_the_high_breaks_out(self):
        path = OPEN + [(1001 + i, 1003 + i, 1000 + i, 1002 + i) for i in range(15)]
        f = setups.detect(bars(path), DAY, TICK, 0.9)
        self.assertIn("day_break", names(f, "BUY"))


class Hygiene(unittest.TestCase):
    def test_a_forming_bar_is_ignored(self):
        b = bars(OPEN + [(1001, 1002, 1000, 1001)])
        at = datetime(2026, 11, 11, 9, 32, tzinfo=sess.IST)       # the 09:30 bar is still forming
        self.assertEqual(setups.detect(b, DAY, TICK, 0.0, now=at)[0]["why"].split(":")[0], "opening range 998-1004")
        self.assertEqual(len(setups.detect(b[:2], DAY, TICK, 0.0)), 0)

    def test_risk_too_tight_or_too_wide_is_dropped(self):
        tight = [(1000, 1000.5, 999.9, 1000.2)] * 3 + [(1000.2, 1000.3, 1000, 1000.1)]
        self.assertEqual(setups.detect(bars(tight), DAY, TICK, 0.0), [])

    def test_rank(self):
        r = setups.rank({"A": {"score": 0.8}, "B": {"score": -0.6}, "C": {"score": 0.2}, "NIFTYBEES": {"score": 1}})
        self.assertEqual((r["strongest"], r["weakest"]), (["A", "C"], ["B"]))


if __name__ == "__main__":
    unittest.main()
