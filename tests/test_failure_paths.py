"""Failure paths found in the 12-13 Nov audit. Each test is a broker state
that used to lose money or leave a position unprotected."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, FakeBroker, order, position, reset  # noqa: E402

import common  # noqa: E402
import fillmonitor as fm  # noqa: E402
import flatten  # noqa: E402
import session as sess  # noqa: E402

AT = datetime(2026, 11, 11, 10, 30, tzinfo=sess.IST)
sess.now = lambda clock=None: AT


def plan(oid="P1", side="B", stop="280.30", target="282.49", ts=None):
    common.log("auto", {"event": "placed", "order_id": oid, "symbol": "NIFTYBEES", "tsym": "NIFTYBEES-EQ",
                        "side": side, "token": NIFTY.token, "exch": "NSE", "tick": "0.01", "qty": 40,
                        "trigger": "281.01", "entry": "281.03", "stop": stop, "target": target,
                        "ts": ts or (AT - timedelta(minutes=5)).isoformat()})


def filled(oid, qty=40, side="B", avg="281.02"):
    return order(oid, NIFTY, side=side, status="COMPLETE", qty=qty, filled=qty, avg=avg, typ="LMT",
                 remarks="auto/NIFTYBEES")


def ev(name):
    return common.events(name)


class NeverOverCover(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_closed_trade_does_not_re_protect_a_later_trade(self):
        # P1 filled and was exited by Claude (exit order X1 filled); P2 then filled.
        plan("P1", stop="279.00")
        plan("P2", stop="280.30")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "279.00"})
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="CANCELED", trigger="279"),
                               order("X1", NIFTY, side="S", status="COMPLETE", qty=40, filled=40, typ="LMT",
                                     remarks="exit/NIFTYBEES"),
                               filled("P2")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "281.10"}})
        fm.tick(b, AT, {})
        self.assertEqual(len(b.placed), 1, "exactly one stop for the one open trade")
        self.assertEqual(b.placed[0]["trigger"], D("280.30"), "P2's stop, not the closed P1's")

    def test_no_stop_is_re_placed_while_claudes_exit_is_working(self):
        plan("P1")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="CANCELED", trigger="280.3"),
                               order("X1", NIFTY, side="S", status="OPEN", qty=40, price="280.50", typ="LMT",
                                     remarks="exit/NIFTYBEES")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "280.20"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [], "a second sell would reverse the position when both fill")
        self.assertEqual(b.modified[-1]["id"], "X1", "the exit is chased to a marketable price")
        self.assertLess(b.modified[-1]["price"], D("280.50"))

    def test_two_stops_never_exceed_the_position(self):
        plan("P1"); plan("P2")
        b = FakeBroker(orders=[filled("P1", qty=40), filled("P2", qty=40)],
                       positions=[position(NIFTY, 50, "281.02")],     # 30 sold by hand
                       quotes={NIFTY.token: {"ltp": "281.10"}})
        fm.tick(b, AT, {})
        self.assertLessEqual(sum(p["qty"] for p in b.placed), 50)


class StopsThatCannotWork(unittest.TestCase):
    def setUp(self):
        reset()

    def test_price_already_through_the_stop_exits_now(self):
        plan("P1")
        b = FakeBroker(orders=[filled("P1")], positions=[position(NIFTY, 40, "281.02")],
                       quotes={NIFTY.token: {"ltp": "280.00"}})
        fm.tick(b, AT, {})
        self.assertEqual(len(b.placed), 1)
        self.assertIsNone(b.placed[0]["trigger"], "a marketable limit, not an SL the exchange would reject")
        self.assertEqual(len(ev("stop_breached_exit")), 1)

    def test_a_stop_rejected_twice_exits_now(self):
        plan("P1")
        for i in (1, 2):
            common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": f"S{i}", "trigger": "280.30"})
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="REJECTED", trigger="280.3"),
                               order("S2", NIFTY, side="S", status="REJECTED", trigger="280.3")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "281.00"}})
        fm.tick(b, AT, {})
        self.assertIsNone(b.placed[0]["trigger"])
        self.assertEqual(len(ev("stop_breached_exit")), 1)

    def test_a_triggered_stop_left_behind_by_a_gap_is_chased(self):
        plan("P1")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="OPEN", price="278.90",
                                                   trigger="280.30", remarks="stop/NIFTYBEES")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "278.00"}})
        fm.tick(b, AT, {})
        m = b.modified[-1]
        self.assertEqual(m["id"], "S1")
        self.assertLess(m["price"], D("278.90"))
        self.assertEqual(len(ev("stop_chased")), 1)


class Targets(unittest.TestCase):
    def setUp(self):
        reset()
        plan("P1")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})

    def test_a_rejected_target_exit_puts_the_stop_back(self):
        common.log("fill", {"event": "target_exit", "parent": "P1", "order_id": "T1"})
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="CANCELED", trigger="280.3"),
                               order("T1", NIFTY, side="S", status="REJECTED", typ="LMT", remarks="tgt/NIFTYBEES")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "281.50"}})
        fm.tick(b, AT, {})
        self.assertEqual(len(b.placed), 1)
        self.assertEqual(b.placed[0]["trigger"], D("280.30"), "protected again, not left naked")

    def test_target_cancels_the_rest_of_a_partial_entry(self):
        b = FakeBroker(orders=[order("P1", NIFTY, status="OPEN", qty=40, filled=20, typ="LMT"),
                               order("S1", NIFTY, side="S", qty=20, price="278.9", trigger="280.3",
                                     remarks="stop/NIFTYBEES")],
                       positions=[position(NIFTY, 20, "281.02")], quotes={NIFTY.token: {"ltp": "282.60"}})
        fm.tick(b, AT, {})
        self.assertIn("P1", b.cancelled)
        self.assertEqual(b.placed[0]["qty"], 20)


class Strays(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_reversed_position_on_a_bot_stock_is_guarded(self):
        plan("P1")
        # long closed by stop + an extra sell: now short 40, nobody's trade
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="COMPLETE", qty=80, filled=80)],
                       positions=[position(NIFTY, -40, "280.00", ltp="281.60")],
                       quotes={NIFTY.token: {"ltp": "281.60"}})
        fm.tick(b, AT, {})
        self.assertEqual([(p["side"], p["qty"]) for p in b.placed], [("B", 40)], "bought back by the manual guard")


class ExitUsesTheLivePosition(unittest.TestCase):
    def setUp(self):
        reset()

    def test_exit_of_an_already_closed_position_sends_nothing(self):
        stale = position(NIFTY, 40, "281.02")
        b = FakeBroker(positions=[position(NIFTY, 0, "281.02")])
        self.assertIsNone(flatten.close_position(b, stale, remarks="exit/NIFTYBEES", sleep=lambda s: None))
        self.assertEqual(b.placed, [])

    def test_exit_uses_the_current_quantity(self):
        stale = position(NIFTY, 40, "281.02")
        b = FakeBroker(positions=[position(NIFTY, 15, "281.02")])
        flatten.close_position(b, stale, remarks="exit/NIFTYBEES", sleep=lambda s: None)
        self.assertEqual(b.placed[0]["qty"], 15)


if __name__ == "__main__":
    unittest.main()


class CatalystWaivesTheDirectionGate(unittest.TestCase):
    def setUp(self):
        reset()

    def test_fresh_bad_news_allows_a_short_only(self):
        import realtime
        common.log("news", {"event": "news_alert", "symbol": "LT", "score": -0.8, "headline": "L&T blacklisted",
                            "ts": (AT - timedelta(minutes=5)).isoformat()})
        self.assertIsNotNone(realtime.catalyst("LT", "S", AT))
        self.assertIsNone(realtime.catalyst("LT", "B", AT), "bad news never waives a long")
        self.assertIsNone(realtime.catalyst("SBIN", "S", AT), "only for the stock in the news")
        self.assertIsNone(realtime.catalyst("LT", "S", AT + timedelta(minutes=40)), "stale after 30 min")

    def test_a_price_shock_counts(self):
        import realtime
        common.log("realtime", {"event": "price_shock", "symbol": "LT", "move_pct": "-1.4",
                                "ts": (AT - timedelta(minutes=2)).isoformat()})
        self.assertIn("shock", realtime.catalyst("LT", "S", AT))


class ClaudeDownAlert(unittest.TestCase):
    def setUp(self):
        reset()

    def test_second_failure_in_a_row_alerts_the_phone(self):
        import brain
        sent = []
        orig = common.notify
        common.notify = lambda *a, **k: sent.append(a)
        try:
            brain._alert_if_down(AT)
            self.assertEqual(sent, [])
            common.log("brain", {"event": "brain_unavailable", "ts": AT.isoformat()})
            brain._alert_if_down(AT)
            self.assertEqual(len(sent), 1)
        finally:
            common.notify = orig
