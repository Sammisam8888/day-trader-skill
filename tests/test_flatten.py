"""The flatten: clear the stop, exit with a marketable limit, verify,
re-price, sweep."""

import os
import sys
import unittest
from decimal import Decimal as D
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, order, position, reset  # noqa: E402

import common  # noqa: E402
import flatten  # noqa: E402
import session as sess  # noqa: E402
from shoonya import LoginRequired  # noqa: E402

AT = datetime(2026, 11, 11, 15, 5, tzinfo=sess.IST)
sess.now = lambda clock=None: AT


class Closing(FakeBroker):
    """Positions close when a sell is placed, like a filled exit."""

    def place(self, inst, **kw):
        oid = super().place(inst, **kw)
        if kw["side"] == "S" and kw.get("trigger") is None:
            self._positions = [p for p in self._positions if p["token"] != inst.token]
        return oid


class TestFlatten(unittest.TestCase):
    def setUp(self):
        reset()

    def test_cancels_the_stop_then_sells_with_a_limit(self):
        b = Closing(positions=[position(NIFTY, 40, "281")],
                    orders=[order("S1", NIFTY, side="S", qty=40, trigger="280.3", price="278.9")],
                    quotes={NIFTY.token: {"ltp": "281.5", "bid": "281.49"}})
        self.assertEqual(flatten.run(b, now=AT, sleep=lambda s: None), 0)
        self.assertEqual(b.cancelled[0], "S1")
        s = b.placed[0]
        self.assertEqual((s["side"], s["qty"], s["trigger"]), ("S", 40, None))
        self.assertLess(s["price"], 281.49)

    def test_closes_manual_positions_too(self):
        b = Closing(positions=[position(SENSEX, 5, "900")], quotes={SENSEX.token: {"ltp": "901"}})
        flatten.run(b, now=AT, sleep=lambda s: None)
        self.assertEqual(b.placed[0]["symbol"], "SENSEXIETF")

    def test_an_unfilled_exit_is_repriced_further_through(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")],
                       quotes={NIFTY.token: {"ltp": "281", "bid": "280.99", "lc": "252.90", "uc": "309.10"}})
        self.assertEqual(flatten.run(b, now=AT, sleep=lambda s: None), 1)
        prices = [p["price"] for p in b.placed]
        self.assertEqual(len(prices), 4)
        self.assertTrue(prices[0] > prices[1] > prices[2] > prices[3], prices)
        self.assertEqual(prices[3], D("252.90"), "the must-close round sells at the lower band: fills like a market order")
        self.assertEqual(len(common.events("flatten_incomplete")), 1)

    def test_a_late_pass_goes_straight_to_must_close(self):
        from datetime import timedelta
        b = FakeBroker(positions=[position(NIFTY, -10, "281")],
                       quotes={NIFTY.token: {"ltp": "281", "lc": "252.90", "uc": "309.10"}})
        flatten.run(b, now=AT + timedelta(minutes=5), sleep=lambda s: None)
        self.assertEqual((b.placed[0]["side"], b.placed[0]["price"]), ("B", D("309.10")))

    def test_a_short_is_bought_back(self):
        b = FakeBroker(positions=[position(NIFTY, -10, "281")], quotes={NIFTY.token: {"ltp": "281"}})
        flatten.run(b, now=AT, sleep=lambda s: None)
        self.assertEqual(b.placed[0]["side"], "B")

    def test_sweeps_our_pending_entries_and_leaves_others(self):
        common.log("auto", {"event": "placed", "order_id": "P9", "symbol": "SENSEXIETF"})
        b = FakeBroker(orders=[order("P9", SENSEX, remarks=""), order("MINE", NIFTY, remarks="manual")])
        flatten.run(b, now=AT, sleep=lambda s: None)
        self.assertEqual(b.cancelled, ["P9"])

    def test_nothing_open_nothing_done(self):
        b = FakeBroker()
        self.assertEqual(flatten.run(b, now=AT, sleep=lambda s: None), 0)
        self.assertEqual(b.placed, [])

    def test_does_nothing_before_flat_by(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")])
        early = datetime(2026, 11, 8, 12, 0, tzinfo=sess.IST)
        self.assertEqual(flatten.run(b, now=early, sleep=lambda s: None), 0)
        self.assertEqual((b.placed, common.events("login_required")), ([], []))

    def test_not_logged_in_is_loud(self):
        class NoLogin(FakeBroker):
            def positions(self, intraday_only=True):
                raise LoginRequired("no session")
        self.assertEqual(flatten.run(NoLogin(), now=AT, sleep=lambda s: None), 1)
        self.assertIn("MANUALLY", common.events("login_required")[0]["action"])


if __name__ == "__main__":
    unittest.main()
