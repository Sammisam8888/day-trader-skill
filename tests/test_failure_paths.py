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


class ClaudeOutputIsReadStrictly(unittest.TestCase):
    def test_only_buy_or_sell_is_a_side(self):
        import autotrader
        self.assertEqual(autotrader.parse_side("buy"), "B")
        self.assertEqual(autotrader.parse_side("SHORT"), "S")
        for bad in ("", None, "hold", "BUUY", "flat"):
            self.assertIsNone(autotrader.parse_side(bad), f"{bad!r} must never become a short")


class CancelThenReEnter(unittest.TestCase):
    def test_cancelling_a_stale_entry_frees_its_slot_in_the_same_response(self):
        import json
        import autotrader
        import brain
        from _fake import SENSEX, opening
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_CONCURRENT="3",
              TRADER_MAX_ENTRIES_PER_SYMBOL="3")
        day = AT.date()
        at = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)
        sess.now = lambda clock=None: at
        brain.find_claude = lambda: "/x/claude"
        common.log("auto", {"event": "placed", "order_id": "OLD", "symbol": "NIFTYBEES", "side": "B",
                            "token": NIFTY.token, "risk": "30"})
        b = FakeBroker(orders=[order("OLD", NIFTY, typ="SL-LMT", remarks="auto/NIFTYBEES")],
                       candles={NIFTY.token: opening(day), SENSEX.token: opening(day, "900.00", "896.00")},
                       quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                               SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
        reply = json.dumps({"view": "t", "decisions": [
            {"action": "cancel", "order_id": "OLD", "reason": "stale"},
            {"action": "enter", "symbol": "NIFTYBEES", "side": "BUY", "order": "stop", "entry": 281.05,
             "stop": 280.30, "target": 282.80, "size": 1.0, "reason": "re-price"}]})
        try:
            autotrader.run(b, now=at, decide=lambda *a, **k: brain.decide(*a, **k, run=lambda p: reply))
        finally:
            sess.now = lambda clock=None: AT
        self.assertEqual(b.cancelled, ["OLD"])
        self.assertEqual(len(b.placed), 1, "the re-priced entry goes in")


class AmbiguousPlaceOrder(unittest.TestCase):
    def test_a_timeout_after_the_order_reached_shoonya_returns_its_id(self):
        import shoonya
        tok = {"token": "t", "uid": "U", "actid": "U"}
        sent = {"n": 0}
        def http(endpoint, body, token):
            if endpoint == "PlaceOrder":
                sent["n"] += 1
                raise shoonya.NetworkError("PlaceOrder: network: timed out")
            if endpoint == "OrderBook":
                return [{"norenordno": "777", "tsym": "NIFTYBEES-EQ", "token": NIFTY.token, "trantype": "B",
                         "qty": "40", "remarks": "auto/NIFTYBEES", "status": "OPEN", "prctyp": "SL-LMT",
                         "norentm": AT.strftime("%H:%M:%S %d-%m-%Y")}]
            return {"stat": "Ok"}
        sess.now = lambda clock=None: AT              # other modules set their own clock
        b = shoonya.Shoonya(token=tok, http=http, sleep=lambda s: None)
        oid = b.place(NIFTY, side="B", qty=40, price=D("281.2"), trigger=D("281.05"), remarks="auto/NIFTYBEES")
        self.assertEqual(oid, "777")
        self.assertEqual(sent["n"], 1, "never sent twice")

    def test_a_timeout_that_never_arrived_is_an_error_not_a_crash(self):
        import shoonya
        def http(endpoint, body, token):
            if endpoint == "PlaceOrder":
                raise shoonya.NetworkError("x")
            return {"stat": "Not_Ok", "emsg": "no data"}
        b = shoonya.Shoonya(token={"token": "t", "uid": "U"}, http=http, sleep=lambda s: None)
        with self.assertRaises(shoonya.ShoonyaError):
            b.place(NIFTY, side="B", qty=40, price=D("281.2"), remarks="auto/NIFTYBEES")


class OneFlattenAtATime(unittest.TestCase):
    def test_a_second_flatten_while_one_runs_does_nothing(self):
        import fcntl
        reset()
        common.STATE_DIR.mkdir(parents=True, exist_ok=True)
        held = open(common.STATE_DIR / ".flatten.lock", "w")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            late = datetime(2026, 11, 11, 15, 6, tzinfo=sess.IST)
            b = FakeBroker(positions=[position(NIFTY, 40, "281")])
            flatten.run(b, now=late, sleep=lambda s: None)
            self.assertEqual(b.placed, [], "both would sell the same 40 and leave a short")
        finally:
            held.close()


class Notifications(unittest.TestCase):
    def test_the_same_alert_is_not_repeated_every_tick(self):
        reset(TRADER_NTFY_TOPIC="t-test")
        import urllib.request
        sent = []
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=8: sent.append(req) or type("R", (), {"read": lambda s: b""})()
        common._notified.clear()
        try:
            for _ in range(5):
                common.notify("Stop chased: LT", "x")
            import threading
            for t in threading.enumerate():
                if t is not threading.current_thread() and not t.daemon:
                    t.join(2)
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(len(sent), 1)


class ExitStartedRace(unittest.TestCase):
    """13 Nov 10:50: the fill monitor read the book between Claude's exit
    cancelling the stop and placing the buy, and put a stop back."""
    def setUp(self):
        reset()
        sess.now = lambda clock=None: AT
        plan("P1")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})

    def book(self):
        return FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", status="CANCELED", trigger="280.3")],
                          positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "281.10"}})

    def test_no_stop_is_put_back_while_an_exit_is_starting(self):
        common.log("flatten", {"event": "exit_started", "symbol": "NIFTYBEES", "token": NIFTY.token})
        b = self.book()
        fm.tick(b, AT, {})
        self.assertEqual(b.placed, [])

    def test_an_aborted_exit_is_protected_again(self):
        common.log("flatten", {"event": "exit_started", "symbol": "NIFTYBEES", "token": NIFTY.token})
        common.log("flatten", {"event": "exit_aborted", "symbol": "NIFTYBEES", "token": NIFTY.token})
        b = self.book()
        fm.tick(b, AT, {})
        self.assertEqual(len(b.placed), 1)

    def test_a_stale_start_does_not_block_protection(self):
        common.log("flatten", {"event": "exit_started", "symbol": "NIFTYBEES", "token": NIFTY.token})
        b = self.book()
        fm.tick(b, AT + timedelta(seconds=30), {})
        self.assertEqual(len(b.placed), 1)


class ProfitLock(unittest.TestCase):
    """13 Nov: +214 open at 12:21 became +17 net on the stops. Once a trade has
    shown profit after charges it is booked before it can give it back."""
    def setUp(self):
        reset()
        sess.now = lambda clock=None: AT
        plan("P1", target="290.00")                    # a far target: the lock, not the target, must act
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        self.state = {}

    def tick(self, ltp, state=None):
        b = FakeBroker(orders=[filled("P1"), order("S1", NIFTY, side="S", qty=40, price="278.90",
                                                   trigger="280.30", remarks="stop/NIFTYBEES")],
                       positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": ltp}})
        fm.tick(b, AT, self.state if state is None else state)
        return b

    def test_small_profit_does_not_arm(self):
        b = self.tick("281.60")                          # about +23 gross, +13 net
        self.assertEqual((b.placed, b.modified), ([], []))
        self.assertEqual(ev("profit_lock_armed"), [])

    def test_arming_moves_the_exchange_stop_to_breakeven_after_charges(self):
        b = self.tick("282.40")                          # about +55 gross, +45 net
        self.assertEqual(len(ev("profit_lock_armed")), 1)
        m = b.modified[-1]
        self.assertEqual(m["id"], "S1")
        self.assertGreater(m["trigger"], D("281.02"), "above entry: costs covered")
        self.assertLess(m["trigger"], D("281.40"))
        self.assertEqual(b.placed, [], "no exit while profit holds")

    def test_giving_back_half_books_the_profit_at_market(self):
        self.tick("282.40")
        self.tick("283.00")                              # best about +69 net
        b = self.tick("281.90")                          # about +25 net: under half the best
        self.assertIn("S1", b.cancelled)
        x = b.placed[-1]
        self.assertEqual((x["side"], x["qty"], x["trigger"], x["remarks"]), ("S", 40, None, "lock/NIFTYBEES"))
        self.assertIn("profit lock", ev("target_exit")[-1]["reason"])

    def test_the_lock_survives_a_fill_monitor_restart(self):
        self.tick("283.00")
        b = self.tick("281.90", state={})               # a fresh process: best comes from disk
        self.assertEqual(b.placed[-1]["remarks"], "lock/NIFTYBEES")

    def test_an_armed_trade_never_ends_below_five_rupees(self):
        import profitlock
        reset(TRADER_LOCK_KEEP_PCT="0")
        self.assertEqual(profitlock.floor(D("45")), D("5"))

    def test_the_lock_can_be_switched_off(self):
        reset(TRADER_LOCK="false")
        plan("P1", target="290.00")
        common.log("fill", {"event": "stop_placed", "parent": "P1", "order_id": "S1", "trigger": "280.30"})
        self.tick("283.00")
        b = self.tick("281.90")
        self.assertEqual(b.placed, [])
