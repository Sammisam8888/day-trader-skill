"""The paper bench: never touches the broker; opens, closes, charges, kills."""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import paper  # noqa: E402
import session as sess  # noqa: E402

T0 = datetime(2026, 11, 18, 9, 15, tzinfo=sess.IST).timestamp()


def feed_path(bench, sym, prices, start=T0, step=60, vol=1000):
    cum = 0
    for i, px in enumerate(prices):
        cum += vol
        bench.feed(sym, start + i * step + 1, px, cum)


class NoBroker(unittest.TestCase):
    def test_the_bench_cannot_reach_the_broker(self):
        src = open(paper.__file__).read()
        for bad in ("import shoonya", "from shoonya", "broker.place", ".place(", "autotrader"):
            self.assertNotIn(bad, src)


class Bars(unittest.TestCase):
    def test_minutes_and_volume(self):
        s = paper.Series()
        self.assertFalse(s.add(T0 + 1, 100, 10))
        s.add(T0 + 20, 101, 30)
        self.assertTrue(s.add(T0 + 61, 99, 50))
        b = s.closed()[0]
        self.assertEqual((b.o, b.h, b.l, b.c, b.v), (100, 101, 100, 101, 20))


class Trading(unittest.TestCase):
    def setUp(self):
        reset()

    def test_orb_breakout_opens_then_hits_target_after_charges(self):
        b = paper.Bench("2026-11-18", write=False)
        rng = [100, 100.2, 99.9, 100.1, 100.0] * 3                 # 09:15-09:30 range 99.9-100.2
        up = [100.1, 100.3, 100.5, 100.8, 101.0, 101.2]
        feed_path(b, "AAA", rng + up)
        opened = [k for k in b.open if k[1] == "AAA"]
        self.assertTrue(any(k[0] in ("orb5", "orb15") for k in opened) or b.trades)
        feed_path(b, "AAA", [101.5, 102.0, 102.5], start=T0 + 21 * 60)
        self.assertGreater(sum(b.trades.values()), 0)
        self.assertTrue(all(isinstance(v, D) for v in b.realised.values()))

    def test_everything_is_flat_at_14_50(self):
        b = paper.Bench("2026-11-18", write=False)
        b.open[("orb5", "AAA")] = paper.Trade("orb5", "AAA", "B", 100, 100.0, 90.0, 120.0, T0)
        t = datetime(2026, 11, 18, 14, 50, 5, tzinfo=sess.IST).timestamp()
        b.on_price("AAA", 101.0, t)
        self.assertEqual(b.open, {})
        self.assertEqual(b.trades["orb5"], 1)

    def test_model_kill_closes_and_stops_that_model_only(self):
        b = paper.Bench("2026-11-18", write=False)
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 500, 100.0, 90.0, 120.0, T0)
        b.open[("orb5", "BBB")] = paper.Trade("orb5", "BBB", "B", 500, 100.0, 90.0, 120.0, T0)
        b.check_kills({"AAA": 99.8, "BBB": 100.0}, T0 + 3600)
        self.assertIn("rsi2", b.killed)
        self.assertNotIn("orb5", b.killed)
        self.assertIn(("orb5", "BBB"), b.open)

    def test_no_entries_after_14_35(self):
        b = paper.Bench("2026-11-18", write=False)
        late = datetime(2026, 11, 18, 14, 36, tzinfo=sess.IST).timestamp()
        feed_path(b, "AAA", [100 + i * 0.5 for i in range(10)], start=late)
        self.assertEqual(b.open, {})

    def test_charges_make_a_flat_trade_a_loss(self):
        self.assertLess(paper.round_trip_net("B", 100.0, 100.0, 500), 0)

    def test_summary_shape(self):
        b = paper.Bench("2026-11-18", write=False)
        s = b.summary({})
        self.assertEqual(set(s["models"]), set(paper.MODELS))
        self.assertEqual(D(s["total"]), 0)


if __name__ == "__main__":
    unittest.main()
