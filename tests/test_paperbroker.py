"""The paper broker: fills on the tape, stops, gaps, margin, FIFO P&L, and no real order ever."""

import json
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import common  # noqa: E402
import paperbroker as pbm  # noqa: E402
import session as sess  # noqa: E402
import shoonya  # noqa: E402
from shoonya import Instrument  # noqa: E402

AT = datetime(2026, 11, 20, 10, 0, tzinfo=sess.IST)
INST = Instrument("HDFCBANK", "HDFCBANK-EQ", "1333", "NSE", D("0.05"), 1)


class Data:
    """Read-only market data stand-in."""
    def __init__(self):
        self.q = {"ltp": D("810.00"), "bid": D("809.95"), "ask": D("810.05"), "uc": D("890"), "lc": D("730"), "tick": D("0.05")}

    def quote(self, token, exch="NSE"):
        return dict(self.q)


class Clock:
    def __init__(self):
        self.t = AT

    def __call__(self, *a):
        return self.t


def tape(day, sym, rows):
    d = common.STATE_DIR / "tape" / day
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{sym}.jsonl", "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class PaperBrokerTest(unittest.TestCase):
    def setUp(self):
        reset()
        self.clock = Clock()
        sess.now = self.clock
        self.data = Data()
        self.b = pbm.PaperBroker(data=self.data, now=self.clock)
        self.day = "2026-11-20"
        tape(self.day, "HDFCBANK", [{"t": AT.timestamp() - 5, "lp": 810.0, "bp1": 809.95, "sp1": 810.05}])

    def ticks(self, *rows):
        tape(self.day, "HDFCBANK", [{"t": AT.timestamp() + i + 1, "lp": lp, "bp1": bid, "sp1": ask}
                                    for i, (lp, bid, ask) in enumerate(rows)])

    def test_a_marketable_limit_fills_at_the_touch(self):
        oid = self.b.place(INST, side="B", qty=10, price=D("812.00"), remarks="auto/HDFCBANK")
        o = next(x for x in self.b.orders() if x["id"] == oid)
        self.assertEqual((o["status"], o["filled"], o["avg"]), ("COMPLETE", 10, D("810.05")))
        p = self.b.positions()[0]
        self.assertEqual((p["qty"], p["avg"]), (10, D("810.05")))

    def test_a_stop_entry_triggers_on_the_tape_between_polls(self):
        self.data.q.update(ltp=D("811"), bid=D("810.95"), ask=D("811.05"))   # above the trigger
        oid = self.b.place(INST, side="S", qty=65, price=D("809.95"), trigger=D("810.45"))
        self.ticks((811.0, 810.95, 811.05))
        self.assertEqual(self.b.orders()[0]["status"], "TRIGGER_PENDING")
        self.ticks((810.40, 810.35, 810.45))                                    # the sell stop triggers
        o = next(x for x in self.b.orders() if x["id"] == oid)
        self.assertEqual((o["status"], o["avg"]), ("COMPLETE", D("810.35")))

    def test_a_gap_past_the_limit_leaves_the_stop_resting_then_it_fills(self):
        self.b.place(INST, side="B", qty=10, price=D("812.00"))                # long 10 at 810.05
        sid = self.b.place(INST, side="S", qty=10, price=D("804.00"), trigger=D("805.00"))
        self.ticks((803.0, 802.90, 803.10))                                    # gapped through 804
        o = next(x for x in self.b.orders() if x["id"] == sid)
        self.assertEqual(o["status"], "OPEN", "triggered but its limit 804 is above the bid")
        self.ticks((804.2, 804.10, 804.30))
        o = next(x for x in self.b.orders() if x["id"] == sid)
        self.assertEqual((o["status"], o["avg"]), ("COMPLETE", D("804.10")))
        p = self.b.positions()[0]
        self.assertEqual((p["qty"], p["realised"]), (0, D("-59.50")))

    def test_modify_and_cancel(self):
        self.b.place(INST, side="B", qty=10, price=D("812.00"))
        sid = self.b.place(INST, side="S", qty=10, price=D("804.00"), trigger=D("805.00"))
        self.b.modify({"id": sid}, INST, qty=10, price=D("807.00"), trigger=D("808.00"))
        self.assertEqual(next(x for x in self.b.orders() if x["id"] == sid)["trigger"], D("808.00"))
        self.b.cancel(sid)
        self.assertEqual(next(x for x in self.b.orders() if x["id"] == sid)["status"], "CANCELED")
        with self.assertRaises(shoonya.ShoonyaError):
            self.b.cancel(sid)

    def test_margin_beyond_capital_is_rejected_but_exits_are_not(self):
        reset(TRADER_PAPER_CAPITAL="20000")
        sess.now = self.clock
        oid = self.b.place(INST, side="B", qty=200, price=D("812.00"))         # 162k x 20% = 32k > 20k
        self.assertEqual(next(x for x in self.b.orders() if x["id"] == oid)["status"], "REJECTED")
        self.b.place(INST, side="B", qty=100, price=D("812.00"))               # 16k margin: fits
        xid = self.b.place(INST, side="S", qty=100, price=D("809.00"))         # closing: always allowed
        self.assertEqual(next(x for x in self.b.orders() if x["id"] == xid)["status"], "COMPLETE")

    def test_fifo_realised_on_a_partial_close_and_reverse(self):
        b = pbm.PaperBroker._book([
            {"token": "1", "symbol": "X", "tsym": "X-EQ", "side": "B", "qty": 10, "price": "100"},
            {"token": "1", "symbol": "X", "tsym": "X-EQ", "side": "B", "qty": 10, "price": "102"},
            {"token": "1", "symbol": "X", "tsym": "X-EQ", "side": "S", "qty": 15, "price": "105"},
            {"token": "1", "symbol": "X", "tsym": "X-EQ", "side": "S", "qty": 10, "price": "104"}])["1"]
        self.assertEqual(b["realised"], D(50 + 15 + 10))       # 10x5 + 5x3 + 5x2
        self.assertEqual(b["lots"], [(-5, D("104"))])


class NoRealOrders(unittest.TestCase):
    def setUp(self):
        reset(TRADER_BROKER="paper")

    def test_shoonya_is_the_paper_broker_and_real_order_calls_refuse(self):
        self.assertIsInstance(shoonya.Shoonya(), pbm.PaperBroker)
        live = shoonya.Shoonya(data_only=True, token={"token": "t", "uid": "U"}, http=lambda *a: {})
        self.assertNotIsInstance(live, pbm.PaperBroker)
        for call in (lambda: live.place(INST, side="B", qty=1, price=D("1")),
                     lambda: live.modify({"id": "1"}, INST, qty=1, price=D("1")),
                     lambda: live.cancel("1")):
            with self.assertRaises(shoonya.ShoonyaError):
                call()

    def test_live_mode_is_unchanged(self):
        reset()
        self.assertNotIsInstance(shoonya.Shoonya(token={"token": "t", "uid": "U"}), pbm.PaperBroker)


if __name__ == "__main__":
    unittest.main()


class WholePipeline(unittest.TestCase):
    """The real autotrader, fill monitor and flatten, on paper fills from the tape."""

    def test_enter_protect_trail_and_flatten_on_paper(self):
        from datetime import date, timedelta
        import autotrader
        import brain
        import fillmonitor as fm
        import flatten
        from _fake import NIFTY, SENSEX, FakeBroker, opening
        day = date(2026, 11, 11)
        at = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)
        reset(TRADER_DECIDER="ai", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_CONCURRENT="3",
              TRADER_LOCK_ARM_R="0.5", TRADER_LOCK_ARM_RS="20", TRADER_LOCK_STEPS="0.5:40,1:60,2:75,3:85")
        clock = Clock()
        clock.t = at
        sess.now = clock
        brain.find_cli = lambda: "/x/ai"
        data = FakeBroker(candles={NIFTY.token: opening(day), SENSEX.token: opening(day, "900.00", "896.00")},
                          quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                                  SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
        pb = pbm.PaperBroker(data=data, now=clock)
        reply = json.dumps({"view": "t", "decisions": [{"action": "enter", "symbol": "NIFTYBEES", "side": "BUY",
                                                        "order": "stop", "entry": 281.05, "stop": 280.30,
                                                        "target": 284.00, "size": 1.0, "reason": "t"}]})
        autotrader.run(pb, now=at, decide=lambda *a, **k: brain.decide(*a, **k, run=lambda p: reply))
        entry = [o for o in pb.orders() if o["remarks"].startswith("auto/")]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["status"], "TRIGGER_PENDING")

        def move(px, secs):
            px = float(px)
            clock.t = clock.t + timedelta(seconds=secs)
            data._quotes[NIFTY.token] = {"ltp": str(px), "bid": str(round(px - 0.01, 2)), "ask": str(round(px + 0.01, 2)),
                                        "uc": "308", "lc": "252"}
            tape("2026-11-11", "NIFTYBEES", [{"t": clock.t.timestamp(), "lp": px, "bp1": px - 0.01, "sp1": px + 0.01}])
            fm.tick(pb, clock.t, state)

        state = {}
        move(281.10, 30)                                   # the stop entry triggers and fills
        self.assertEqual(pb.positions()[0]["qty"], entry[0]["qty"])
        move(281.15, 5)                                    # the fill monitor places the protective stop
        stops = [o for o in pb.orders() if o["pending"] and o["side"] == "S"]
        self.assertEqual(len(stops), 1)
        first = stops[0]["trigger"]
        for px in (281.6, 282.0, 282.4):                   # the trade runs: the stop trails up
            move(px, 5)
        st = [o for o in pb.orders() if o["pending"] and o["side"] == "S"][0]
        self.assertGreater(st["trigger"], first)
        self.assertGreater(st["trigger"], D("281.10"), "profit locked at the exchange (on paper)")
        move(st["trigger"] - D("0.02"), 5)                # it turns: the trailing stop books it
        move(float(st["trigger"]) - 0.03, 5)
        p = pb.positions()[0]
        self.assertEqual(p["qty"], 0)
        self.assertGreater(p["realised"], 0, "booked in profit")
        clock.t = datetime(2026, 11, 11, 14, 50, tzinfo=sess.IST)
        flatten.run(pb, now=clock.t, sleep=lambda s: None)
        self.assertFalse([o for o in pb.orders() if o["pending"]])


class Pip(unittest.TestCase):
    def setUp(self):
        reset()

    def test_streak_counts_consecutive_days_of_400_plus_after_charges(self):
        import pip
        rec = {"2026-11-20": "450", "2026-11-23": "-10", "2026-11-24": "401", "2026-11-25": "700", "2026-11-26": "400"}
        self.assertEqual(pip.streak(rec), 3)
        rec["2026-11-27"] = "399.99"
        self.assertEqual(pip.streak(rec), 0, "under Rs 400 resets the streak")

    def test_record_takes_the_days_last_net_and_a_tradeless_day_is_zero(self):
        import pip
        sess.now = lambda clock=None: AT
        common.log("fill", {"event": "pnl", "net": "120.00"})
        common.log("fill", {"event": "pnl", "net": "455.50"})
        r = pip.record("2026-11-20")
        self.assertEqual((r["net"], r["streak"], r["passed"]), ("455.50", 1, False))
        r2 = pip.record("2026-11-23")
        self.assertEqual((r2["net"], r2["streak"]), ("0.00", 0))
