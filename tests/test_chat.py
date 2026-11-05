"""Chat control: the grammar must be unambiguous, every action must go
through the same guards as the buttons, and a buy must be capped."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import load  # noqa: E402

TMP = tempfile.mkdtemp()
os.environ["TRADER_STATE_DIR"] = TMP
os.environ["TRADER_ENV_FILE"] = os.path.join(TMP, "env")
os.environ["TRADER_KILL_SWITCH"] = os.path.join(TMP, "halt")
os.environ["TRADER_TIMER_DROPIN"] = os.path.join(TMP, "auto.timer.d", "override.conf")
_c = load("indstocks", "chat")
Path(os.environ["TRADER_ENV_FILE"]).write_text("TRADER_LIVE=true\n")


class Broker:
    def __init__(self, cash="25000", ltp="1310", positions=(), book=()):
        self.cash, self.ltp = cash, ltp
        self.positions, self.book, self.posts = list(positions), list(book), []

    def get(self, path, params=None):
        if path == "/funds":
            return {"data": {"detailed_avl_balance": {"eq_mis": self.cash},
                             "realized_pnl": "0", "unrealized_pnl": "0"}}
        if path == "/portfolio/positions":
            return {"data": self.positions}
        if path == "/order-book":
            return {"data": self.book}
        if path == "/market/quotes/ltp":
            return {"data": {c: {"live_price": self.ltp} for c in params["scrip-codes"].split(",")}}
        raise AssertionError(path)

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"data": {"order_id": "O1"}}


class TestStopAndGo(unittest.TestCase):
    def tearDown(self):
        try:
            os.unlink(os.environ["TRADER_KILL_SWITCH"])
        except FileNotFoundError:
            pass

    def test_stop_writes_the_kill_switch(self):
        r = _c.handle_command("stop", Broker())
        self.assertIn("Stopped", r)
        self.assertTrue(os.path.exists(os.environ["TRADER_KILL_SWITCH"]))

    def test_stop_everything_also_stops(self):
        _c.handle_command("STOP everything now", Broker())
        self.assertTrue(os.path.exists(os.environ["TRADER_KILL_SWITCH"]))

    def test_continue_removes_it(self):
        _c.handle_command("stop", Broker())
        self.assertIn("Running", _c.handle_command("continue", Broker()))
        self.assertFalse(os.path.exists(os.environ["TRADER_KILL_SWITCH"]))

    def test_stop_does_not_touch_the_broker(self):
        b = Broker()
        _c.handle_command("stop", b)
        self.assertEqual(b.posts, [])


class TestFrequency(unittest.TestCase):
    def test_every_n_minutes_rewrites_the_timer(self):
        _c._systemctl = lambda *a: None
        r = _c.handle_command("scan every 5 minutes", Broker())
        self.assertIn("every 5 minutes", r)
        spec = Path(os.environ["TRADER_TIMER_DROPIN"]).read_text()
        self.assertIn("OnCalendar=Mon-Fri *-*-* 09..14:00/5:00 Asia/Kolkata", spec)
        self.assertIn("OnCalendar=\n", spec, "the empty line clears the unit's own value")

    def test_bounds(self):
        self.assertIn("between 2 and 60", _c.act_frequency(1))
        self.assertIn("between 2 and 60", _c.act_frequency(90))

    def test_faster_means_five(self):
        _c._systemctl = lambda *a: None
        self.assertIn("every 5", _c.handle_command("go faster", Broker()))


class TestBuy(unittest.TestCase):
    def setUp(self):
        class Inst:
            security_id = "2885"

        class Master:
            def load(self):
                return self

            def resolve(self, sym):
                if sym == "RELIANCE":
                    return Inst()
                raise KeyError(sym)
        _c.Instruments = lambda **k: Master()

    def test_buy_is_capped_at_half_of_cash(self):
        b = Broker(cash="25000", ltp="1310")
        r = _c.handle_command("buy 100 reliance", b)
        self.assertIn("over the chat buy cap", r)
        self.assertIn("up to 9 shares", r)
        self.assertEqual(b.posts, [], "nothing sent")

    def test_buy_inside_the_cap_is_a_market_intraday_order(self):
        b = Broker(cash="25000", ltp="1310")
        r = _c.handle_command("buy 5 RELIANCE", b)
        self.assertIn("Buying RELIANCE x5", r)
        path, body = b.posts[0]
        self.assertEqual(path, "/order")
        self.assertEqual(body["txn_type"], "BUY")
        self.assertEqual(body["product"], "INTRADAY")
        self.assertEqual(body["order_type"], "MARKET")
        self.assertTrue(body["remarks"].startswith("chat/RELIANCE/"))

    def test_both_word_orders(self):
        b = Broker()
        _c.handle_command("buy reliance 3", b)
        self.assertEqual(b.posts[0][1]["qty"], 3)

    def test_unknown_symbol(self):
        b = Broker()
        self.assertIn("Unknown symbol", _c.handle_command("buy 1 NOPE", b))
        self.assertEqual(b.posts, [])


class TestSell(unittest.TestCase):
    POS = [{"security_id": "2885", "name": "Reliance Industries", "net_qty": 1,
            "avg_price": "1316.6", "product": "INTRADAY", "exchange": "NSE"}]

    def test_sell_by_name_prefix(self):
        _c._sleep = lambda s: None
        b = Broker(positions=self.POS)
        r = _c.handle_command("sell reliance", b)
        self.assertIn("Selling Reliance Industries x1", r)
        self.assertEqual(b.posts[-1][1]["txn_type"], "SELL")

    def test_sell_all(self):
        _c._sleep = lambda s: None
        b = Broker(positions=self.POS)
        self.assertIn("Selling", _c.handle_command("sell all", b))

    def test_nothing_to_sell(self):
        self.assertIn("No open intraday position", _c.handle_command("sell ITC", Broker()))


class TestLimitsAndMisc(unittest.TestCase):
    def test_set_max_positions(self):
        r = _c.handle_command("set max positions to 3", Broker())
        self.assertIn("TRADER_MAX_CONCURRENT=3", r)
        self.assertIn("TRADER_MAX_CONCURRENT=3", Path(os.environ["TRADER_ENV_FILE"]).read_text())

    def test_leverage_over_five_refused(self):
        self.assertIn("refused", _c.handle_command("leverage 10", Broker()))

    def test_status_reads_the_broker(self):
        r = _c.handle_command("status", Broker())
        self.assertIn("cash 25000", r)
        self.assertIn("LIVE", r)

    def test_free_text_with_nothing_to_answer_gets_help(self):
        _c.find_claude = lambda: None
        r = _c.handle("what do you think about nifty today", Broker())
        self.assertIn("no Claude login", r)

    def test_free_text_goes_to_the_cli_and_actions_run(self):
        _c.find_claude = lambda: "/x/claude"
        _c._run_cli = lambda prompt: '{"reply": "Stopping as asked.", "actions": [{"name": "stop", "args": {"reason": "owner asked"}}]}'
        r = _c.handle("please stop for now", Broker())
        self.assertIn("Stopping as asked.", r)
        self.assertIn("[stop] Stopped", r)
        self.assertTrue(os.path.exists(os.environ["TRADER_KILL_SWITCH"]))
        os.unlink(os.environ["TRADER_KILL_SWITCH"])

    def test_cli_reply_in_a_code_fence_still_parses(self):
        got = _c._parse_json_reply('```json\n{"reply": "hi", "actions": []}\n```')
        self.assertEqual(got["reply"], "hi")

    def test_cli_runs_as_the_login_user_with_no_tools(self):
        import inspect
        src = inspect.getsource(_c._run_cli)
        self.assertIn('"sudo", "-u", CLI_USER', src)
        self.assertIn('"--tools", ""', src)

    def test_unrecognised_is_none_for_the_grammar(self):
        self.assertIsNone(_c.handle_command("hello there", Broker()))


if __name__ == "__main__":
    unittest.main()
