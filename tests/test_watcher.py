"""Watcher alerts, timer specs, and the dashboard's safety properties."""

import os
import shutil
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, FakeBroker, order, position, reset  # noqa: E402
from _load import ROOT  # noqa: E402

import session as sess  # noqa: E402
import shoonya as sh  # noqa: E402
import watcher  # noqa: E402

watcher.monitor_running = lambda: True


def at(hh, mm, day=11):
    return datetime(2026, 11, day, hh, mm, tzinfo=sess.IST)


class TestAlerts(unittest.TestCase):
    def setUp(self):
        reset()
        sh.save_token("T", "U", "U")
        sess.now = lambda clock=None: at(10, 0)

    def levels(self, b, now):
        return [lvl for lvl, _ in watcher.alerts(b, now)]

    def test_no_login_before_the_open(self):
        sh.clear_token()
        self.assertEqual(self.levels(FakeBroker(), at(8, 45)), ["LOGIN"])

    def test_quiet_when_protected(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")],
                       orders=[order("S", NIFTY, side="S", qty=40, trigger="280.3", price="278.9")])
        self.assertEqual(self.levels(b, at(10, 0)), [])

    def test_a_long_with_no_sell_order_is_unprotected(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")])
        self.assertEqual(self.levels(b, at(10, 0)), ["UNPROTECTED"])

    def test_open_after_the_flatten_is_near_close(self):
        b = FakeBroker(positions=[position(NIFTY, 40, "281")])
        self.assertIn("NEAR CLOSE", self.levels(b, at(15, 14)))

    def test_monitor_down(self):
        watcher.monitor_running = lambda: False
        try:
            self.assertIn("MONITOR", self.levels(FakeBroker(), at(10, 0)))
        finally:
            watcher.monitor_running = lambda: True

    def test_weekend_is_silent(self):
        sh.clear_token()
        self.assertEqual(self.levels(FakeBroker(), at(10, 0, day=14)), [])

    def test_the_watcher_never_places_or_cancels(self):
        text = Path(ROOT, "indstocks", "watcher.py").read_text(encoding="utf-8")
        for call in (".place(", ".cancel(", ".modify("):
            self.assertNotIn(call, text)


class TestTimers(unittest.TestCase):
    def test_every_timer_spec_is_valid_systemd(self):
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze not available")
        for timer in Path(ROOT, "scripts").glob("*.timer"):
            for line in timer.read_text(encoding="utf-8").splitlines():
                if line.startswith("OnCalendar=") and line != "OnCalendar=":
                    spec = line[len("OnCalendar="):].replace(" Asia/Kolkata", "")
                    r = subprocess.run(["systemd-analyze", "calendar", spec], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, f"{timer.name}: {spec!r}")

    def test_timers_run_every_day_so_special_sessions_are_covered(self):
        for name in ("auto.timer", "watch.timer", "flatten.timer"):
            text = Path(ROOT, "scripts", name).read_text(encoding="utf-8")
            self.assertNotIn("Mon-Fri", text, name)


class TestDashboard(unittest.TestCase):
    def source(self):
        return Path(ROOT, "indstocks", "dashboard.py").read_text(encoding="utf-8")

    def test_the_dashboard_never_places_an_entry_itself(self):
        text = self.source()
        self.assertNotIn(".place(", text, "entries come from the scanner or a capped chat buy")
        self.assertNotIn('"PlaceOrder"', text)

    def test_post_routes(self):
        text = self.source()
        body = text[text.index("def do_POST"):text.index("def log_message")]
        for route in ("/halt", "/resume", "/limits", "/cancel", "/exit", "/chat", "/term", "/shoonya/code"):
            self.assertIn(f'path == "{route}"', body)
        self.assertIn("unknown action", body)

    def test_login_routes(self):
        text = self.source()
        self.assertIn('path == "/shoonya/login"', text)
        self.assertIn('path == "/shoonya/callback"', text)
        self.assertIn("sh.exchange_code(code)", text)

    def test_never_binds_all_interfaces(self):
        os.environ["TRADER_DASHBOARD_BIND"] = "0.0.0.0"
        try:
            import importlib
            import dashboard
            importlib.reload(dashboard)
            with self.assertRaises(SystemExit):
                dashboard.bind_addresses()
        finally:
            os.environ.pop("TRADER_DASHBOARD_BIND", None)

    def test_limits_are_validated_before_the_file_is_touched(self):
        text = self.source()
        start = text.index("def write_limits")
        self.assertLess(text.index("not a number", start), text.index("ENV_FILE.read_text", start))
        self.assertIn("leverage above 5x refused", text)

    def test_page_renders_logged_out(self):
        import dashboard
        reset(SHOONYA_CLIENT_ID="FN245398_U")
        sh.clear_token()
        html = dashboard.render("2026-11-11")
        self.assertIn("Not logged in to Shoonya", html)
        self.assertIn("/shoonya/login", html)

    def test_page_renders_with_positions_and_orders(self):
        import dashboard
        reset()
        sh.save_token("T", "FN245398", "FN245398")
        orig = sh.Shoonya
        sh.Shoonya = lambda: FakeBroker(
            positions=[position(NIFTY, 40, "281.02", ltp="281.50")],
            orders=[order("S1", NIFTY, side="S", qty=40, trigger="280.30", price="278.90")])
        try:
            html = dashboard.render("2026-11-11")
        finally:
            sh.Shoonya = orig
        self.assertIn("NIFTYBEES", html)
        self.assertIn("Stop at the exchange", html)
        self.assertIn("280.30", html)


if __name__ == "__main__":
    unittest.main()


class TestApi(unittest.TestCase):
    """The JSON API the React dashboard renders from."""

    def setUp(self):
        reset()
        sh.save_token("T", "FN245398", "FN245398")
        sess.now = lambda clock=None: at(10, 30)

    def tearDown(self):
        import metrics
        metrics.SNAPSHOT.unlink(missing_ok=True)

    def test_state_from_the_monitor_snapshot_needs_no_broker(self):
        import dashboard, metrics
        from decimal import Decimal as Dd
        metrics.write_snapshot({"positions": [position(NIFTY, 44, "281.02")],
                                "orders": [order("S1", NIFTY, side="S", qty=44, trigger="280.30", price="278.90",
                                                 remarks="stop/NIFTYBEES")],
                                "ltps": {NIFTY.token: Dd("282.00")}, "cash": Dd("26319")})
        orig = sh.Shoonya
        sh.Shoonya = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no broker call expected"))
        try:
            s = dashboard.api_state()
        finally:
            sh.Shoonya = orig
        self.assertEqual(s["data"]["source"], "monitor")
        self.assertEqual(s["positions"][0]["symbol"], "NIFTYBEES")
        self.assertTrue(s["positions"][0]["stop_live"])
        self.assertAlmostEqual(s["positions"][0]["pnl"], 43.12)
        self.assertEqual(s["orders"][0]["kind"], "stop")
        self.assertEqual(s["orders"][0]["source"], "ai")
        self.assertEqual(s["cash"], 26319.0)

    def test_logged_out_state(self):
        import dashboard
        sh.clear_token()
        s = dashboard.api_state()
        self.assertEqual(s["status"], "login")
        self.assertFalse(s["login"]["ok"])

    def test_actions(self):
        import common, dashboard
        self.assertTrue(dashboard.api_action({"action": "halt", "reason": "test"})["ok"])
        self.assertTrue(common.KILL_SWITCH.exists())
        self.assertTrue(dashboard.api_action({"action": "resume"})["ok"])
        self.assertFalse(common.KILL_SWITCH.exists())
        self.assertFalse(dashboard.api_action({"action": "rm -rf"})["ok"])

    def test_pnl_series_and_journal(self):
        import common, dashboard
        common.log("fill", {"event": "pnl", "realised": "0", "open": "12.5", "charges": "3", "net": "9.5"})
        s = dashboard.api_state()
        self.assertEqual(s["series"][-1]["net"], 9.5)
        self.assertEqual(s["stats"]["history"][-1]["day"], "2026-11-11")

    def test_built_frontend_is_committed(self):
        dist = Path(ROOT, "indstocks", "web", "dist", "index.html")
        self.assertTrue(dist.is_file(), "run `npm run build` in indstocks/web and commit dist/")
