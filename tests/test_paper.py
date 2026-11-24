"""The paper bench: never touches the broker; opens, closes, charges, kills."""

import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import paper  # noqa: E402
import session as sess  # noqa: E402

T0 = datetime(2026, 11, 18, 9, 15, tzinfo=sess.IST).timestamp()


def feed_path(bench, sym, prices, start=T0, step=60, vol=1000):
    cum = 0
    for i, px in enumerate(prices):
        cum += vol
        bench.feed(sym, start + i * step + 1, px, cum)


class NoBroker(unittest.TestCase):
    def test_the_bench_cannot_reach_the_broker(self):
        src = open(paper.__file__).read()
        for bad in ("import shoonya", "from shoonya", "broker.place", ".place(", "autotrader"):
            self.assertNotIn(bad, src)


class Bars(unittest.TestCase):
    def test_minutes_and_volume(self):
        s = paper.Series()
        self.assertFalse(s.add(T0 + 1, 100, 10))
        s.add(T0 + 20, 101, 30)
        self.assertTrue(s.add(T0 + 61, 99, 50))
        b = s.closed()[0]
        self.assertEqual((b.o, b.h, b.l, b.c, b.v), (100, 101, 100, 101, 20))


class Trading(unittest.TestCase):
    def setUp(self):
        reset()

    def test_orb_breakout_opens_then_hits_target_after_charges(self):
        b = paper.Bench("2026-11-18", write=False)
        rng = [100, 100.2, 99.9, 100.1, 100.0] * 3                 # 09:15-09:30 range 99.9-100.2
        up = [100.1, 100.3, 100.5, 100.8, 101.0, 101.2]
        feed_path(b, "AAA", rng + up)
        opened = [k for k in b.open if k[1] == "AAA"]
        self.assertTrue(any(k[0] in ("orb5", "orb15") for k in opened) or b.trades)
        feed_path(b, "AAA", [101.5, 102.0, 102.5], start=T0 + 21 * 60)
        self.assertGreater(sum(b.trades.values()), 0)
        self.assertTrue(all(isinstance(v, D) for v in b.realised.values()))

    def test_everything_is_flat_at_14_50(self):
        b = paper.Bench("2026-11-18", write=False)
        b.open[("orb5", "AAA")] = paper.Trade("orb5", "AAA", "B", 100, 100.0, 90.0, 120.0, T0)
        t = datetime(2026, 11, 18, 14, 50, 5, tzinfo=sess.IST).timestamp()
        b.on_price("AAA", 101.0, t)
        self.assertEqual(b.open, {})
        self.assertEqual(b.trades["orb5"], 1)

    def test_uncapped_paper_only_records_where_the_kill_would_fire(self):
        b = paper.Bench("2026-11-18", write=False)
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 500, 100.0, 90.0, 120.0, T0)
        b.check_kills({"AAA": 99.8}, T0 + 3600)
        self.assertNotIn("rsi2", b.killed)
        self.assertIn(("rsi2", "AAA"), b.open)
        self.assertIn("rsi2", b.would_kill)

    def test_model_kill_closes_and_stops_that_model_only(self):
        reset(TRADER_PAPER_UNCAPPED="false")
        b = paper.Bench("2026-11-18", write=False)
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 500, 100.0, 90.0, 120.0, T0)
        b.open[("orb5", "BBB")] = paper.Trade("orb5", "BBB", "B", 500, 100.0, 90.0, 120.0, T0)
        b.check_kills({"AAA": 99.8, "BBB": 100.0}, T0 + 3600)
        self.assertIn("rsi2", b.killed)
        self.assertNotIn("orb5", b.killed)
        self.assertIn(("orb5", "BBB"), b.open)

    def test_no_entries_after_14_35(self):
        b = paper.Bench("2026-11-18", write=False)
        late = datetime(2026, 11, 18, 14, 36, tzinfo=sess.IST).timestamp()
        feed_path(b, "AAA", [100 + i * 0.5 for i in range(10)], start=late)
        self.assertEqual(b.open, {})

    def test_kill_holds_during_replay_and_sizes_cap_the_loss(self):
        reset(TRADER_PAPER_UNCAPPED="false")
        b = paper.Bench("2026-11-18", write=False)
        b.open[("rsi2", "AAA")] = paper.Trade("rsi2", "AAA", "B", 500, 100.0, 90.0, 120.0, T0)
        b.feed("AAA", T0 + 3000, 99.5, None)
        self.assertIn("rsi2", b.killed)
        self.assertGreaterEqual(b.realised["rsi2"], D("-400"))

    def test_charges_make_a_flat_trade_a_loss(self):
        self.assertLess(paper.round_trip_net("B", 100.0, 100.0, 500), 0)

    def test_summary_shape(self):
        b = paper.Bench("2026-11-18", write=False)
        s = b.summary({})
        self.assertEqual(set(s["models"]), set(paper.MODELS))
        self.assertEqual(D(s["total"]), 0)


class Inverted(unittest.TestCase):
    def test_inverted_swaps_side_stop_and_target(self):
        f = paper.inverted(lambda bars, st: ("B", 99.0, 103.0))
        self.assertEqual(f([], {}), ("S", 103.0, 99.0))
        self.assertIn("orb5_inv", paper.MODELS)


class Api(unittest.TestCase):
    def setUp(self):
        reset()

    def test_paper_endpoint_pairs_opens_and_closes(self):
        import json
        import dashboard
        paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
        (paper.PAPER_DIR / "2026-11-18.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"ev": "open", "t": 1, "model": "orb5", "symbol": "A", "side": "B", "qty": 5, "entry": 10, "stop": 9, "target": 12},
            {"ev": "close", "t": 2, "model": "orb5", "symbol": "A", "side": "B", "qty": 5, "entry": 10, "exit": 12, "net": "8", "why": "target"},
            {"ev": "open", "t": 3, "model": "rsi2", "symbol": "B", "side": "S", "qty": 5, "entry": 10, "stop": 11, "target": 9}]))
        r = dashboard.api_paper("2026-11-18")
        self.assertEqual([t["ev"] for t in r["trades"]], ["open", "close"])
        self.assertEqual(r["trades"][1]["stop"], 9)


class ForTheAI(unittest.TestCase):
    def setUp(self):
        reset(TRADER_TAPE_EXTRA="TCS, ITC,RELIANCE")

    def test_the_ai_sees_breakouts_vs_fades(self):
        import json
        import brain
        paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime(2026, 11, 18, 11, 0, tzinfo=sess.IST)
        m = {"orb15": {"net": "-200", "trades": 3, "wins": 0}, "orb15_inv": {"net": "90", "trades": 3, "wins": 3},
             "rsi2": {"net": "0", "trades": 0, "wins": 0}}
        (paper.PAPER_DIR / "2026-11-18-summary.json").write_text(json.dumps({"models": m}))
        v = brain._paper_bench(now)
        self.assertEqual(v["best"][0]["model"], "orb15_inv")
        self.assertEqual((v["breakout_models_net"], v["fade_models_net"]), (-200, 90))
        self.assertEqual(brain._paper_bench(datetime(2026, 11, 19, 11, 0, tzinfo=sess.IST)), {})

    def test_tape_records_extra_symbols_once(self):
        import tape
        syms = tape.symbols()
        self.assertIn("TCS", syms)
        self.assertEqual(syms.count("RELIANCE"), 1)


class Desk(unittest.TestCase):
    def setUp(self):
        reset(TRADER_SYMBOLS="NIFTYBEES,HDFCBANK", TRADER_INTERNALS="false")

    def test_desk_combines_the_tape_sectors_and_the_bench(self):
        import json
        import time
        import dashboard
        import tape
        tape.TAPE_DIR.mkdir(parents=True, exist_ok=True)
        tape.LIVE_FILE.write_text(json.dumps({"symbols": {
            "HDFCBANK": {"ltp": 808.1, "chg_5m": -0.2, "momentum": -0.3, "imbalance": 0.4, "updated": time.time()},
            "TCS": {"ltp": 4100.0, "chg_5m": 0.5, "updated": time.time()}}}))
        r = dashboard.api_terminal("2026-11-24")
        rows = {m["symbol"]: m for m in r["monitor"]}
        self.assertEqual((rows["HDFCBANK"]["sector"], rows["HDFCBANK"]["traded"]), ("Banks", True))
        self.assertEqual((rows["TCS"]["sector"], rows["TCS"]["traded"]), ("IT", False))
        self.assertEqual(r["bench"], [])


if __name__ == "__main__":
    unittest.main()


class Knobs(unittest.TestCase):
    def setUp(self):
        reset()

    def test_clamp_keeps_knobs_inside_tested_ranges(self):
        p = paper.clamp("orb", {"minutes": 90, "target_x": "x", "invert": 1, "bogus": 5, "stop_frac": float("nan")})
        self.assertEqual(p["minutes"], 30)
        self.assertEqual(p["target_x"], 2.0)
        self.assertEqual(p["invert"], 1)
        self.assertNotIn("bogus", p)
        self.assertEqual(p["stop_frac"], 0.5)

    def test_side_and_enabled_knobs(self):
        sig = lambda bars, st, p: ("B", 99.0, 103.0)
        paper.RECIPES["_t"] = sig
        paper.KNOBS["_t"] = {}
        try:
            self.assertIsNone(paper.apply("_t", [], {}, {"enabled": 0}))
            self.assertIsNone(paper.apply("_t", [], {}, {"enabled": 1, "side": 2}))
            self.assertEqual(paper.apply("_t", [], {}, {"enabled": 1, "invert": 1}), ("S", 103.0, 99.0))
        finally:
            del paper.RECIPES["_t"], paper.KNOBS["_t"]

    def test_ai_copy_reads_the_tuner_file(self):
        import json
        import session as s2
        s2.now = lambda clock=None: datetime(2026, 11, 19, 10, 0, tzinfo=sess.IST)
        paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
        (paper.PAPER_DIR / "params-2026-11-19.json").write_text(json.dumps({"models": {"orb": {"minutes": 5, "invert": 1}}}))
        p = paper.ai_params("orb")
        self.assertEqual((p["minutes"], p["invert"]), (5, 1))
        self.assertEqual(paper.ai_params("rsi2"), paper.defaults("rsi2"))
        self.assertIn("orb@ai", paper.MODELS)


class Tuner(unittest.TestCase):
    def setUp(self):
        reset()

    def test_tuner_writes_clamped_settings_and_survives_garbage(self):
        import json
        import tuner
        now = datetime(2026, 11, 19, 9, 22, tzinfo=sess.IST)
        reply = json.dumps({"day_read": "choppy", "models": {"orb": {"minutes": 99, "invert": 1, "why": "breakouts failing"},
                                                             "nope": {"x": 1}, "rsi2": {"enabled": 0}}})
        out = tuner.run(now, ask=lambda p: reply)
        self.assertEqual(out["models"]["orb"]["minutes"], 30)
        self.assertEqual(out["models"]["rsi2"]["enabled"], 0)
        self.assertNotIn("nope", out["models"])
        self.assertEqual(tuner.current("2026-11-19")["day_read"], "choppy")
        again = tuner.run(now, ask=lambda p: "not json at all")
        self.assertEqual(again["models"]["orb"]["minutes"], 30, "a bad reply keeps the previous settings")
        later = tuner.run(now, ask=lambda p: json.dumps({"models": {"rsi2": {"enabled": 1}}}))
        self.assertEqual(later["models"]["orb"]["invert"], 1, "untouched recipes keep their settings")

    def test_prompt_lists_every_recipe_and_range(self):
        import tuner
        t = tuner.knob_table()
        for r in paper.RECIPES:
            self.assertIn(r, t)
        self.assertIn("minutes 5..30", t)
