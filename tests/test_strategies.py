"""Strategy journals, the bench's Rs 1 lakh wallets and day target, the liquidity models, nightly learnings."""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import common  # noqa: E402
import journal  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402

T0 = datetime(2026, 11, 26, 9, 15, tzinfo=sess.IST).timestamp()


def bars(prices):
    return [paper.Bar(int(T0) + 60 * i, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1000) for i, (o, c) in enumerate(prices)]


class Journal(unittest.TestCase):
    def setUp(self):
        reset()

    def test_rows_land_in_the_strategy_folder_and_ai_results_are_deduped(self):
        journal.row("orb15", "2026-11-26", time="09:31:00", event="open", stock="LT", side="BUY", qty=10, price=3900)
        f = journal.log_file("orb15", "2026-11-26")
        self.assertTrue(f.exists())
        self.assertIn("| 09:31:00 | open | LT | BUY | 10 | 3900 |", f.read_text())
        rec = {"ts": "2026-11-26T14:50:01+05:30", "event": "trade_result", "symbol": "LT", "side": "B",
               "net": "120.5", "win": True, "order_id": "P1", "setup": "orb"}
        journal.on_event(rec)
        journal.on_event(dict(rec, ts="2026-11-26T14:53:01+05:30"))
        self.assertEqual(journal.log_file(journal.AI, "2026-11-26").read_text().count("order P1"), 1)

    def test_common_log_feeds_the_ai_journal(self):
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        common.log("auto", {"event": "placed", "symbol": "SBIN", "side": "B", "qty": 50, "trigger": "800",
                            "stop": "796", "target": "808", "setup": "orb", "why": "test"})
        self.assertIn("| entry order | SBIN | BUY | 50 | 800 | 796 | 808 |",
                      journal.log_file(journal.AI, "2026-11-26").read_text())


class Wallet(unittest.TestCase):
    def setUp(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_PAPER_RISK="500", TRADER_BENCH_TARGET="1000")

    def test_target_is_a_milestone_and_the_model_keeps_trading(self):
        b = paper.Bench("2026-11-26")
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 1000, 100.0, 95.0, 120.0, T0)
        b.check_kills({"AAA": 101.5}, T0 + 600)
        self.assertNotIn("rsi2", b.killed)
        self.assertIn(("rsi2", "AAA"), b.open, "the winner keeps running")
        self.assertIn("rsi2", b.target_hit)
        b.check_kills({"AAA": 102.5}, T0 + 700)
        self.assertEqual(journal.log_file("rsi2", "2026-11-26").read_text().count("DAY TARGET"), 1)
        self.assertIn("Keeps trading", journal.log_file("rsi2", "2026-11-26").read_text())

    def test_target_closes_and_stops_the_model_when_asked(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_PAPER_RISK="500", TRADER_BENCH_TARGET="1000",
              TRADER_TARGET_STOPS="true")
        b = paper.Bench("2026-11-26")
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 1000, 100.0, 95.0, 120.0, T0)
        b.check_kills({"AAA": 101.5}, T0 + 600)
        self.assertIn("rsi2", b.killed)
        self.assertNotIn(("rsi2", "AAA"), b.open)
        self.assertIn("DAY TARGET", journal.log_file("rsi2", "2026-11-26").read_text())
        self.assertEqual(paper.capital(), D("100000"))


class Liquidity(unittest.TestCase):
    def test_a_sweep_of_equal_lows_that_snaps_back_is_a_long(self):
        # three swing lows near 100 (support), then a dip to 99.7 and a close back above 100
        px = [(101.2, 101.3)] * 20
        for _ in range(3):
            px += [(101.0, 100.6), (100.6, 100.2), (100.2, 100.05), (100.05, 100.4), (100.4, 100.9), (100.9, 101.1)]
        px += [(101.0, 100.8)] * 30
        px += [(100.8, 100.1), (100.1, 99.7), (99.75, 100.3)]
        p = paper.clamp("liq_sweep", {"lookback": 60, "touches": 3, "tol_pct": 0.12, "sweep_pct": 0.08, "reclaim_bars": 3})
        sig = paper.m_liq_sweep(bars(px), {}, p)
        self.assertIsNotNone(sig)
        self.assertEqual(sig[0], "B")
        self.assertLess(sig[1], 99.7)

    def test_pools_need_enough_touches(self):
        b = bars([(101.0, 100.0), (100.0, 101.0)] * 3 + [(101.0, 101.5)] * 10)
        self.assertEqual(paper.pools(b, "low", 5, 0.1), [])


class Learnings(unittest.TestCase):
    def setUp(self):
        reset()

    def test_one_learning_per_traded_strategy_and_a_note_for_idle_ones(self):
        import strategylearn
        journal.row("orb15", "2026-11-26", time="09:31:00", event="open", stock="LT", side="BUY", qty=10, price=3900)
        journal.row("orb15", "2026-11-26", time="10:02:00", event="close", stock="LT", side="BUY", qty=10, price=3920,
                    net="171.20", note="target")
        journal.note("rsi2", "2026-11-26", "nothing")
        asked = []
        out = strategylearn.run("2026-11-26", ask=lambda p: (asked.append(p) or "# Breakouts held\n\nDate: 2026-11-26\n"))
        self.assertEqual(out["written"], ["orb15"])
        self.assertEqual(out["no_trades"], ["rsi2"])
        self.assertIn("1 closed trades, 1 won", asked[0])
        self.assertTrue(journal.learn_file("orb15", "2026-11-26").read_text().startswith("# Breakouts held"))
        again = strategylearn.run("2026-11-26", ask=lambda p: "x")
        self.assertEqual(again["written"], [], "a day is reviewed once")


if __name__ == "__main__":
    unittest.main()
