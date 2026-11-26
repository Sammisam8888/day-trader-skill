"""The AI agent: its own Rs 1 lakh wallet that follows the bench models the AI picks."""

import json
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import agent  # noqa: E402
import journal  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402

DAY = "2026-11-26"
T0 = datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST).timestamp()


def decide(t, **d):
    paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
    with open(paper.PAPER_DIR / f"agent-{DAY}.jsonl", "a") as fh:
        fh.write(json.dumps({"t": t, "at": datetime.fromtimestamp(t, sess.IST).isoformat(), "follow": {},
                             "exit": [], "exit_all": False, "stand_aside": False, "note": "", **d}) + "\n")


class Book(unittest.TestCase):
    def setUp(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_PAPER_RISK="500", TRADER_BENCH_TARGET="1000")
        paper._AGENT.update(mtime=None, day=None, rows=[])
        self.b = paper.Bench(DAY)

    def test_follows_only_the_chosen_models_sized_by_its_risk(self):
        decide(T0, follow={"vwap_trend@ai": {"risk": 2, "side": "both", "why": "trend day"}})
        self.b.agent_follow("rsi2", "AAA", "B", 100.0, 99.0, 103.0, T0 + 60)
        self.assertNotIn((paper.AGENT, "AAA"), self.b.open)
        self.b.agent_follow("vwap_trend@ai", "BBB", "B", 100.0, 99.0, 103.0, T0 + 60)
        t = self.b.open[(paper.AGENT, "BBB")]
        self.assertEqual(t.src, "vwap_trend@ai")
        self.assertEqual(t.qty, 990)                     # Rs 500 x2 over 1.01 a share (entry 100.01 after slip)
        self.assertIn("followed vwap_trend@ai", journal.log_file(paper.AGENT, DAY).read_text())

    def test_nothing_before_the_first_decision_and_the_side_filter_holds(self):
        decide(T0, follow={"orb15": {"risk": 1, "side": "long"}})
        self.b.agent_follow("orb15", "AAA", "B", 100.0, 99.0, 103.0, T0 - 60)
        self.b.agent_follow("orb15", "BBB", "S", 100.0, 101.0, 97.0, T0 + 60)
        self.assertEqual([k for k in self.b.open if k[0] == paper.AGENT], [])

    def test_an_exit_decision_closes_what_followed_that_model(self):
        decide(T0, follow={"orb15": {"risk": 1}, "rsi2": {"risk": 1}})
        self.b.agent_follow("orb15", "AAA", "B", 100.0, 99.0, 103.0, T0 + 60)
        self.b.agent_follow("rsi2", "BBB", "B", 100.0, 99.0, 103.0, T0 + 60)
        self.b.check_kills({"AAA": 100.5, "BBB": 100.5}, T0 + 70)
        decide(T0 + 900, follow={"rsi2": {"risk": 1}}, exit=["orb15"], note="orb is failing")
        self.b.check_kills({"AAA": 100.5, "BBB": 100.5}, T0 + 901)
        self.assertNotIn((paper.AGENT, "AAA"), self.b.open)
        self.assertIn((paper.AGENT, "BBB"), self.b.open)
        log = journal.log_file(paper.AGENT, DAY).read_text()
        self.assertIn("AI agent exit", log)
        self.assertIn("exit orb15. orb is failing", log)

    def test_it_is_a_book_with_its_own_wallet_in_the_summary(self):
        self.assertIn(paper.AGENT, paper.books())
        self.assertIn(paper.AGENT, self.b.summary({})["models"])


class Brain(unittest.TestCase):
    def setUp(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_PAPER_RISK="500", TRADER_BENCH_TARGET="1000")
        paper._AGENT.update(mtime=None, day=None, rows=[])

    def test_validate_keeps_real_models_clamps_risk_and_caps_the_count(self):
        reply = {"follow": {"nope": {"risk": 1}, "orb15": {"risk": 9, "side": "sideways"},
                            **{m: {"risk": 1} for m in list(paper.MODELS)[3:15]}},
                 "exit": ["orb5", "bogus"]}
        v = agent.validate(reply)
        self.assertNotIn("nope", v["follow"])
        self.assertEqual(v["follow"]["orb15"], {"risk": 2.0, "side": "both", "why": ""})
        self.assertLessEqual(len(v["follow"]), agent.MAX_FOLLOW)
        self.assertEqual(v["exit"], ["orb5"])

    def test_run_records_a_timestamped_decision_the_bench_reads(self):
        now = datetime(2026, 11, 26, 10, 5, tzinfo=sess.IST)
        sess.now = lambda clock=None: now
        out = agent.run(now, ask=lambda p: json.dumps({"day_read": "trend", "follow": {"liq_run": {"risk": 1.5}}}))
        self.assertEqual(out["follow"]["liq_run"]["risk"], 1.5)
        rows = paper.agent_decisions(DAY)
        self.assertEqual(rows[-1]["follow"], out["follow"])

    def test_a_bad_reply_changes_nothing(self):
        now = datetime(2026, 11, 26, 10, 5, tzinfo=sess.IST)
        self.assertIsNone(agent.run(now, ask=lambda p: "no json here"))
        self.assertEqual(paper.agent_decisions(DAY), [])


class Restart(unittest.TestCase):
    def test_a_bench_restart_keeps_the_fno_sleeve_logs(self):
        reset()
        journal.note("orb15~fut", DAY, "sleeve trade")
        journal.note("orb15", DAY, "stock trade")
        journal.reset_bench_day(DAY)
        self.assertTrue(journal.log_file("orb15~fut", DAY).exists())
        self.assertFalse(journal.log_file("orb15", DAY).exists())


if __name__ == "__main__":
    unittest.main()
