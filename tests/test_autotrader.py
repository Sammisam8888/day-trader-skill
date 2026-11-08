"""The scanner: what it places, what it refuses, and why."""

import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402  (sets the sandbox env first)
from _fake import NIFTY, SENSEX, FakeBroker, opening, order, position, reset  # noqa: E402

import autotrader  # noqa: E402
import common  # noqa: E402
import session as sess  # noqa: E402
from shoonya import LoginRequired  # noqa: E402

DAY = date(2026, 11, 11)                       # a Wednesday
AT = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)


def broker(**kw):
    kw.setdefault("candles", {NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")})
    kw.setdefault("quotes", {NIFTY.token: {"ltp": "280.50"}, SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
    return FakeBroker(**kw)


def events(name):
    return common.events(name, DAY.isoformat())


class Placement(unittest.TestCase):
    def setUp(self):
        reset()
        sess.now = lambda clock=None: AT

    def test_places_an_sl_lmt_buy_one_tick_over_the_range_high(self):
        b = broker()
        autotrader.run(b, now=AT)
        n = next(p for p in b.placed if p["symbol"] == "NIFTYBEES")
        self.assertEqual((n["side"], n["trigger"]), ("B", D("281.01")))
        self.assertEqual(n["price"], D("281.03"), "limit = trigger + 2 ticks of slippage")
        self.assertEqual(n["remarks"], "auto/NIFTYBEES")
        rec = next(r for r in events("placed") if r["symbol"] == "NIFTYBEES")
        # stop at the midpoint 280.30; risk 0.73 from 281.03; target 2R = 282.49
        self.assertEqual((rec["stop"], rec["target"]), ("280.30", "282.49"))
        self.assertIn("opening range", rec["why"])

    def test_two_slots_split_the_cash(self):
        b = broker()
        autotrader.run(b, now=AT)
        values = [p["qty"] * p["price"] for p in b.placed]
        self.assertEqual(len(values), 2)
        self.assertLessEqual(sum(values), D("25000"), "both filling still fits in cash at 1.0x")

    def test_a_large_concurrent_cap_does_not_shrink_orders(self):
        reset(TRADER_MAX_CONCURRENT="20")
        b = broker()
        autotrader.run(b, now=AT)
        values = [p["qty"] * p["price"] for p in b.placed]
        self.assertEqual(len(values), 2)
        self.assertGreater(min(values), D("11000"), "cash splits across 2 symbols, not 20 slots")

    def test_shadow_mode_places_nothing(self):
        reset(TRADER_LIVE="false")
        b = broker()
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])
        self.assertEqual(len(events("would_place")), 2)

    def test_never_chases_a_price_already_through_the_trigger(self):
        b = broker(quotes={NIFTY.token: {"ltp": "281.20"}, SENSEX.token: {"ltp": "898"}})
        autotrader.run(b, now=AT)
        self.assertNotIn("NIFTYBEES", [p["symbol"] for p in b.placed])
        self.assertTrue(any("not chasing" in r["reason"] for r in events("skip")))

    def test_below_the_range_low_is_no_setup(self):
        b = broker(quotes={NIFTY.token: {"ltp": "279.00"}, SENSEX.token: {"ltp": "898"}})
        autotrader.run(b, now=AT)
        self.assertNotIn("NIFTYBEES", [p["symbol"] for p in b.placed])

    def test_a_resting_order_is_not_placed_twice(self):
        b = broker()
        autotrader.run(b, now=AT)
        first = len(b.placed)
        autotrader.run(b, now=AT)
        self.assertEqual(len(b.placed), first)

    def test_a_held_symbol_is_not_entered_again(self):
        b = broker(positions=[position(NIFTY, 40, "281.02")])
        autotrader.run(b, now=AT)
        self.assertEqual([p["symbol"] for p in b.placed], ["SENSEXIETF"])

    def test_a_failed_order_is_logged_and_not_retried_in_the_run(self):
        b = broker(fail_place=True)
        autotrader.run(b, now=AT)
        self.assertEqual(len(events("order_failed")), 2)


class Guards(unittest.TestCase):
    def setUp(self):
        reset()
        sess.now = lambda clock=None: AT

    def test_kill_switch(self):
        common.KILL_SWITCH.write_text("test\n")
        b = broker()
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])

    def test_outside_the_entry_window(self):
        b = broker()
        autotrader.run(b, now=datetime(2026, 11, 11, 9, 20, tzinfo=sess.IST))
        autotrader.run(b, now=datetime(2026, 11, 11, 13, 0, tzinfo=sess.IST))
        self.assertEqual(b.placed, [])

    def test_weekend_does_nothing(self):
        b = broker()
        self.assertEqual(autotrader.run(b, now=datetime(2026, 11, 14, 10, 0, tzinfo=sess.IST)), 0)
        self.assertEqual(b.placed, [])

    def test_daily_loss_counts_open_losses(self):
        b = broker(positions=[position(SENSEX, 10, "900", unrealised="-1600")])
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])
        self.assertTrue(any("daily loss" in r["reason"] for r in events("halted")))

    def test_leverage_ceiling(self):
        b = broker(cash="10000", positions=[position(SENSEX, 12, "900")])
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])

    def test_fills_count_toward_the_trade_cap(self):
        reset(TRADER_MAX_TRADES="1")
        b = broker()
        autotrader.run(b, now=AT)
        oid = b.placed[0]["id"]
        for o in b._orders:
            if o["id"] == oid:
                o.update(status="COMPLETE", pending=False, filled=o["qty"])
        n = len(b.placed)
        autotrader.run(b, now=AT)
        self.assertEqual(len(b.placed), n)
        self.assertTrue(any("trade limit" in r["reason"] for r in events("halted")))

    def test_not_logged_in_is_logged_not_crashed(self):
        class NoLogin(FakeBroker):
            def cash(self):
                raise LoginRequired("not logged in to Shoonya today")
        self.assertEqual(autotrader.run(NoLogin(), now=AT), 0)
        self.assertEqual(len(events("login_required")), 1)

    def test_live_is_off_unless_explicitly_on(self):
        for v in ("", "0", "no", "False", "maybe"):
            reset(TRADER_LIVE=v)
            self.assertFalse(common.live(), v)
        reset(TRADER_LIVE="true")
        self.assertTrue(common.live())


class Muhurat(unittest.TestCase):
    def test_sunday_special_session_trades(self):
        reset()
        day = date(2026, 11, 8)
        at = datetime(2026, 11, 8, 14, 45, tzinfo=sess.IST)
        sess.now = lambda clock=None: at
        c = [_fake.candle(day, 14, 20, "280", "281", "279.9", "280.5"),
             _fake.candle(day, 14, 25, "280.5", "280.8", "279.6", "280.1"),
             _fake.candle(day, 14, 30, "280.1", "280.6", "279.8", "280.4")]
        b = FakeBroker(candles={NIFTY.token: c}, quotes={NIFTY.token: {"ltp": "280.5"}},
                       instruments=(NIFTY,))
        reset(TRADER_SYMBOLS="NIFTYBEES")
        autotrader.run(b, now=at)
        self.assertEqual([p["symbol"] for p in b.placed], ["NIFTYBEES"])


if __name__ == "__main__":
    unittest.main()
