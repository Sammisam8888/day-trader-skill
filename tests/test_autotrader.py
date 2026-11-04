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
