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


def F(**kw):
    """Facts as triggers.facts() would gather them; a quiet day by default."""
    f = {"now": AT, "day_pnl": D("0"), "realised": D("0"), "stops_hit": 0, "targets_hit": 0, "vix": {},
         "news": None, "bias": None, "events": [], "direction": [], "markets_failed": (0, 10)}
    f.update(kw)
    return f


def trend(state="UP", score=0.8, n=6):
    return [(f"2026-11-11T10:{i:02d}:00+05:30", state, score) for i in range(n)]


def cmd(name, trigger="", **args):
    return {"command": name, "trigger": trigger, "args": args}


class Triggers(unittest.TestCase):
    """Claude's word is not evidence: a command runs only on a trigger the data confirms."""
    def setUp(self):
        setup()

    def test_unknown_command_is_refused(self):
        self.assertEqual(control.apply({"command": "rm -rf /"}, facts=F())[0], "refused")

    def test_halt_without_a_trigger_is_refused(self):
        out, msg = control.apply(cmd("halt_today", reason="feels choppy"), facts=F())
        self.assertEqual(out, "refused")
        self.assertIn("needs one of these triggers", msg)
        self.assertIsNone(common.halted())

    def test_halt_with_a_trigger_the_data_denies_is_refused(self):
        out, msg = control.apply(cmd("halt_today", "drawdown", reason="x"), facts=F(day_pnl=D("-200")))
        self.assertEqual(out, "refused")
        self.assertIn("does not hold", msg)

    def test_halt_on_a_real_drawdown(self):
        out, msg = control.apply(cmd("halt_today", "drawdown", reason="down hard"), facts=F(day_pnl=D("-1300")))
        self.assertEqual(out, "applied", msg)
        self.assertIn("drawdown", common.halted())

    def test_stop_streak_and_vix_and_news(self):
        self.assertEqual(control.apply(cmd("halt_today", "stop_streak"), facts=F(stops_hit=3))[0], "applied")
        setup()
        self.assertEqual(control.apply(cmd("halt_today", "stop_streak"), facts=F(stops_hit=3, targets_hit=1))[0], "refused")
        self.assertEqual(control.apply(cmd("halt_today", "vix_spike"), facts=F(vix={"level": 18, "change_pct": 16}))[0], "applied")
        setup()
        self.assertEqual(control.apply(cmd("halt_today", "shock_news"), facts=F(news=-0.5))[0], "refused")

    def test_chop_not_before_eleven(self):
        flips = [(f"2026-11-11T10:{i:02d}:00+05:30", s, 0.5) for i, s in enumerate(["UP", "DOWN", "UP", "DOWN"])]
        self.assertEqual(control.apply(cmd("halt_today", "chop"), facts=F(direction=flips))[0], "refused")
        late = [(f"2026-11-11T11:{i:02d}:00+05:30", s, 0.5) for i, s in enumerate(["UP", "DOWN", "UP", "DOWN"])]
        self.assertEqual(control.apply(cmd("halt_today", "chop"), facts=F(now=AT.replace(hour=11, minute=30), direction=late))[0], "applied")

    def test_flatten_needs_the_stricter_threshold(self):
        self.assertEqual(control.apply(cmd("flatten_now", "drawdown"), facts=F(day_pnl=D("-1500")))[0], "refused")
        self.assertEqual(control.apply(cmd("flatten_now", "stop_streak"), facts=F(stops_hit=5))[0], "refused")
        self.assertEqual(control.apply(cmd("flatten_now", "drawdown"), facts=F(day_pnl=D("-1900")))[0], "applied")

    def test_claude_cannot_lift_the_owners_halt(self):
        control.apply(cmd("halt_today", reason="x"), by="owner")
        self.assertEqual(control.apply(cmd("resume_today", "condition_cleared"), facts=F(now=AT.replace(hour=12)))[0], "refused")
        self.assertEqual(control.apply(cmd("resume_today"), by="owner")[0], "applied")

    def test_resume_needs_the_condition_cleared(self):
        control.apply(cmd("halt_today", "vix_spike"), facts=F(vix={"level": 18, "change_pct": 16}))
        self.assertIn("30 min", control.apply(cmd("resume_today", "condition_cleared"), facts=F(now=AT.replace(minute=40)))[1])
        later = AT.replace(hour=11, minute=30)
        self.assertIn("still holds", control.apply(cmd("resume_today", "condition_cleared"),
                                                   facts=F(now=later, vix={"level": 18, "change_pct": 16}))[1])
        self.assertEqual(control.apply(cmd("resume_today", "condition_cleared"),
                                       facts=F(now=later, vix={"level": 16, "change_pct": 4}))[0], "applied")

    def test_lowering_needs_a_trigger_too(self):
        self.assertEqual(control.apply(cmd("set_risk_today", max_loss_per_trade=300), facts=F())[0], "refused")
        out, msg = control.apply(cmd("set_risk_today", "sentiment_against", size=0.5, sides="SELL"), facts=F(bias=-0.6))
        self.assertEqual(out, "applied", msg)
        self.assertEqual((control.size_mult(), control.sides_allowed()), (D("0.5"), ["SELL"]))
        out, _ = control.apply(cmd("set_risk_today", "drawdown", max_loss_per_trade=300, daily_loss_limit=1000),
                               facts=F(day_pnl=D("-1300")))
        self.assertEqual((out, common.limits().daily_loss_limit), ("applied", D("1000")))

    def test_raise_only_on_a_verified_trend_day(self):
        green = dict(day_pnl=D("400"), realised=D("350"), targets_hit=1, direction=trend())
        out, msg = control.apply(cmd("set_risk_today", "trend_day", max_loss_per_trade=800, max_trades=10), day_pnl=D("400"), now=AT, facts=F(**green))
        self.assertEqual(out, "applied", msg)
        self.assertEqual((common.limits().max_loss_per_trade, common.limits().max_trades_per_day), (D("800"), 10))

    def test_raise_refusals(self):
        green = dict(day_pnl=D("400"), realised=D("350"), targets_hit=1, direction=trend())
        def r(args, trigger="trend_day", pnl="400", at=AT, **f):
            facts = F(**{**green, **f, "now": at})
            return control.apply({"command": "set_risk_today", "trigger": trigger, "args": args}, day_pnl=D(pnl), now=at, facts=facts)[1]
        self.assertIn("trend_day", r({"max_loss_per_trade": 800}, trigger="drawdown"))
        self.assertIn("does not hold", r({"max_loss_per_trade": 800}, targets_hit=0))
        self.assertIn("does not hold", r({"max_loss_per_trade": 800}, direction=trend("UP", 0.4)))
        self.assertIn("does not hold", r({"max_loss_per_trade": 800}, direction=trend("UP", 0.8, 5) + trend("MIXED", 0.8, 1)))
        self.assertIn("ceiling", r({"max_loss_per_trade": 2000}))
        self.assertIn("P&L", r({"max_loss_per_trade": 800}, pnl="-10"))
        self.assertIn("09:45", r({"max_loss_per_trade": 800}, at=AT.replace(hour=9, minute=20)))
        self.assertIn("last line", r({"daily_loss_limit": 5000}))
        self.assertIn("separate", r({"max_loss_per_trade": 800, "max_trades": 2}))
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))

    def test_daily_loss_is_never_raised_even_if_the_file_says_so(self):
        control.write_json(control.day_file(), {"risk": {"daily_loss_limit": "99999"}})
        self.assertEqual(common.limits().daily_loss_limit, D("2500"))

    def test_raises_revert_when_the_day_turns_red(self):
        control.apply(cmd("set_risk_today", "trend_day", max_loss_per_trade=800), day_pnl=D("50"), now=AT,
                      facts=F(day_pnl=D("50"), realised=D("50"), targets_hit=1, direction=trend("DOWN", -0.7)))
        self.assertEqual(common.limits().max_loss_per_trade, D("800"))
        self.assertIsNone(control.check_raises(D("-500")))
        self.assertIsNotNone(control.check_raises(D("-1300")))
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))

    def test_undo(self):
        control.apply(cmd("set_risk_today", "drawdown", max_loss_per_trade=300), facts=F(day_pnl=D("-1300")))
        self.assertEqual(control.undo(DAY.isoformat(), 0)[0], "applied")
        self.assertEqual(common.limits().max_loss_per_trade, D("600"))

    def test_notify_needs_no_trigger(self):
        self.assertEqual(control.apply(cmd("notify", message="hi"), facts=F())[0], "applied")


class Calendar(unittest.TestCase):
    def setUp(self):
        setup()

    def add(self, **args):
        return control.apply({"command": "calendar_add", "args": args}, facts=F())

    def test_holiday_needs_an_official_source(self):
        self.assertIn("nseindia.com", self.add(kind="holiday", date="2026-11-12", what="x", source="a blog")[1])
        self.assertIsNotNone(sess.session_for(datetime(2026, 11, 12, 10, 0, tzinfo=sess.IST)))
        self.assertEqual(self.add(kind="holiday", date="2026-11-12", what="x",
                                  source="https://nsearchives.nseindia.com/content/circulars/X.pdf")[0], "applied")
        self.assertIsNone(sess.session_for(datetime(2026, 11, 12, 10, 0, tzinfo=sess.IST)))

    def test_special_session_on_a_sunday(self):
        sun = datetime(2026, 11, 15, 14, 30, tzinfo=sess.IST)
        self.assertIsNone(sess.session_for(sun))
        out, msg = self.add(kind="special_session", date="2026-11-15", open="14:00", last_entry="14:40",
                            flat_by="15:00", close="15:15", source="https://www.nseindia.com/circular")
        self.assertEqual(out, "applied", msg)
        s = sess.session_for(sun)
        self.assertEqual((s.open.time(), s.flat_by.time()), (time(14, 0), time(15, 0)))

    def test_bad_dates_and_times_refused(self):
        nse = "https://www.nseindia.com/x"
        for args in ({"kind": "holiday", "date": "2025-01-01", "source": nse}, {"kind": "holiday", "date": "2027-06-01", "source": nse},
                     {"kind": "special_session", "date": "2026-11-15", "open": "15:00", "last_entry": "14:00",
                      "flat_by": "15:30", "close": "16:00", "source": nse},
                     {"kind": "event", "date": "2026-11-12", "symbol": "TCS", "source": "x"},
                     {"kind": "event", "date": "2026-11-12", "what": "no source"}):
            self.assertEqual(self.add(**args)[0], "refused", args)

    def test_nse_sync_replaces_the_static_list(self):
        msg = control.sync_nse_holidays(lambda api: {"CM": [{"tradingDate": "12-Nov-2026"}, {"tradingDate": "25-Dec-2026"}]})
        self.assertIn("2026: 2", msg)
        self.assertFalse(sess.is_trading_day(date(2026, 11, 12)))
        self.assertTrue(sess.is_trading_day(date(2026, 11, 10)), "NSE list now rules for 2026")

    def test_event_with_time_pauses_entries(self):
        import sentiment
        self.add(kind="event", date="2026-11-11", what="Fed minutes", at="10:30", source="Reuters")
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
        control.apply({"command": "calendar_add", "args": {"kind": "event", "date": "2026-11-11", "what": "RBI", "at": "11:00", "source": "RBI"}}, facts=F())
        autotrader.run(b, now=AT, decide=claude({"action": "control", "command": "halt_today", "trigger": "event_risk", "reason": "RBI at 11"}, enter()))
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
        control.apply({"command": "set_risk_today", "trigger": "sentiment_against", "args": {"sides": "SELL"}}, facts=F(bias=-0.6))
        b = self.broker()
        autotrader.run(b, now=AT, decide=claude(enter()))
        self.assertEqual(b.placed, [])


class Ops(unittest.TestCase):
    def setUp(self):
        setup()

    def test_ops_review_applies_commands_and_is_transcribed(self):
        reply = json.dumps({"view": "RBI at 10, pause.", "commands": [
            {"command": "calendar_add", "args": {"kind": "event", "date": "2026-11-11", "what": "RBI", "at": "10:00", "source": "RBI release"}},
            {"command": "set_risk_today", "trigger": "trend_day", "args": {"max_loss_per_trade": 2000}}]})
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
