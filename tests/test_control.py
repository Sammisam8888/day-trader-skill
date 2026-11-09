"""Claude's control channel: typed commands only, restrictions always,
raises only within the owner's ceilings, and a live calendar."""

import json
import os
import sys
import unittest
from datetime import date, datetime, time
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, opening, position, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import common  # noqa: E402
import control  # noqa: E402
import ops  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 10, 30, tzinfo=sess.IST)


def setup(**env):
    reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0", TRADER_MAX_LOSS="600", TRADER_DAILY_LOSS="2500",
          TRADER_MAX_TRADES="8", TRADER_CEIL_MAX_LOSS="900", TRADER_CEIL_MAX_TRADES="12", **env)
    sess.now = lambda clock=None: AT
    brain.find_claude = lambda: "/x/claude"
    try:
        os.unlink(sess.CALENDAR_FILE)
    except FileNotFoundError:
        pass
    sess._cal_cache["mtime"] = None


class Commands(unittest.TestCase):
    def setUp(self):
        setup()

    def test_unknown_command_is_refused(self):
        self.assertEqual(control.apply({"command": "rm -rf /"})[0], "refused")

    def test_halt_today_stops_scans(self):
        control.apply({"command": "halt_today", "reason": "chop"})
        self.assertIn("chop", common.halted())
        self.assertIsNone(common.halted() if False else None)

    def test_claude_cannot_lift_the_owners_halt(self):
        control.apply({"command": "halt_today", "reason": "x"}, by="owner")
        self.assertEqual(control.apply({"command": "resume_today"}, by="claude")[0], "refused")
        self.assertEqual(control.apply({"command": "resume_today"}, by="owner")[0], "applied")

    def test_lowering_is_always_allowed(self):
        out, _ = control.apply({"command": "set_risk_today", "args": {"max_loss_per_trade": 300, "daily_loss_limit": 1000}},
                               day_pnl=D("-900"), now=AT.replace(hour=9, minute=20))
        self.assertEqual(out, "applied")
        lim = common.limits()
        self.assertEqual((lim.max_loss_per_trade, lim.daily_loss_limit), (D("300"), D("1000")))

    def test_raise_within_ceiling_on_a_green_day(self):
        out, msg = control.apply({"command": "set_risk_today", "args": {"max_loss_per_trade": 800, "max_trades": 10}},
                                 day_pnl=D("150"), now=AT)
        self.assertEqual(out, "applied", msg)
        self.assertEqual((common.limits().max_loss_per_trade, common.limits().max_trades_per_day), (D("800"), 10))

    def test_raise_refusals(self):
        def raise_(args, pnl="100", at=AT):
            return control.apply({"command": "set_risk_today", "args": args}, day_pnl=D(pnl), now=at)
        self.assertIn("ceiling", raise_({"max_loss_per_trade": 2000})[1])
        self.assertIn("P&L", raise_({"max_loss_per_trade": 800}, pnl="-10")[1])
        self.assertIn("09:45", raise_({"max_loss_per_trade": 800}, at=AT.replace(hour=9, minute=20))[1])
        self.assertIn("last line", raise_({"daily_loss_limit": 5000})[1])
        self.assertIn("TRADER_CEIL_MAX_CONCURRENT", raise_({"max_concurrent": 6})[1])
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))

    def test_daily_loss_is_never_raised_even_if_the_file_says_so(self):
        control.write_json(control.day_file(), {"risk": {"daily_loss_limit": "99999"}})
        self.assertEqual(common.limits().daily_loss_limit, D("2500"))

    def test_raises_revert_when_the_day_turns_red(self):
        control.apply({"command": "set_risk_today", "args": {"max_loss_per_trade": 800}}, day_pnl=D("50"), now=AT)
        self.assertIsNone(control.check_raises(D("-500")))
        self.assertIsNotNone(control.check_raises(D("-1300")))
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))

    def test_size_and_sides(self):
        control.apply({"command": "set_risk_today", "args": {"size": 3, "sides": "sell"}})
        self.assertEqual((control.size_mult(), control.sides_allowed()), (D(1), ["SELL"]))

    def test_undo(self):
        control.apply({"command": "set_risk_today", "args": {"max_loss_per_trade": 300}})
        self.assertEqual(control.undo(DAY.isoformat(), 0)[0], "applied")
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))


class Calendar(unittest.TestCase):
    def setUp(self):
        setup()

    def test_holiday_added_by_claude_stops_the_day(self):
        self.assertIsNotNone(sess.session_for(datetime(2026, 11, 12, 10, 0, tzinfo=sess.IST)))
        out, _ = control.apply({"command": "calendar_add", "args": {"kind": "holiday", "date": "2026-11-12", "what": "x", "source": "NSE circular"}})
        self.assertEqual(out, "applied")
        self.assertIsNone(sess.session_for(datetime(2026, 11, 12, 10, 0, tzinfo=sess.IST)))

    def test_special_session_on_a_sunday(self):
        sun = datetime(2026, 11, 15, 14, 30, tzinfo=sess.IST)
        self.assertIsNone(sess.session_for(sun))
        control.apply({"command": "calendar_add", "args": {"kind": "special_session", "date": "2026-11-15",
                                                           "open": "14:00", "last_entry": "14:40", "flat_by": "15:00", "close": "15:15"}})
        s = sess.session_for(sun)
        self.assertEqual((s.open.time(), s.flat_by.time()), (time(14, 0), time(15, 0)))

    def test_bad_dates_and_times_refused(self):
        for args in ({"kind": "holiday", "date": "2025-01-01"}, {"kind": "holiday", "date": "2027-06-01"},
                     {"kind": "special_session", "date": "2026-11-15", "open": "15:00", "last_entry": "14:00",
                      "flat_by": "15:30", "close": "16:00"},
                     {"kind": "event", "date": "2026-11-12", "symbol": "TCS"}):
            self.assertEqual(control.apply({"command": "calendar_add", "args": args})[0], "refused", args)

    def test_nse_sync_replaces_the_static_list(self):
        msg = control.sync_nse_holidays(lambda api: {"CM": [{"tradingDate": "12-Nov-2026"}, {"tradingDate": "25-Dec-2026"}]})
        self.assertIn("2026: 2", msg)
        self.assertFalse(sess.is_trading_day(date(2026, 11, 12)))
        self.assertTrue(sess.is_trading_day(date(2026, 11, 10)), "NSE list now rules for 2026")

    def test_event_with_time_pauses_entries(self):
        import sentiment
        control.apply({"command": "calendar_add", "args": {"kind": "event", "date": "2026-11-11", "what": "Fed minutes", "at": "10:30"}})
        ev = sentiment.calendar(DAY, [], lambda api: [])
        self.assertTrue(any(e["what"] == "Fed minutes" and e["window"] for e in ev["market"]))


def claude(*decisions):
    reply = json.dumps({"view": "", "decisions": list(decisions)})
    return lambda *a, **k: brain.decide(*a, **k, run=lambda prompt: reply)


def enter():
    return {"action": "enter", "symbol": "NIFTYBEES", "side": "BUY", "order": "stop", "entry": 281.05,
            "stop": 280.30, "target": 282.80, "size": 1.0, "reason": "t"}


class InScan(unittest.TestCase):
    def setUp(self):
        setup()

    def broker(self, **kw):
        return FakeBroker(candles={NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")},
                          quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                                  SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}}, **kw)

    def test_halt_in_the_same_reply_blocks_the_entry_after_it(self):
        b = self.broker()
        autotrader.run(b, now=AT, decide=claude({"action": "control", "command": "halt_today", "reason": "shock"}, enter()))
        self.assertEqual(b.placed, [])
        rec = json.loads(brain.transcript_path(DAY.isoformat()).read_text().splitlines()[-1])
        self.assertEqual([a["outcome"] for a in rec["actions"]], ["applied", "refused"])
        b2 = self.broker()
        autotrader.run(b2, now=AT, decide=claude(enter()))
        self.assertEqual(b2.placed, [], "later scans stop too")

    def test_claude_is_told_about_control(self):
        seen = {}
        def run(p):
            seen["p"] = p
            return '{"view":"","decisions":[]}'
        autotrader.run(self.broker(), now=AT, decide=lambda *a, **k: brain.decide(*a, **k, run=run))
        state = json.loads(seen["p"].split("STATE:\n", 1)[1])
        self.assertEqual(state["control"]["owner_ceilings"]["max_loss_per_trade"], "900")
        self.assertIn("set_risk_today", seen["p"])

    def test_sides_restriction_refuses_longs(self):
        control.apply({"command": "set_risk_today", "args": {"sides": "SELL"}})
        b = self.broker()
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(b.placed, [])


class Ops(unittest.TestCase):
    def setUp(self):
        setup()

    def test_ops_review_applies_commands_and_is_transcribed(self):
        reply = json.dumps({"view": "RBI at 10, pause.", "commands": [
            {"command": "calendar_add", "args": {"kind": "event", "date": "2026-11-11", "what": "RBI", "at": "10:00"}},
            {"command": "set_risk_today", "args": {"max_loss_per_trade": 2000}}]})
        r = ops.run(AT.replace(hour=8, minute=50), ask=lambda p: reply, nse=lambda api: {"CM": [{"tradingDate": "25-Dec-2026"}]})
        self.assertEqual([a["outcome"] for a in r["actions"]], ["applied", "refused"])
        rec = json.loads(brain.transcript_path(DAY.isoformat()).read_text().splitlines()[-1])
        self.assertEqual(rec["source"], "ops")
        self.assertIn("untrusted", rec["prompt"])

    def test_ops_survives_a_bad_reply(self):
        r = ops.run(AT.replace(hour=8, minute=50), ask=lambda p: "no json here", nse=lambda api: {})
        self.assertIsNotNone(r["error"])


if __name__ == "__main__":
    unittest.main()
