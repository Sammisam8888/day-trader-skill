import os
import unittest
from pathlib import Path

from _load import ROOT, load

_a = load("indstocks", "autotrader")


class TestFailsClosed(unittest.TestCase):
    def test_live_is_off_unless_explicitly_switched_on(self):
        for value in ("", "0", "false", "no", "maybe", "TRUE_ISH"):
            os.environ["TRADER_LIVE"] = value
            mod = load("indstocks", "autotrader")
            self.assertFalse(mod.LIVE, f"{value!r} must not enable live")
        os.environ.pop("TRADER_LIVE", None)

    def test_only_explicit_affirmatives_enable_live(self):
        for value in ("1", "true", "yes", "TRUE", " Yes "):
            os.environ["TRADER_LIVE"] = value
            mod = load("indstocks", "autotrader")
            self.assertTrue(mod.LIVE, f"{value!r} should enable live")
        os.environ.pop("TRADER_LIVE", None)

    def test_kill_switch_is_checked_before_any_order(self):
        with open(os.path.join(ROOT, "indstocks", "autotrader.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        halt_at = text.index("reason = halted()")
        post_at = text.index('client.post("/smart/order"')
        self.assertLess(halt_at, post_at,
                        "the halt check must precede any order call")
        # And again inside the loop, so a halt mid-run stops the rest.
        self.assertIn("if halted():", text)

    def test_daily_loss_limit_uses_broker_pnl_not_our_own_log(self):
        with open(os.path.join(ROOT, "indstocks", "autotrader.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("realised_pnl", text)
        self.assertIn("daily loss limit reached", text)

    def test_a_failed_order_is_not_retried(self):
        with open(os.path.join(ROOT, "indstocks", "autotrader.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("never retried", text)
        self.assertNotIn("for attempt in", text)


class TestCapitalIsSpentAcrossTheRun(unittest.TestCase):
    """Three orders must not each claim the whole account."""

    def source(self, name):
        with open(os.path.join(ROOT, "indstocks", name), encoding="utf-8") as fh:
            return fh.read()

    def test_autotrader_sizes_against_remaining_not_total(self):
        text = self.source("autotrader.py")
        self.assertIn("size_position(plan, remaining_cash", text)
        self.assertNotIn("size_position(plan, cash", text)

    def test_shadow_sizes_against_remaining_too(self):
        text = self.source("shadow.py")
        self.assertIn("size_position(plan, remaining_cash", text)
        self.assertNotIn("size_position(plan, cash", text)

    def test_both_decrement_after_committing(self):
        for name in ("autotrader.py", "shadow.py"):
            self.assertIn("remaining_cash -= sizing.position_value",
                          self.source(name), name)

    def test_the_arithmetic_that_was_wrong(self):
        # 2026-11-04: three tickets at 22,341 + 22,447 + 21,753 against a
        # 25,000 balance, each sized as though it were the only one.
        from decimal import Decimal as D
        _r = load("indstocks", "risk")
        _st = load("indstocks", "strategy")

        def plan(entry, stop, target):
            e, s, t_ = D(entry), D(stop), D(target)
            return _st.ExitPlan(e, s, t_, e - s, (t_ - e) / (e - s))

        cash = D("25000")
        limits = _r.Limits()
        total = D(0)
        for args in (("1314.2", "1304.9", "1332.8"),
                     ("2494.1", "2478.4", "2525.5"),
                     ("1144.9", "1137.8", "1159.1")):
            total += _r.size_position(plan(*args), cash, limits).position_value
        self.assertGreater(total, cash * 2, "the bug: 2.6x the account")

        remaining = cash
        spent = D(0)
        for args in (("1314.2", "1304.9", "1332.8"),
                     ("2494.1", "2478.4", "2525.5"),
                     ("1144.9", "1137.8", "1159.1")):
            s = _r.size_position(plan(*args), remaining, limits)
            spent += s.position_value
            remaining -= s.position_value
        self.assertLessEqual(spent, cash, "the fix: never over-commit")


class TestCrossRunMemory(unittest.TestCase):
    """Each timer firing is a new process; the day's limits are per DAY."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self._old = _a.STATE_DIR
        _a.STATE_DIR = Path(self.tmp.name)

    def tearDown(self):
        _a.STATE_DIR = self._old
        self.tmp.cleanup()

    def test_empty_log_means_nothing_placed(self):
        s = _a.todays_placements()
        self.assertEqual(s["trades_taken"], 0)
        self.assertEqual(s["open_symbols"], ())

    def test_counts_placements_from_an_earlier_run(self):
        import json
        from decimal import Decimal as D
        import session as sess
        path = _a.STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"event": "run_start"},
            {"event": "placed", "symbol": "RELIANCE", "risk": "158.10"},
            {"event": "blocked", "symbol": "WIPRO"},
            {"event": "run_end"},
            {"event": "placed", "symbol": "ITC", "risk": "51.30"},
        ]) + "\n", encoding="utf-8")

        s = _a.todays_placements()
        self.assertEqual(s["trades_taken"], 2)
        self.assertEqual(set(s["open_symbols"]), {"RELIANCE", "ITC"})
        self.assertEqual(s["risk_committed"], D("209.40"))

    def test_only_placed_events_count(self):
        import json
        import session as sess
        path = _a.STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"event": "would_place", "symbol": "RELIANCE", "risk": "158.10"},
            {"event": "blocked", "symbol": "ITC", "risk": "51.30"},
            {"event": "order_failed", "symbol": "SBIN", "risk": "10"},
        ]) + "\n", encoding="utf-8")
        self.assertEqual(_a.todays_placements()["trades_taken"], 0,
                         "a shadow decision is not a placement")

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        import json
        import session as sess
        path = _a.STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps({"event": "placed", "symbol": "ITC", "risk": "51.30"})
            + "\n", encoding="utf-8")
        self.assertEqual(_a.todays_placements()["trades_taken"], 1)

    def test_runner_reads_its_own_log_not_the_ticket_log(self):
        with open(os.path.join(ROOT, "indstocks", "autotrader.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("day_log = todays_placements()", text)
        self.assertNotIn("day_log = tk_mod.day_state_from_log()", text)

    def test_a_symbol_placed_today_is_skipped(self):
        with open(os.path.join(ROOT, "indstocks", "autotrader.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("already placed today", text)


class TestOperatorControls(unittest.TestCase):
    def test_go_live_script_offers_halt_and_resume(self):
        text = Path(ROOT, "scripts", "go-live.sh").read_text(encoding="utf-8")
        for verb in ("on)", "off)", "halt)", "resume)", "status)"):
            self.assertIn(verb, text)

    def test_halt_warns_that_resting_orders_survive(self):
        text = Path(ROOT, "scripts", "go-live.sh").read_text(encoding="utf-8")
        self.assertIn("NOT cancelled", text,
                      "halting stops new orders; it cannot recall resting ones")


if __name__ == "__main__":
    unittest.main()
