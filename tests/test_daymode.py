"""Day mode: lunch window, stop after losers, the paper mirror and the flip."""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, candle, opening, order, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import common  # noqa: E402
import daymode  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 11, 0, tzinfo=sess.IST)


def placed(oid, inst, side="B", entry="281.05", stop="280.30", setup="orb"):
    common.log("auto", {"event": "placed", "order_id": oid, "symbol": inst.symbol, "side": side, "token": inst.token,
                        "entry": entry, "stop": stop, "target": "283", "risk": "30", "setup": setup})


def stopped(oid, sid, inst, side, fill, exit_):
    common.log("fill", {"event": "stop_placed", "parent": oid, "order_id": sid, "symbol": inst.symbol, "side": side})
    return [order(oid, inst, side=("B" if side == "S" else "S"), status="COMPLETE", qty=10, filled=10, avg=fill),
            order(sid, inst, side=side, status="COMPLETE", qty=10, filled=10, avg=exit_)]


class Pieces(unittest.TestCase):
    def setUp(self):
        reset(TRADER_NO_ENTRY_WINDOWS="12:00-13:00")
        sess.now = lambda clock=None: AT

    def test_window(self):
        self.assertIsNone(daymode.no_entry_window(AT))
        self.assertIn("12:00-13:00", daymode.no_entry_window(AT.replace(hour=12, minute=10)))

    def test_losers_count_only_stops_worse_than_entry(self):
        placed("E1", NIFTY); placed("E2", SENSEX)
        orders = stopped("E1", "S1", NIFTY, "S", "281", "280.2") + stopped("E2", "S2", SENSEX, "S", "900", "901")
        self.assertEqual(daymode.losers(orders, common.events("placed")), {"normal": 1, "flip": 0})

    def test_mirror_opens_opposite_and_is_valued_on_candles(self):
        reset(TRADER_TARGET_R="4")
        placed("E1", NIFTY, entry="281.05", stop="280.30")      # R = 0.75
        orders = [order("E1", NIFTY, status="COMPLETE", qty=10, filled=10, avg="281.05")]
        daymode.ensure_mirrors(orders, common.events("placed"), AT)
        m = common.events("mirror_open")[0]
        self.assertEqual((m["side"], m["stop"], m["target"]), ("S", "281.80", "278.05"))
        daymode.ensure_mirrors(orders, common.events("placed"), AT)
        self.assertEqual(len(common.events("mirror_open")), 1, "one mirror per fill")
        cs = [candle(DAY, 10, 50, 281, 282.5, 280.9, 281.0),          # before the fill: ignored (would stop it)
              candle(DAY, 11, 0, 281, 281.1, 279.0, 279.2), candle(DAY, 11, 5, 279.2, 279.4, 277.9, 278.0)]
        v = daymode.mirror_value({"NIFTYBEES": (NIFTY, cs, {"ltp": D("278")})}, DAY)
        self.assertGreater(v["closed"], 0, "the mirror short hit its target as price fell")


def claude(*decisions):
    reply = json.dumps({"view": "", "decisions": list(decisions)})
    return lambda *a, **k: brain.decide(*a, **k, run=lambda p: reply)


def enter(sym="NIFTYBEES", side="BUY", setup="orb", **kw):
    return {"action": "enter", "symbol": sym, "side": side, "order": "stop", "entry": 281.05, "stop": 280.30,
            "target": 284.10, "size": 1.0, "reason": "t", "setup": setup, **kw}


class Flow(unittest.TestCase):
    def setUp(self):
        sess.now = lambda clock=None: AT
        brain.find_claude = lambda: "/x/claude"

    def broker(self, orders=(), ltp="280.50"):
        return FakeBroker(orders=list(orders), cash="25000",
                          candles={NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")},
                          quotes={NIFTY.token: {"ltp": ltp, "uc": "308", "lc": "252"},
                                  SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})

    def losing_day(self, n=3):
        orders = []
        for i in range(n):
            placed(f"E{i}", SENSEX, entry="900", stop="899")
            orders += stopped(f"E{i}", f"S{i}", SENSEX, "S", "900", "899")
        return orders

    def test_three_losers_stop_new_entries(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_STOP_AFTER_LOSSES="3", TRADER_MAX_TRADES="20")
        b = self.broker(self.losing_day())
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(b.placed, [])
        self.assertIn("3 losing trades", common.events("refused", DAY.isoformat())[-1]["reason"])

    def test_flip_when_the_mirror_is_winning_and_only_flips_allowed(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_STOP_AFTER_LOSSES="3", TRADER_FLIP="true",
              TRADER_MAX_TRADES="20", TRADER_TARGET_R="4")
        orders = self.losing_day()
        for i in range(3):     # the mirrors (short at 900, stop 901) are well in profit: price fell to 890
            common.log("daymode", {"event": "mirror_open", "parent": f"E{i}", "symbol": "SENSEXIETF", "side": "S", "qty": 50,
                                   "entry": "900", "stop": "901", "target": "896", "opened": AT.isoformat()})
        b = FakeBroker(orders=orders, cash="25000",
                       candles={NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")},
                       quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                               SENSEX.token: {"ltp": "890.00", "uc": "990", "lc": "810"}})
        seen = {}
        def run(p):
            seen["p"] = p
            return json.dumps({"view": "", "decisions": [enter(setup="orb")]})
        autotrader.run(b, now=AT, decide=lambda *a, **k: brain.decide(*a, **k, run=run))
        self.assertTrue(common.events("flip_on", DAY.isoformat()))
        state = json.loads(seen["p"].split("STATE:\n", 1)[1])
        self.assertTrue(state["day_mode"]["flipped"])
        self.assertTrue(all(s["setup"].endswith("_flip") for m in state["markets"] for s in m.get("setups", [])))
        self.assertEqual(b.placed, [], "a non-flip entry is refused in flip mode")
        self.assertIn("flip mode", common.events("refused", DAY.isoformat())[-1]["reason"])

    def test_lunch_window_refuses_entries(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_NO_ENTRY_WINDOWS="10:30-11:30")
        b = self.broker()
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(b.placed, [])

    def test_direction_gate_enforced_without_touching_sentiment(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_DIRECTION_GATE="enforce")
        self.assertTrue(daymode.direction_enforced())
        self.assertFalse(autotrader.gates_enforced())


if __name__ == "__main__":
    unittest.main()
