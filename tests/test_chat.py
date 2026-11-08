"""Chat control: grammar, guards, and that every action goes through the
same broker calls as the buttons."""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, order, position, reset  # noqa: E402

import chat  # noqa: E402
import common  # noqa: E402
import session as sess  # noqa: E402

sess.now = lambda clock=None: datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)
chat._systemctl = lambda *a: None
chat._sleep = lambda s: None


class Grammar(unittest.TestCase):
    def setUp(self):
        reset()

    def test_stop_and_continue(self):
        self.assertIn("Stopped", chat.handle_command("stop everything", FakeBroker()))
        self.assertTrue(common.KILL_SWITCH.exists())
        self.assertIn("Running", chat.handle_command("continue", FakeBroker()))
        self.assertFalse(common.KILL_SWITCH.exists())

    def test_stop_never_touches_the_broker(self):
        b = FakeBroker()
        chat.handle_command("stop", b)
        self.assertEqual((b.placed, b.cancelled), ([], []))

    def test_scan_interval(self):
        self.assertIn("every 3 minutes", chat.handle_command("scan every 3 minutes", FakeBroker()))
        self.assertIn("OnCalendar=*-*-* 09..15:00/3:00", chat.TIMER_DROPIN.read_text())
        self.assertIn("between 2 and 60", chat.act_frequency(1))

    def test_limits(self):
        self.assertIn("TRADER_MAX_CONCURRENT=3", chat.handle_command("max positions 3", FakeBroker()))
        self.assertIn("refused", chat.handle_command("leverage 9", FakeBroker()))
        self.assertIn("refused", chat.handle_command("cost gate 0.5", FakeBroker()))

    def test_symbols(self):
        r = chat.handle_command("symbols niftybees sensexietf", FakeBroker())
        self.assertIn("NIFTYBEES,SENSEXIETF", r)
        self.assertEqual(common.symbols(), ["NIFTYBEES", "SENSEXIETF"])

    def test_free_text_is_not_a_command(self):
        self.assertIsNone(chat.handle_command("what do you think about today", FakeBroker()))


class Orders(unittest.TestCase):
    def setUp(self):
        reset()

    def test_buy_is_capped_at_half_of_cash(self):
        b = FakeBroker(cash="25000", quotes={NIFTY.token: {"ltp": "280"}})
        self.assertIn("over the chat buy cap", chat.handle_command("buy 100 niftybees", b))
        self.assertEqual(b.placed, [])

    def test_buy_inside_the_cap_is_a_marketable_limit(self):
        b = FakeBroker(cash="25000", quotes={NIFTY.token: {"ltp": "280"}})
        r = chat.handle_command("buy 40 NIFTYBEES", b)
        self.assertIn("Buying NIFTYBEES x40", r)
        p = b.placed[0]
        self.assertEqual((p["side"], p["qty"], p["trigger"]), ("B", 40, None))
        self.assertGreater(p["price"], 280)

    def test_unknown_symbol(self):
        self.assertIn("Unknown symbol", chat.handle_command("buy 1 NOPE", FakeBroker()))

    def test_sell_clears_the_stop_first(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")],
                       orders=[order("S1", NIFTY, side="S", qty=40, trigger="280.3", price="279")])
        r = chat.handle_command("sell niftybees", b)
        self.assertIn("Selling NIFTYBEES x40", r)
        self.assertEqual(b.cancelled, ["S1"])
        self.assertEqual(b.placed[0]["side"], "S")

    def test_cancel_all_keeps_stops(self):
        b = FakeBroker(orders=[order("P1", NIFTY), order("S1", SENSEX, side="S", trigger="899", price="894")])
        chat.handle_command("cancel all", b)
        self.assertEqual(b.cancelled, ["P1"])

    def test_status(self):
        r = chat.handle_command("status", FakeBroker(positions=[position(NIFTY, 40, "281")]))
        self.assertIn("cash 25000", r)
        self.assertIn("NIFTYBEES x40", r)


class Model(unittest.TestCase):
    def setUp(self):
        reset()

    def test_free_text_goes_to_the_cli_and_its_actions_run(self):
        chat.find_claude = lambda: "/x/claude"
        chat._run_cli = lambda prompt: '{"reply": "Stopping.", "actions": [{"name": "stop", "args": {}}]}'
        r = chat.handle("please stop for now", FakeBroker())
        self.assertIn("Stopping.", r)
        self.assertTrue(common.KILL_SWITCH.exists())

    def test_reply_in_a_code_fence_parses(self):
        self.assertEqual(chat._parse_json_reply('```json\n{"reply": "hi", "actions": []}\n```')["reply"], "hi")


if __name__ == "__main__":
    unittest.main()
