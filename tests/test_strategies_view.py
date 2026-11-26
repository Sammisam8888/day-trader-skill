"""The Strategies tab's data: leaderboard, one strategy's day, pipeline health."""

import json
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import common  # noqa: E402
import journal  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402
import strategies_view as sv  # noqa: E402

DAY = "2026-11-26"
NOW = datetime(2026, 11, 26, 11, 0, tzinfo=sess.IST)


def seed():
    paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
    for d, net in (("2026-11-25", "-120"), (DAY, "1012.40")):
        (paper.PAPER_DIR / f"{d}-summary.json").write_text(json.dumps({"at": NOW.timestamp(), "models": {
            "liq_sweep": {"net": net, "trades": 3, "wins": 2, "open_trades": 0, "target_hit": 1.0 if net == "1012.40" else None}}}))
    t = NOW.timestamp()
    (paper.PAPER_DIR / f"{DAY}.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ev": "open", "t": t - 600, "model": "liq_sweep", "symbol": "LT", "side": "B", "qty": 50, "entry": 3900, "stop": 3890, "target": 3920},
        {"ev": "close", "t": t - 300, "model": "liq_sweep", "symbol": "LT", "side": "B", "qty": 50, "entry": 3900, "exit": 3920, "net": "960.5", "why": "target"},
        {"ev": "open", "t": t - 60, "model": "liq_sweep", "symbol": "SBIN", "side": "S", "qty": 100, "entry": 800, "stop": 803, "target": 794},
        {"ev": "open", "t": t - 60, "model": "orb15", "symbol": "SBIN", "side": "S", "qty": 100, "entry": 800, "stop": 803, "target": 794}]))
    journal.row("liq_sweep", DAY, time="10:50:00", event="open", stock="LT", side="BUY", qty=50, price=3900, stop=3890, target=3920)
    journal.row("liq_sweep", DAY, time="10:55:00", event="close", stock="LT", side="BUY", qty=50, price=3920, net="960.5", note="target")
    journal.learn_file("liq_sweep", DAY).parent.mkdir(parents=True, exist_ok=True)
    journal.learn_file("liq_sweep", DAY).write_text("# Sweeps of the opening lows paid\n\nDate: 2026-11-26\n")


class View(unittest.TestCase):
    def setUp(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_BENCH_TARGET="1000", TRADER_PAPER_CAPITAL="100000", TRADER_DAY_TARGET="1000")
        sess.now = lambda clock=None: NOW
        seed()

    def test_overview_lists_every_model_and_the_ai_trader(self):
        ov = sv.overview(DAY)
        names = [r["name"] for r in ov["strategies"]]
        import fno
        self.assertEqual(set(names), set(paper.MODELS) | {journal.AI} | set(fno.models_with_sleeves()))
        self.assertEqual(len(names), 36 + 70, "35 models + the AI trader + a futures and an options sleeve per model")
        top = ov["strategies"][0]
        self.assertEqual((top["name"], top["today"]["net"], top["target_days"], top["days"]), ("liq_sweep", 1012.4, 1, 2))
        self.assertEqual(top["learning"]["text"], "Sweeps of the opening lows paid")
        self.assertEqual(sv.kind_of("orb@ai"), "ai_tuned")
        self.assertEqual(sv.kind_of("orb15_inv"), "inverted")

    def test_detail_has_trades_log_learning_and_knob_ranges(self):
        d = sv.detail("liq_sweep", DAY)
        self.assertEqual([t["stock"] for t in d["trades"]], ["LT", "SBIN"])
        self.assertEqual(d["trades"][0]["net"], 960.5)
        self.assertIsNone(d["trades"][1]["closed"], "the SBIN trade is still open")
        self.assertEqual([r["event"] for r in d["log_rows"]], ["open", "close"])
        self.assertIn("Sweeps", d["learning_md"])
        self.assertEqual(d["knobs"]["ranges"]["touches"], [2, 5])
        self.assertEqual(d["days"], ["2026-11-25", DAY])

    def test_pipeline_flags_a_dead_tape_in_market_hours(self):
        p = sv.pipeline(NOW)
        st = {s["key"]: s["status"] for s in p["stages"]}
        self.assertEqual(st["tape"], "down")
        self.assertEqual(st["bench"], "ok")
        self.assertIn(p["broker"], ("paper", "live"))

    def test_the_endpoint_refuses_odd_names(self):
        self.assertFalse(all(c.isalnum() or c in "_@-" for c in "../../etc"))


if __name__ == "__main__":
    unittest.main()
