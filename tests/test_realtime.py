"""Real-time layer: price shocks, breaking items for Claude, market internals."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import reset  # noqa: E402

import common  # noqa: E402
import realtime  # noqa: E402
import session as sess  # noqa: E402

AT = datetime(2026, 11, 12, 13, 55, tzinfo=sess.IST)


class Shock(unittest.TestCase):
    def setUp(self):
        reset()
        sess.now = lambda clock=None: AT

    def feed(self, prices, state, start=AT):
        out = []
        for i, p in enumerate(prices):
            out += realtime.watch_prices({"11483": D(str(p))}, {"11483": "LT"}, start + timedelta(minutes=i), state)
        return out

    def test_a_one_percent_drop_in_five_minutes_is_a_shock(self):
        state = {}
        found = self.feed([4200, 4198, 4190, 4175, 4150], state)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["symbol"], "LT")
        self.assertLess(D(found[0]["move_pct"]), D("-0.9"))

    def test_quiet_prices_are_not(self):
        self.assertEqual(self.feed([4200, 4201, 4199, 4202, 4198, 4200], {}), [])

    def test_one_alert_per_fifteen_minutes(self):
        state = {}
        self.feed([4200, 4150], state)
        again = self.feed([4150, 4100], state, AT + timedelta(minutes=3))
        self.assertEqual(again, [])

    def test_breaking_lists_recent_shocks_and_news(self):
        common.log("realtime", {"event": "price_shock", "symbol": "LT", "move_pct": "-1.2"})
        common.log("newswatch", {"event": "news_alert", "symbol": "LT", "score": -0.9,
                                 "headline": "Karnataka blacklists L&T"})
        b = realtime.breaking(AT)
        self.assertEqual({x["event"] for x in b}, {"price_shock", "news_alert"})


class Internals(unittest.TestCase):
    def test_breadth_and_sectors(self):
        reset(TRADER_INTERNALS="true")
        data = {"data": [{"index": "NIFTY 50", "percentChange": 0.4, "advances": "18", "declines": "32", "unchanged": "0"},
                         {"index": "NIFTY BANK", "percentChange": 1.1}, {"index": "NIFTY IT", "percentChange": -0.8}]}
        v = realtime.internals(get=lambda api: data, max_age_s=0)
        self.assertEqual((v["advances"], v["declines"]), (18, 32))
        self.assertIn("narrow", v["breadth"])
        self.assertEqual(v["sectors"]["NIFTY IT"], -0.8)

    def test_off_means_no_network(self):
        reset()
        self.assertIsNone(realtime.internals(get=lambda api: 1 / 0))


if __name__ == "__main__":
    unittest.main()
