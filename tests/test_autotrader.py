"""The scanner: Claude (or the rule) decides, this code validates, sizes
and places. Claude's decisions are scripted here; nothing calls a model."""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, opening, order, position, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import common  # noqa: E402
import session as sess  # noqa: E402
from shoonya import LoginRequired  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)


def broker(**kw):
    kw.setdefault("candles", {NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")})
    kw.setdefault("quotes", {NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                             SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})
    return FakeBroker(**kw)


def claude(*decisions, view="test"):
    """A decide() that runs the real pipeline with Claude's reply scripted."""
    reply = json.dumps({"view": view, "decisions": list(decisions)})
    return lambda *a, **k: brain.decide(*a, **k, run=lambda prompt: reply)


def ev(name):
    return common.events(name, DAY.isoformat())


def enter(sym="NIFTYBEES", side="BUY", order="stop", entry=281.05, stop=280.30, target=282.80, **kw):
    return {"action": "enter", "symbol": sym, "side": side, "order": order, "entry": entry,
            "stop": stop, "target": target, "size": 1.0, "reason": "test", **kw}


class ClaudeDecides(unittest.TestCase):
    def setUp(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_CONCURRENT="3")
        sess.now = lambda clock=None: AT
        brain.find_claude = lambda: "/x/claude"

    def test_a_long_stop_entry_is_placed_with_a_trigger(self):
        b = broker()
        autotrader.run(b, now=AT, decide=claude(enter()))
        p = b.placed[0]
        self.assertEqual((p["side"], p["trigger"], p["price"]), ("B", D("281.05"), D("281.07")))
        rec = ev("placed")[0]
        self.assertEqual((rec["side"], rec["decided_by"], rec["stop"]), ("B", "claude", "280.30"))

    def test_a_short_is_placed_below_the_market(self):
        b = broker()
        autotrader.run(b, now=AT, decide=claude(enter(side="SELL", entry=279.55, stop=280.30, target=278.00)))
        p = b.placed[0]
        self.assertEqual((p["side"], p["trigger"], p["price"]), ("S", D("279.55"), D("279.53")))

    def test_claude_does_not_choose_the_quantity(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LOSS="30", TRADER_MAX_LEVERAGE="2.0")
        b = broker()
        autotrader.run(b, now=AT, decide=claude(enter(qty=100000)))
        # max loss 30 / risk 0.77 per unit -> 38 units, whatever Claude asked
        self.assertEqual(b.placed[0]["qty"], 38)

    def test_size_scales_the_slot_down_only(self):
        b = broker()
        autotrader.run(b, now=AT, decide=claude(enter(size=0.25), enter("SENSEXIETF", entry=900.05, stop=897.0, target=906.0, size=5)))
        nifty, sensex = b.placed
        self.assertLess(nifty["qty"] * nifty["price"], sensex["qty"] * sensex["price"] / 2)

    def test_two_x_leverage_is_honoured_and_capped(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_SYMBOLS="NIFTYBEES", TRADER_MAX_LOSS="5000")
        b = broker(cash="10000")
        autotrader.run(b, now=AT, decide=claude(enter(stop=279.0, target=285.0)))
        value = b.placed[0]["qty"] * b.placed[0]["price"]
        self.assertGreater(value, D("10000"), "uses MIS leverage")
        self.assertLessEqual(value, D("20000"), "never past 2x")

    def refused(self, d, needle, **kw):
        b = broker(**kw)
        autotrader.run(b, now=AT, decide=claude(d))
        self.assertEqual(b.placed, [], needle)
        msgs = " ".join(str(r.get("reason") or r.get("reasons")) for r in ev("refused") + ev("blocked") + ev("unsized"))
        self.assertIn(needle, msgs)

    def test_refuses_a_chase(self):
        self.refused(enter(entry=280.40, stop=279.9, target=282), "would chase")

    def test_refuses_levels_in_the_wrong_order(self):
        self.refused(enter(stop=282.0, target=283.0), "long plan must be")

    def test_refuses_a_stop_inside_noise(self):
        self.refused(enter(entry=281.05, stop=280.90, target=281.60), "inside normal noise")

    def test_refuses_poor_reward_to_risk(self):
        self.refused(enter(entry=281.05, stop=280.0, target=281.80), "reward:risk")

    def test_refuses_a_symbol_outside_the_list(self):
        self.refused(enter(sym="ADANIENT"), "not in the trading list")

    def test_refuses_shorts_when_switched_off(self):
        reset(TRADER_DECIDER="claude", TRADER_ALLOW_SHORT="false")
        self.refused(enter(side="SELL", entry=279.55, stop=280.30, target=278.0), "shorts are switched off")

    def test_refuses_a_held_symbol(self):
        self.refused(enter(), "already holding", positions=[position(NIFTY, 40, "281")])

    def test_reentry_is_allowed_until_the_per_symbol_cap(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_ENTRIES_PER_SYMBOL="2")
        for i in ("A1", "A2"):
            common.log("auto", {"event": "placed", "order_id": i, "symbol": "NIFTYBEES", "side": "B", "risk": "50"})
        b = broker(orders=[order("A1", NIFTY, status="COMPLETE", filled=40), order("A2", NIFTY, status="COMPLETE", filled=40)])
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(b.placed, [])
        self.assertIn("2 time(s) today", ev("refused")[0]["reason"])

    def test_one_reentry_after_a_stop_is_fine(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_ENTRIES_PER_SYMBOL="2")
        common.log("auto", {"event": "placed", "order_id": "A1", "symbol": "NIFTYBEES", "side": "B", "risk": "50"})
        b = broker(orders=[order("A1", NIFTY, status="COMPLETE", filled=40)])
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(len(b.placed), 1)

    def test_cancel_only_its_own_entries(self):
        common.log("auto", {"event": "placed", "order_id": "MINE", "symbol": "NIFTYBEES"})
        b = broker(orders=[order("MINE", NIFTY), order("YOURS", SENSEX)])
        autotrader.run(b, now=AT, decide=claude({"action": "cancel", "order_id": "MINE"},
                                                {"action": "cancel", "order_id": "YOURS"}))
        self.assertEqual(b.cancelled, ["MINE"])

    def test_exit_closes_a_position(self):
        b = broker(positions=[position(NIFTY, -40, "281")])
        autotrader.run(b, now=AT, decide=claude({"action": "exit", "symbol": "NIFTYBEES", "reason": "reversal"}))
        self.assertEqual((b.placed[0]["side"], b.placed[0]["qty"]), ("B", 40))

    def test_unreadable_reply_means_no_trade_not_a_rule_trade(self):
        b = broker()
        autotrader.run(b, now=AT, decide=lambda *a, **k: brain.decide(*a, **k, run=lambda p: "I think buy"))
        self.assertEqual(ev("brain_view")[0]["source"], "none")
        self.assertEqual(b.placed, [])

    def test_rule_fallback_only_when_the_owner_asks(self):
        reset(TRADER_DECIDER="claude", TRADER_BRAIN_FALLBACK="rules", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_CONCURRENT="3")
        b = broker()
        autotrader.run(b, now=AT, decide=lambda *a, **k: brain.decide(*a, **k, run=lambda p: "I think buy"))
        self.assertEqual(ev("brain_view")[0]["source"], "rules")

    def test_the_prompt_carries_markets_account_and_limits(self):
        seen = {}
        b = broker(positions=[position(SENSEX, 5, "899")])
        autotrader.run(b, now=AT, decide=lambda *a, **k: brain.decide(
            *a, **k, run=lambda p: (seen.setdefault("p", p), '{"decisions": []}')[1]))
        state = json.loads(seen["p"].split("STATE:\n", 1)[1])
        self.assertEqual([m["symbol"] for m in state["markets"]], ["NIFTYBEES", "SENSEXIETF"])
        self.assertEqual(state["markets"][0]["opening_range"], {"high": "281.00", "low": "279.60"})
        self.assertEqual(state["positions"][0]["side"], "LONG")
        self.assertEqual(state["account"]["max_leverage"], "2.0")
        self.assertIn("reward:risk at least", seen["p"])


class RulesDecide(unittest.TestCase):
    def setUp(self):
        reset(TRADER_DECIDER="rules", TRADER_MAX_CONCURRENT="3")
        sess.now = lambda clock=None: AT

    def test_both_sides_of_the_opening_range_rest(self):
        b = broker()
        autotrader.run(b, now=AT)
        nifty = sorted((p["side"], p["trigger"]) for p in b.placed if p["symbol"] == "NIFTYBEES")
        self.assertEqual(nifty, [("B", D("281.01")), ("S", D("279.59"))])

    def test_shadow_mode_places_nothing(self):
        reset(TRADER_DECIDER="rules", TRADER_LIVE="false")
        b = broker()
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])
        self.assertGreater(len(ev("would_place")), 0)


class Guards(unittest.TestCase):
    def setUp(self):
        reset(TRADER_DECIDER="rules")
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

    def test_daily_loss_counts_open_losses(self):
        b = broker(positions=[position(SENSEX, 10, "900", unrealised="-1600")])
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])

    def test_leverage_ceiling(self):
        reset(TRADER_DECIDER="rules", TRADER_MAX_LEVERAGE="1.0")
        b = broker(cash="10000", positions=[position(SENSEX, 12, "900")])
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])

    def test_trade_cap_counts_fills(self):
        reset(TRADER_DECIDER="rules", TRADER_MAX_TRADES="1")
        common.log("auto", {"event": "placed", "order_id": "F1", "symbol": "SENSEXIETF"})
        b = broker(orders=[order("F1", SENSEX, status="COMPLETE", filled=10)])
        autotrader.run(b, now=AT)
        self.assertEqual(b.placed, [])

    def test_not_logged_in(self):
        class NoLogin(FakeBroker):
            def cash(self):
                raise LoginRequired("no session")
        self.assertEqual(autotrader.run(NoLogin(), now=AT), 0)
        self.assertEqual(len(ev("login_required")), 1)

    def test_live_is_off_unless_explicitly_on(self):
        for v in ("", "0", "no", "False"):
            reset(TRADER_LIVE=v)
            self.assertFalse(common.live(), v)


class Muhurat(unittest.TestCase):
    def test_sunday_special_session_trades(self):
        reset(TRADER_DECIDER="rules", TRADER_SYMBOLS="NIFTYBEES")
        at = datetime(2026, 11, 8, 14, 45, tzinfo=sess.IST)
        sess.now = lambda clock=None: at
        b = FakeBroker(candles={NIFTY.token: opening(date(2026, 11, 8), hh=14, mm=20)},
                       quotes={NIFTY.token: {"ltp": "280.5"}}, instruments=(NIFTY,))
        autotrader.run(b, now=at)
        self.assertEqual({p["symbol"] for p in b.placed}, {"NIFTYBEES"})


if __name__ == "__main__":
    unittest.main()


class Adjust(unittest.TestCase):
    """Claude moves the stop/target of an open trade; the code holds the line."""

    def setUp(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LOSS="600")
        sess.now = lambda clock=None: AT
        common.log("auto", {"event": "placed", "order_id": "E1", "symbol": "NIFTYBEES", "side": "B",
                            "token": NIFTY.token, "tick": "0.01", "entry": "281.03", "stop": "280.30",
                            "target": "282.80", "risk": "32"})

    def book(self, ltp="281.60"):
        return broker(positions=[position(NIFTY, 44, "281.02", ltp=ltp)],
                      orders=[order("E1", NIFTY, status="COMPLETE", filled=44, avg="281.02"),
                              order("S1", NIFTY, side="S", qty=44, trigger="280.30", price="278.90")],
                      quotes={NIFTY.token: {"ltp": ltp}, SENSEX.token: {"ltp": "898", "uc": "990", "lc": "810"}})

    def test_tightening_the_stop_modifies_the_exchange_order(self):
        b = self.book()
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "NIFTYBEES", "stop": 281.10, "reason": "lock"}))
        self.assertEqual(b.modified[0]["trigger"], D("281.10"))
        self.assertEqual(ev("plan_adjusted")[0]["stop"], "281.10")

    def test_a_stop_above_the_price_is_refused(self):
        b = self.book()
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "NIFTYBEES", "stop": 281.90}))
        self.assertEqual(b.modified, [])
        self.assertIn("losing side", ev("refused")[0]["reason"])

    def test_a_stop_that_risks_more_than_max_loss_is_refused(self):
        b = self.book()
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "NIFTYBEES", "stop": 260.00}))
        self.assertEqual(b.modified, [])
        self.assertIn("over max loss per trade", ev("refused")[0]["reason"])

    def test_target_extension_is_recorded_for_the_monitor(self):
        b = self.book()
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "NIFTYBEES", "target": 284.00}))
        self.assertEqual(ev("plan_adjusted")[0]["target"], "284.00")

    def test_claude_adopts_a_manual_position_with_a_stop_at_the_exchange(self):
        b = broker(positions=[position(SENSEX, 5, "899", ltp="900")])
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "SENSEXIETF", "stop": 895,
                                                 "target": 910, "reason": "owner's trade"}))
        p = b.placed[0]
        self.assertEqual((p["side"], p["qty"], p["trigger"]), ("S", 5, D("895.00")))
        st = ev("stop_placed")[0]
        self.assertEqual((st["parent"], st["adopted"]), (f"manual:{SENSEX.token}", True))
        self.assertEqual(ev("plan_adjusted")[0]["target"], "910.00")

    def test_an_adopted_manual_stop_is_then_moved(self):
        b = broker(positions=[position(SENSEX, 5, "899", ltp="903")],
                   orders=[order("MS", SENSEX, side="S", qty=5, trigger="895", price="890")])
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "SENSEXIETF", "stop": 897}))
        self.assertEqual(b.modified[0]["trigger"], D("897.00"))

    def test_a_manual_target_without_a_stop_is_refused(self):
        b = broker(positions=[position(SENSEX, 5, "899", ltp="900")])
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "SENSEXIETF", "target": 910}))
        self.assertEqual(b.placed, [])
        self.assertIn("needs a stop", ev("refused")[0]["reason"])

    def test_the_transcript_records_ask_reply_and_actions(self):
        b = self.book()
        autotrader.run(b, now=AT, decide=claude({"action": "adjust", "symbol": "NIFTYBEES", "stop": 281.10},
                                                 enter("SENSEXIETF", entry=900.05, stop=897.0, target=906.0), view="up day"))
        tr = brain.read_transcripts(DAY.isoformat())[-1]
        self.assertEqual(tr["source"], "claude")
        self.assertIn("STATE:", tr["prompt"])
        self.assertIn("up day", tr["reply"])
        self.assertEqual([a["outcome"] for a in tr["actions"]], ["adjusted", "placed"])
        self.assertTrue(tr["actions"][1]["order_id"])
        placed = ev("placed")[-1]
        self.assertLess(float(placed["net_if_stop"]), 0)
        self.assertGreater(float(placed["net_if_target"]), 0)


class AdjustedTargetView(unittest.TestCase):
    def test_a_stop_only_adjust_keeps_the_extended_target_in_claudes_view(self):
        reset(TRADER_DECIDER="claude")
        common.log("auto", {"event": "plan_adjusted", "parent": "P", "symbol": "X", "stop": "1", "target": "1082.00"})
        common.log("auto", {"event": "plan_adjusted", "parent": "P", "symbol": "X", "stop": "2", "target": None})
        adj = {}
        for r in common.events("plan_adjusted"):
            if r.get("target"):
                adj[str(r.get("parent"))] = r
        self.assertEqual(adj["P"]["target"], "1082.00")
        import inspect
        self.assertIn('if r.get("target"):', inspect.getsource(brain.build))
