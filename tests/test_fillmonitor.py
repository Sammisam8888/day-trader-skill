"""The fill monitor: stop on fill, target, trailing, pruning, manual,
shorts, and the flatten hand-off. Every case is a scripted broker state."""

import os
import sys
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, order, position, reset  # noqa: E402

import common  # noqa: E402
import fillmonitor as fm  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 10, 30, tzinfo=sess.IST)
sess.now = lambda clock=None: AT


def plan(oid="P1", inst=NIFTY, qty=40, ts=None, side="B", **kw):
    rec = {"event": "placed", "order_id": oid, "symbol": inst.symbol, "tsym": inst.tsym, "side": side,
           "token": inst.token, "exch": "NSE", "tick": "0.01", "qty": qty,
           "trigger": "281.01", "entry": "281.03", "stop": "280.30", "target": "282.49",
           "range_high": "281.00", "range_low": "279.60",
           "ts": ts or (AT - timedelta(minutes=5)).isoformat()}
    rec.update(kw)
    common.log("auto", rec)


def filled_entry(oid="P1", inst=NIFTY, qty=40, avg="281.02"):
    return order(oid, inst, status="COMPLETE", qty=qty, filled=qty, avg=avg, remarks=f"auto/{inst.symbol}")


def ev(name):
    return common.events(name)


class Protect(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_fill_gets_a_sell_stop_at_the_exchange(self):
        plan()
        b = FakeBroker(orders=[filled_entry()], positions=[position(NIFTY, 40, "281.02")],
                       quotes={NIFTY.token: {"ltp": "281.10"}})
        out = fm.tick(b, AT, {})
        self.assertEqual(out["protected"], 1)
        s = b.placed[0]
        self.assertEqual((s["side"], s["qty"], s["trigger"]), ("S", 40, D("280.30")))
        self.assertLess(s["price"], s["trigger"], "stop limit sits under the trigger to fill in a gap")
        self.assertEqual(ev("stop_placed")[0]["parent"], "P1")

    def test_the_stop_is_placed_once(self):
        plan()
        b = FakeBroker(orders=[filled_entry()], positions=[position(NIFTY, 40, "281.02")],
                       quotes={NIFTY.token: {"ltp": "281.10"}})
        fm.tick(b, AT, {})
        fm.tick(b, AT, {})
        self.assertEqual(len(b.placed), 1)

    def test_a_partial_fill_that_grows_resizes_the_stop(self):
        plan()
        b = FakeBroker(orders=[order("P1", NIFTY, status="OPEN", qty=40, filled=10)],
                       positions=[position(NIFTY, 10, "281.02")], quotes={NIFTY.token: {"ltp": "281.1"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed[0]["qty"], 10)
        b._orders[0].update(filled=40, status="COMPLETE", pending=False)
        b._positions = [position(NIFTY, 40, "281.02")]
        fm.tick(b, AT, {})
        self.assertEqual(b.modified[-1]["qty"], 40)

    def test_a_failed_stop_is_retried_and_alerted(self):
        plan()
        b = FakeBroker(orders=[filled_entry()], positions=[position(NIFTY, 40, "281.02")],
                       quotes={NIFTY.token: {"ltp": "281.1"}}, fail_place=True)
        fm.tick(b, AT, {})
        self.assertEqual(len(ev("stop_failed")), 1)
        b.fail_place = False
        fm.tick(b, AT, {})
        self.assertEqual(len(ev("stop_placed")), 1)


class TargetAndTrail(unittest.TestCase):
    def setUp(self):
        reset()
        plan()
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1",
                            "symbol": "NIFTYBEES", "qty": 40, "trigger": "280.30"})

    def broker(self, ltp, **kw):
        return FakeBroker(orders=[filled_entry(), order("S1", NIFTY, side="S", qty=40, price="278.90",
                                                        trigger="280.30", remarks="stop/NIFTYBEES")],
                          positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": ltp}}, **kw)

    def test_target_cancels_the_stop_then_sells(self):
        b = self.broker("282.50")
        fm.tick(b, AT, {})
        self.assertEqual(b.cancelled, ["S1"])
        self.assertEqual((b.placed[0]["side"], b.placed[0]["qty"]), ("S", 40))
        self.assertIsNone(b.placed[0]["trigger"], "a limit, not a stop")
        self.assertEqual(len(ev("target_exit")), 1)

    def test_an_adjusted_target_is_honoured(self):
        common.log("auto", {"event": "plan_adjusted", "parent": "P1", "target": "283.50"})
        b = self.broker("282.60")
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [], "old target 282.49 passed, new one 283.50 not yet")
        b2 = self.broker("283.55")
        fm.tick(b2, AT, {})
        self.assertEqual(len(common.events("target_exit")), 1)

    def test_if_the_stop_cannot_be_cancelled_nothing_is_sold(self):
        b = self.broker("282.50", fail_cancel={"S1"})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [], "the stop has probably executed: selling again would go short")

    def test_under_one_r_the_stop_stays(self):
        b = self.broker("281.50")
        fm.tick(b, AT, {})
        self.assertEqual(b.modified, [])

    def test_at_one_r_the_stop_moves_to_entry(self):
        b = self.broker("281.76")                     # entry 281.02 + R 0.72
        fm.tick(b, AT, {})
        self.assertEqual(b.modified[0]["trigger"], D("281.02"))

    def test_further_up_it_trails_and_never_moves_down(self):
        b = self.broker("282.20")                     # +1.64R
        fm.tick(b, AT, {})
        # R is measured from the actual fill (281.02), so 0.72: 282.20 - 0.72
        self.assertEqual(b.modified[0]["trigger"], D("281.48"))
        self.assertIsNone(fm.new_stop(D("281.02"), D("280.30"), D("281.80"), D("0.01"), D("281.48")))


class Prune(unittest.TestCase):
    def setUp(self):
        reset()

    def test_resting_entries_go_when_fills_reach_the_cap(self):
        plan("P1"); plan("P2", SENSEX); plan("P3", NIFTY)
        b = FakeBroker(orders=[filled_entry("P1"), filled_entry("P2", SENSEX, 10, "900"),
                               order("P3", NIFTY, remarks="auto/NIFTYBEES")],
                       positions=[position(NIFTY, 40, "281.02"), position(SENSEX, 10, "900")],
                       quotes={NIFTY.token: {"ltp": "281.1"}, SENSEX.token: {"ltp": "900.5"}})
        fm.tick(b, AT, {})
        self.assertIn("P3", b.cancelled)
        self.assertNotIn("P1", b.cancelled)

    def test_one_more_fill_past_cash_is_cancelled(self):
        plan("P1", qty=60); plan("P2", SENSEX, qty=20, entry="900")
        b = FakeBroker(cash="25000", orders=[filled_entry("P1", qty=60), order("P2", SENSEX, qty=20)],
                       positions=[position(NIFTY, 60, "281.02")],
                       quotes={NIFTY.token: {"ltp": "281.1"}, SENSEX.token: {"ltp": "899"}})
        fm.tick(b, AT, {})
        self.assertIn("P2", b.cancelled)            # 16,861 held + 18,000 > 25,000

    def test_price_through_the_planned_stop_kills_the_setup(self):
        plan("P1")
        b = FakeBroker(orders=[order("P1", NIFTY)], quotes={NIFTY.token: {"ltp": "280.20"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.cancelled, ["P1"])

    def test_inside_the_range_it_stays(self):
        plan("P1")
        b = FakeBroker(orders=[order("P1", NIFTY)], quotes={NIFTY.token: {"ltp": "280.80"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.cancelled, [])

    def test_ttl(self):
        reset(TRADER_RESTING_TTL_MIN="30")
        plan("P1", ts=(AT - timedelta(minutes=45)).isoformat())
        b = FakeBroker(orders=[order("P1", NIFTY)], quotes={NIFTY.token: {"ltp": "280.80"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.cancelled, ["P1"])

    def test_after_last_entry_everything_resting_goes_but_stops_stay(self):
        plan("P1"); plan("P2", SENSEX, entry="900.50", stop="899", target="905")
        common.log("fill", {"event": "stop_placed", "parent": "P2", "order_id": "S2", "trigger": "899"})
        late = datetime(2026, 11, 11, 13, 0, tzinfo=sess.IST)
        b = FakeBroker(orders=[order("P1", NIFTY), filled_entry("P2", SENSEX, 10, "900"),
                               order("S2", SENSEX, side="S", qty=10, trigger="899", price="894")],
                       positions=[position(SENSEX, 10, "900")],
                       quotes={NIFTY.token: {"ltp": "280.5"}, SENSEX.token: {"ltp": "900"}})
        fm.tick(b, late, {})
        self.assertEqual(b.cancelled, ["P1"])


class ManualAndShorts(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_manual_position_is_sold_at_its_threshold(self):
        b = FakeBroker(positions=[position(NIFTY, 10, "280.00")], quotes={NIFTY.token: {"ltp": "282.90"}})
        out = fm.tick(b, AT, {})
        self.assertEqual(out["sold"], 1)
        self.assertEqual((b.placed[0]["side"], b.placed[0]["qty"]), ("S", 10))

    def test_inside_the_band_it_is_held(self):
        b = FakeBroker(positions=[position(NIFTY, 10, "280.00")], quotes={NIFTY.token: {"ltp": "280.50"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [])

    def test_a_manual_position_with_its_own_sell_is_left_to_it(self):
        b = FakeBroker(positions=[position(NIFTY, 10, "280.00")],
                       orders=[order("X", NIFTY, side="S", qty=10, trigger="279", price="278")],
                       quotes={NIFTY.token: {"ltp": "290"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [])

    def test_a_manual_short_is_covered_at_its_threshold(self):
        b = FakeBroker(positions=[position(NIFTY, -40, "281.00")], quotes={NIFTY.token: {"ltp": "278.10"}})
        fm.tick(b, AT, {})
        self.assertEqual((b.placed[0]["side"], b.placed[0]["qty"]), ("B", 40))

    def test_a_manual_short_inside_the_band_is_held(self):
        b = FakeBroker(positions=[position(NIFTY, -40, "281.00")], quotes={NIFTY.token: {"ltp": "280.50"}})
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [])


class Shorts(unittest.TestCase):
    """A bot short: stop is a BUY above, target below, trail moves down."""

    def setUp(self):
        reset()
        plan("P1", side="S", entry="279.53", stop="280.30", target="278.00")

    def short_book(self, ltp, stop_id=None):
        orders = [order("P1", NIFTY, side="S", status="COMPLETE", qty=40, filled=40, avg="279.55")]
        if stop_id:
            orders.append(order(stop_id, NIFTY, side="B", qty=40, trigger="280.30", price="281.71", remarks="stop/NIFTYBEES"))
        return FakeBroker(orders=orders, positions=[position(NIFTY, -40, "279.55")], quotes={NIFTY.token: {"ltp": ltp}})

    def test_the_stop_is_a_buy_above(self):
        b = self.short_book("279.40")
        fm.tick(b, AT, {})
        s = b.placed[0]
        self.assertEqual((s["side"], s["qty"], s["trigger"]), ("B", 40, D("280.30")))
        self.assertGreater(s["price"], s["trigger"])

    def test_target_below_buys_back(self):
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        b = self.short_book("277.90", stop_id="S1")
        fm.tick(b, AT, {})
        self.assertEqual(b.cancelled, ["S1"])
        self.assertEqual(b.placed[0]["side"], "B")

    def test_trail_moves_the_stop_down_only(self):
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        b = self.short_book("278.78", stop_id="S1")                 # +1R from 279.55 (R 0.75)
        fm.tick(b, AT, {})
        self.assertEqual(b.modified[0]["trigger"], D("279.55"))
        self.assertIsNone(fm.new_stop(D("279.55"), D("280.30"), D("279.00"), D("0.01"), D("279.55"), "S"))


class FlattenHandOff(unittest.TestCase):
    def setUp(self):
        reset()
        _fake  # sandbox already set

    def test_flatten_runs_once_at_flat_by_and_once_five_minutes_later(self):
        calls = []
        orig = fm.flatten_mod.run
        fm.flatten_mod.run = lambda broker, now=None: calls.append(now)
        try:
            state = {}
            b = FakeBroker()
            for hh, mm in ((15, 4), (15, 5), (15, 6), (15, 10), (15, 11)):
                fm.tick(b, datetime(2026, 11, 11, hh, mm, tzinfo=sess.IST), state)
        finally:
            fm.flatten_mod.run = orig
        self.assertEqual([(c.hour, c.minute) for c in calls], [(15, 5), (15, 10)])

    def test_muhurat_flattens_at_its_own_time(self):
        calls = []
        orig = fm.flatten_mod.run
        fm.flatten_mod.run = lambda broker, now=None: calls.append(now)
        try:
            fm.tick(FakeBroker(), datetime(2026, 11, 8, 15, 10, tzinfo=sess.IST), {})
        finally:
            fm.flatten_mod.run = orig
        self.assertEqual(len(calls), 1)

    def test_idle_outside_the_session(self):
        self.assertTrue(fm.tick(FakeBroker(), datetime(2026, 11, 11, 7, 0, tzinfo=sess.IST), {})["idle"])
        self.assertTrue(fm.tick(FakeBroker(), datetime(2026, 11, 14, 11, 0, tzinfo=sess.IST), {})["idle"])


if __name__ == "__main__":
    unittest.main()
