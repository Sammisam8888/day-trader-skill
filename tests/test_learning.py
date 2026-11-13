"""The learning loop: trades rebuilt from the order book, Claude's review
merged into a lessons store, lessons fed back into every decision."""

import json
import os
import shutil
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, candle, order, reset  # noqa: E402

import common  # noqa: E402
import learning  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 13)
AT = datetime(2026, 11, 13, 15, 50, tzinfo=sess.IST)


def ts(hh, mm):
    return f"{hh:02d}:{mm:02d}:00 13-11-2026"


def placed(oid, inst=NIFTY, side="B", **kw):
    return {"event": "placed", "order_id": oid, "symbol": inst.symbol, "token": inst.token, "side": side,
            "stop": "280.30", "target": "283.00", "setup": "orb", "why": "breakout", "lessons": ["L001"], **kw}


def filled(oid, inst=NIFTY, side="B", qty=40, avg="281.00", at=(10, 0), remarks=""):
    o = order(oid, inst, side=side, status="COMPLETE", qty=qty, filled=qty, avg=avg, typ="LMT", remarks=remarks)
    o["time"] = ts(*at)
    return o


class Book(unittest.TestCase):
    def test_a_long_closed_by_its_stop(self):
        t = learning.trades_from_book([placed("E1")],
                                      [filled("E1"), filled("S1", side="S", avg="280.20", at=(10, 20), remarks="stop/NIFTYBEES")])
        self.assertEqual(len(t), 1)
        self.assertEqual((t[0]["exits"][0]["how"], t[0]["gross"]), ("stop", D("-32.00")))
        self.assertLess(t[0]["net"], t[0]["gross"], "charges come off")

    def test_a_short_booked_by_the_lock_and_a_second_trade_fifo(self):
        t = learning.trades_from_book(
            [placed("E1", side="S"), placed("E2", side="S")],
            [filled("E1", side="S", avg="281.00", at=(10, 0)), filled("X1", side="B", avg="280.00", at=(10, 30), remarks="lock/NIFTYBEES"),
             filled("E2", side="S", avg="282.00", at=(11, 0)), filled("X2", side="B", avg="283.00", at=(11, 30), remarks="exit/NIFTYBEES")])
        self.assertEqual([(x["exits"][0]["how"], x["gross"]) for x in t],
                         [("profit lock", D("40.00")), ("Claude exit", D("-40.00"))])

    def test_an_open_trade_is_not_reviewed(self):
        self.assertEqual(learning.trades_from_book([placed("E1")], [filled("E1")]), [])

    def test_profit_given_back_is_measured(self):
        tr = learning.trades_from_book([placed("E1")],
                                       [filled("E1"), filled("S1", side="S", avg="281.05", at=(10, 20), remarks="stop/NIFTYBEES")])[0]
        bars = [candle(DAY, 10, 0, 281, 284.80, 280.90, 284), candle(DAY, 10, 10, 284, 284, 281, 281)]
        x = learning.excursion(tr, bars)
        self.assertEqual(x["best_open_profit"], D("152"))
        self.assertEqual(x["gave_back"], D("150"))


class Store(unittest.TestCase):
    def setUp(self):
        reset()
        shutil.rmtree(learning.store_dir(), ignore_errors=True)

    def test_new_reinforce_contradict_retire(self):
        les = {}
        ch = learning.merge(les, {"lessons": [{"id": "NEW", "action": "new", "rule": "When +100 open, lock half",
                                               "scope": "exit", "evidence": "LT 13:40"}]}, "2026-11-13")
        self.assertEqual(ch["new"], ["L001"])
        learning.merge(les, {"lessons": [{"id": "L001", "action": "contradict", "evidence": "x"}]}, "2026-11-16")
        self.assertEqual(les["L001"]["status"], "active", "one bad day does not kill a lesson")
        learning.merge(les, {"lessons": [{"id": "L001", "action": "contradict", "evidence": "y"}]}, "2026-11-17")
        self.assertEqual(les["L001"]["status"], "retired")

    def test_reinforced_lessons_rank_first_and_unknown_ids_are_ignored(self):
        les = {}
        learning.merge(les, {"lessons": [{"action": "new", "rule": "A"}, {"action": "new", "rule": "B"}]}, "d1")
        learning.merge(les, {"lessons": [{"id": "L002", "action": "reinforce"}, {"id": "L999", "action": "reinforce"}]}, "d2")
        learning.save(les)
        self.assertEqual([x["id"] for x in learning.for_prompt()], ["L002", "L001"])

    def test_at_most_four_new_lessons_a_day(self):
        les = {}
        learning.merge(les, {"lessons": [{"action": "new", "rule": f"r{i}"} for i in range(9)]}, "d1")
        self.assertEqual(len(les), 4)


class NightlyReview(unittest.TestCase):
    def setUp(self):
        reset()
        shutil.rmtree(learning.store_dir(), ignore_errors=True)
        sess.now = lambda clock=None: AT

    def test_review_writes_the_journal_and_the_lessons(self):
        common.log("auto", placed("E1"))
        book = [filled("E1"), filled("S1", side="S", avg="280.20", at=(10, 20), remarks="stop/NIFTYBEES")]
        seen = {}
        def run(prompt):
            seen["prompt"] = prompt
            return json.dumps({"day_summary": "One stop-out.", "trades": [{"order_id": "E1", "verdict": "bad entry",
                               "post_mortem": "Bought into resistance."}],
                               "lessons": [{"id": "NEW", "action": "new", "rule": "When price is under VWAP, skip ORB longs",
                                            "scope": "entry", "evidence": "NIFTYBEES 10:00"}]})
        out = learning.run_day(DAY.isoformat(), None, run=run, orders=book)
        self.assertEqual(out["new"], ["L001"])
        self.assertIn('"how": "stop"', seen["prompt"])
        j = json.loads(learning.journal_path(DAY.isoformat()).read_text())
        self.assertEqual(j["reviews"][0]["verdict"], "bad entry")
        self.assertEqual(learning.for_prompt()[0]["rule"], "When price is under VWAP, skip ORB longs")

    def test_a_failed_review_changes_nothing(self):
        common.log("auto", placed("E1"))
        book = [filled("E1"), filled("S1", side="S", avg="280.20", at=(10, 20), remarks="stop/NIFTYBEES")]
        out = learning.run_day(DAY.isoformat(), None, run=lambda p: "not json", orders=book)
        self.assertIn("error", out)
        self.assertEqual(learning.load(), {})


if __name__ == "__main__":
    unittest.main()


class LessonsReachEveryDecision(unittest.TestCase):
    def test_the_prompt_carries_the_lessons_and_the_trade_records_the_ones_cited(self):
        import autotrader
        import brain
        from _fake import FakeBroker, opening
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0")
        shutil.rmtree(learning.store_dir(), ignore_errors=True)
        learning.save({"L001": {"rule": "Lock half of any +100 open profit", "support": 3, "against": 0,
                                "status": "active", "updated": "2026-11-13"}})
        at = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)
        sess.now = lambda clock=None: at
        brain.find_claude = lambda: "/x/claude"
        b = FakeBroker(candles={NIFTY.token: opening(at.date()), SENSEX.token: opening(at.date(), "900.00", "896.00")},
                       quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                               SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
        seen = {}
        reply = json.dumps({"view": "t", "decisions": [
            {"action": "enter", "symbol": "NIFTYBEES", "side": "BUY", "order": "stop", "entry": 281.05,
             "stop": 280.30, "target": 282.80, "size": 1.0, "lessons": ["L001"], "reason": "t"}]})
        def run(prompt):
            seen["p"] = prompt
            return reply
        try:
            autotrader.run(b, now=at, decide=lambda *a, **k: brain.decide(*a, **k, run=run))
        finally:
            sess.now = lambda clock=None: AT
        self.assertIn("Lock half of any +100 open profit", seen["p"])
        self.assertEqual(common.events("placed", "2026-11-11")[0]["lessons"], ["L001"])
