"""Market direction: measured from candles, enforced on every entry."""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, candle, order, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import common  # noqa: E402
import regime  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
PREV = date(2026, 11, 10)
AT = datetime(2026, 11, 11, 10, 30, tzinfo=sess.IST)


def walk(start, step, n=15, day=DAY, prev_close=None):
    """Yesterday's flat session ending at prev_close, then n 5-minute bars stepping by `step`."""
    pc = prev_close if prev_close is not None else start
    out = [candle(PREV, 14, 0 + 5 * i, pc, pc + 0.05, pc - 0.05, pc) for i in range(10)]
    px = start
    for i in range(n):
        hh, mm = divmod(9 * 60 + 15 + 5 * i, 60)
        o, c = px, round(px + step, 2)
        out.append(candle(day, hh, mm, o, max(o, c) + 0.02, min(o, c) - 0.02, c))
        px = c
    return out


def bars15(cs):
    return brain._bars([c for c in cs if c.opens_at.date() == DAY], 15)


class Trend(unittest.TestCase):
    def test_a_rising_stock_scores_up(self):
        cs = walk(280, 0.3)
        t = regime.stock_trend(cs, DAY, bars15(cs))
        self.assertEqual(t["state"], regime.UP)
        self.assertGreaterEqual(t["score"], 0.8)

    def test_a_falling_stock_scores_down(self):
        cs = walk(280, -0.3)
        t = regime.stock_trend(cs, DAY, bars15(cs))
        self.assertEqual(t["state"], regime.DOWN)
        self.assertLessEqual(t["score"], -0.8)

    def test_too_early_is_forming(self):
        cs = walk(280, -0.3, n=2)
        self.assertEqual(regime.stock_trend(cs, DAY, bars15(cs))["state"], regime.FORMING)


class Market(unittest.TestCase):
    def down(self):
        return {"NIFTYBEES": {"score": -0.8, "state": "DOWN"}, "A": {"score": -0.6, "state": "DOWN"},
                "B": {"score": -0.4, "state": "DOWN"}, "C": {"score": 0.2, "state": "MIXED"}}

    def test_a_falling_market_allows_shorts_only(self):
        m = regime.market(self.down())
        self.assertEqual((m["state"], m["allowed"]), ("DOWN", ["SELL"]))

    def test_a_rising_market_allows_longs_only(self):
        up = {k: {"score": -v["score"], "state": "UP"} for k, v in self.down().items()}
        self.assertEqual(regime.market(up)["allowed"], ["BUY"])

    def test_mixed_follows_each_stock(self):
        trends = {"NIFTYBEES": {"score": 0.0, "state": "MIXED"}, "A": {"score": 0.8, "state": "UP"},
                  "B": {"score": -0.8, "state": "DOWN"}}
        d = {"market": regime.market(trends), "stocks": trends}
        self.assertEqual(d["market"]["state"], "MIXED")
        self.assertIsNone(regime.check("BUY", "A", d))
        self.assertIsNotNone(regime.check("SELL", "A", d))
        self.assertIsNone(regime.check("SELL", "B", d))
        self.assertIsNotNone(regime.check("BUY", "B", d))

    def test_no_direction_no_gate(self):
        self.assertIsNone(regime.check("BUY", "X", None))


def claude(*decisions):
    reply = json.dumps({"view": "t", "decisions": list(decisions)})
    return lambda *a, **k: brain.decide(*a, **k, run=lambda prompt: reply)


def enter(side, entry, stop, target, sym="NIFTYBEES"):
    return {"action": "enter", "symbol": sym, "side": side, "order": "stop", "entry": entry,
            "stop": stop, "target": target, "size": 1.0, "reason": "t"}


class Enforced(unittest.TestCase):
    def setUp(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_LOSS="600")
        sess.now = lambda clock=None: AT
        brain.find_claude = lambda: "/x/claude"

    def falling(self, **kw):
        return FakeBroker(candles={NIFTY.token: walk(285, -0.3, prev_close=285),
                                   SENSEX.token: walk(900, -0.9, prev_close=900)},
                          quotes={NIFTY.token: {"ltp": "280.50", "uc": "310", "lc": "255"},
                                  SENSEX.token: {"ltp": "886.50", "uc": "990", "lc": "810"}}, **kw)

    def test_a_long_into_a_falling_market_is_refused(self):
        b = self.falling()
        autotrader.run(b, now=AT, decide=claude(enter("BUY", 281.0, 280.2, 282.8)))
        self.assertEqual(b.placed, [])
        r = common.events("refused", DAY.isoformat())[-1]
        self.assertIn("only shorts", r["reason"])

    def test_a_short_into_a_falling_market_is_placed(self):
        b = self.falling()
        autotrader.run(b, now=AT, decide=claude(enter("SELL", 280.2, 281.0, 278.6)))
        self.assertEqual(b.placed[0]["side"], "S")

    def test_claude_is_told_the_direction(self):
        seen = {}
        def run(prompt):
            seen["p"] = prompt
            return json.dumps({"view": "", "decisions": []})
        autotrader.run(self.falling(), now=AT, decide=lambda *a, **k: brain.decide(*a, **k, run=run))
        state = json.loads(seen["p"].split("STATE:\n", 1)[1])
        self.assertEqual(state["direction"]["market"]["state"], "DOWN")
        self.assertIn("shorts only", seen["p"])

    def test_a_resting_buy_is_cancelled_when_the_market_turns_down(self):
        common.log("auto", {"event": "placed", "symbol": "NIFTYBEES", "side": "B", "order_id": "77",
                            "token": NIFTY.token, "risk": "50"})
        b = self.falling(orders=[order("77", NIFTY, "B", qty=50, price="281.10", trigger="281.05")])
        autotrader.run(b, now=AT, decide=claude())
        self.assertIn("77", b.cancelled)
        self.assertIn("direction changed", common.events("entry_cancelled", DAY.isoformat())[-1]["reason"])


if __name__ == "__main__":
    unittest.main()
