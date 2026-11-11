"""Past the normal trade limit, extra trades are paid for out of today's profit."""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, opening, order, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import profit_budget as budget  # noqa: E402
import common  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 11, 0, tzinfo=sess.IST)


def closed(inst, qty, buy, sell):
    return {"symbol": inst.symbol, "token": inst.token, "qty": 0, "avg": D("0"), "ltp": D(sell), "product": "I",
            "realised": (D(sell) - D(buy)) * qty, "unrealised": D("0"),
            "buy_qty": qty, "sell_qty": qty, "buy_avg": D(buy), "sell_avg": D(sell)}


class Room(unittest.TestCase):
    def setUp(self):
        reset(TRADER_PROFIT_GIVEBACK_PCT="50")

    def test_no_profit_no_room(self):
        r = budget.room([closed(NIFTY, 100, "280", "279")], [], [], D("600"))
        self.assertEqual(r["room"], D("0"))

    def test_half_of_realised_net_is_room(self):
        r = budget.room([closed(NIFTY, 100, "280", "290")], [], [], D("600"))
        net = r["realised_net"]
        self.assertGreater(net, D("950"))
        self.assertEqual(r["room"], (net / 2).quantize(D("0.01")))

    def test_open_risk_and_resting_risk_use_up_room(self):
        pos = [closed(NIFTY, 100, "280", "290"),
               {"symbol": "SENSEXIETF", "token": SENSEX.token, "qty": 10, "avg": D("900"), "ltp": D("901"), "product": "I",
                "realised": D("0"), "unrealised": D("10"), "buy_qty": 10, "sell_qty": 0, "buy_avg": D("900"), "sell_avg": D("0")}]
        stop = order("S1", SENSEX, "S", trigger="890", price="889")
        stop["trigger"] = D("890")
        resting = order("R1", NIFTY, "B")
        r0 = budget.room(pos[:1], [], [], D("600"))["room"]
        r = budget.room(pos, [stop, resting], [{"order_id": "R1", "risk": "50", "costs": "10"}], D("600"))
        self.assertEqual(r0 - r["room"], D("160"))     # 100 at the open stop + 60 resting


def claude(*decisions):
    reply = json.dumps({"view": "", "decisions": list(decisions)})
    return lambda *a, **k: brain.decide(*a, **k, run=lambda p: reply)


def enter():
    return {"action": "enter", "symbol": "NIFTYBEES", "side": "BUY", "order": "stop", "entry": 281.05,
            "stop": 280.30, "target": 282.80, "size": 1.0, "reason": "t"}


class PastTheLimit(unittest.TestCase):
    def setUp(self):
        sess.now = lambda clock=None: AT
        brain.find_claude = lambda: "/x/claude"

    def run_with(self, positions, **env):
        reset(**{"TRADER_DECIDER": "claude", "TRADER_MAX_TRADES": "1", "TRADER_MAX_TRADES_HARD": "5",
                 "TRADER_MAX_LEVERAGE": "2.0", "TRADER_SYMBOLS": "NIFTYBEES,SENSEXIETF", **env})
        common.log("auto", {"event": "placed", "symbol": "SENSEXIETF", "side": "B", "order_id": "F1",
                            "token": SENSEX.token, "risk": "40"})
        b = FakeBroker(positions=positions,
                       orders=[order("F1", SENSEX, "B", status="COMPLETE", qty=10, filled=10, avg="900")],
                       candles={NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")},
                       quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                               SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
        autotrader.run(b, now=AT, decide=claude(enter()))
        return b

    def test_flat_day_stops_at_the_limit(self):
        b = self.run_with([closed(SENSEX, 10, "900", "899")])
        self.assertEqual(b.placed, [])
        self.assertEqual(common.events("halted", DAY.isoformat())[-1]["reason"], "trade limit reached")

    def test_profitable_day_funds_another_trade(self):
        b = self.run_with([closed(SENSEX, 100, "900", "910")])
        self.assertEqual(len(b.placed), 1)
        self.assertTrue(common.events("profit_funded", DAY.isoformat()))

    def test_small_profit_cannot_fund_a_big_risk(self):
        b = self.run_with([closed(SENSEX, 10, "900", "904")])
        self.assertEqual(b.placed, [])

    def test_hard_cap_holds(self):
        b = self.run_with([closed(SENSEX, 100, "900", "910")], TRADER_MAX_TRADES_HARD="1")
        self.assertEqual(b.placed, [])


if __name__ == "__main__":
    unittest.main()
