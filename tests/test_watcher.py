import os
import unittest
from pathlib import Path

from _load import ROOT, load

_w = load("indstocks", "watcher")
_s = load("indstocks", "session")
TODAY = _s.now().strftime("%Y-%m-%d")


class FakeClient:
    def __init__(self, orders=None, positions=None):
        self._orders = orders or []
        self._positions = positions or []

    def get(self, path, params=None):
        if "order-book" in path:
            return {"data": self._orders}
        if "positions" in path:
            return {"data": self._positions}
        return {"data": {}}


def parent(oid, name, traded=0, child=None, status="PENDING", sid="1",
           requested=None):
    return {"id": oid, "name": name, "traded_qty": traded, "status": status,
            "child_order_id": child, "security_id": sid,
            "requested_qty": requested if requested is not None else traded,
            "remarks": f"auto/{name}/{TODAY}"}


def child(oid, parent_id, status="PENDING", sid="1", qty=None):
    return {"id": oid, "name": "leg", "status": status, "security_id": sid,
            "parent_order_id": parent_id, "requested_qty": qty,
            "remarks": f"auto/x/{TODAY}"}


class TestProtection(unittest.TestCase):
    def test_filled_with_a_live_child_is_quiet(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=17, child="C1"),
                        child("C1", "P1")])
        self.assertEqual(_w.check(c)["alerts"], [])

    def test_filled_with_a_dead_child_is_unprotected(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=17, child="C1"),
                        child("C1", "P1", status="CANCELLED")])
        a = _w.check(c)["alerts"]
        self.assertEqual(a[0]["level"], "UNPROTECTED")
        self.assertEqual(a[0]["filled_qty"], 17)

    def test_filled_with_no_child_at_all_is_unprotected(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=17)])
        self.assertEqual(_w.check(c)["alerts"][0]["level"], "UNPROTECTED")

    def test_a_partial_fill_still_needs_a_stop(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=4)])
        self.assertEqual(_w.check(c)["alerts"][0]["filled_qty"], 4)

    def test_an_unfilled_order_is_not_an_alert(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=0)])
        self.assertEqual(_w.check(c)["alerts"], [])

    def test_child_found_by_back_reference_when_parent_lacks_the_id(self):
        # Sliced orders create the leg later, so the parent may not carry it.
        c = FakeClient([parent("P1", "RELIANCE", traded=17),
                        child("C1", "P1")])
        self.assertEqual(_w.check(c)["alerts"], [])


class TestPartialFills(unittest.TestCase):
    def test_leg_larger_than_fill_is_a_short_risk(self):
        # Ordered 17, 10 filled, the OCO leg is still for 17.
        c = FakeClient([parent("P1", "RELIANCE", traded=10, child="C1",
                               requested=17),
                        child("C1", "P1", qty=17)])
        a = _w.check(c)["alerts"]
        self.assertEqual(a[0]["level"], "PARTIAL_FILL")
        self.assertIn("short 7", a[0]["action"])

    def test_full_fill_with_matching_leg_is_quiet(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=17, child="C1",
                               requested=17),
                        child("C1", "P1", qty=17)])
        self.assertEqual(_w.check(c)["alerts"], [])

    def test_leg_without_a_quantity_field_does_not_false_alarm(self):
        c = FakeClient([parent("P1", "RELIANCE", traded=17, child="C1"),
                        child("C1", "P1")])
        self.assertEqual(_w.check(c)["alerts"], [])


class TestDuplicates(unittest.TestCase):
    def test_two_resting_orders_in_one_symbol_is_flagged(self):
        c = FakeClient([parent("P1", "RELIANCE", sid="2885"),
                        parent("P2", "RELIANCE", sid="2885")])
        a = _w.check(c)["alerts"]
        self.assertEqual(a[0]["level"], "DUPLICATE")
        self.assertEqual(a[0]["count"], 2)

    def test_todays_three_reliance_orders_would_have_been_caught(self):
        c = FakeClient([parent(f"P{i}", "RELIANCE", sid="2885") for i in range(3)])
        a = _w.check(c)["alerts"]
        self.assertEqual(a[0]["count"], 3)

    def test_one_order_per_symbol_is_fine(self):
        c = FakeClient([parent("P1", "RELIANCE", sid="2885"),
                        parent("P2", "ITC", sid="1660")])
        self.assertEqual(_w.check(c)["alerts"], [])

    def test_a_dead_order_is_not_counted_as_resting(self):
        c = FakeClient([parent("P1", "RELIANCE", sid="2885"),
                        parent("P2", "RELIANCE", sid="2885", status="CANCELLED")])
        self.assertEqual(_w.check(c)["alerts"], [])


class TestStaleOrders(unittest.TestCase):
    def test_yesterdays_resting_order_is_flagged(self):
        # 23:32 on 2026-11-04: six ITC orders from that day still open.
        # Seen from the next morning, they are stale.
        r = parent("P1", "ITC", sid="1660")
        r["remarks"] = "auto/ITC/2026-11-03"       # not today
        a = _w.check(FakeClient([r]))["alerts"]
        self.assertEqual(a[0]["level"], "STALE_ORDERS")
        self.assertIn("another day", a[0]["action"])

    def test_todays_resting_order_is_not_stale(self):
        import session as sess
        r = parent("P1", "ITC", sid="1660")
        r["remarks"] = f"auto/ITC/{sess.now():%Y-%m-%d}"
        self.assertEqual(_w.check(FakeClient([r]))["alerts"], [])

    def test_a_dead_order_from_yesterday_is_not_stale(self):
        r = parent("P1", "ITC", sid="1660", status="CANCELLED")
        r["remarks"] = "auto/ITC/2026-11-03"
        self.assertEqual(_w.check(FakeClient([r]))["alerts"], [])


class TestPush(unittest.TestCase):
    def test_no_topic_means_no_network(self):
        old = _w.NTFY_TOPIC
        _w.NTFY_TOPIC = ""
        try:
            _w.push("t", "b")      # must not raise, must not call out
        finally:
            _w.NTFY_TOPIC = old

    def test_unprotected_is_the_most_urgent(self):
        with open(os.path.join(ROOT, "indstocks", "watcher.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('"UNPROTECTED": 0', text)
        self.assertIn('priority="urgent" if top["level"] == "UNPROTECTED"', text)


class TestItNeverTrades(unittest.TestCase):
    def test_the_watcher_has_no_write_path(self):
        with open(os.path.join(ROOT, "indstocks", "watcher.py"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(".post(", text, "a watcher that acts is a watcher "
                                         "whose bugs place orders")

    def test_it_runs_every_five_minutes(self):
        timer = Path(ROOT, "scripts", "watch.timer").read_text(encoding="utf-8")
        self.assertIn("/5:00", timer)

    def test_every_timer_spec_is_valid_systemd(self):
        # An invalid OnCalendar is only rejected at `systemctl enable` time,
        # on the server, which is a poor place to discover a typo.
        import shutil
        import subprocess
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze not available")
        for timer in Path(ROOT, "scripts").glob("*.timer"):
            for line in timer.read_text(encoding="utf-8").splitlines():
                if not line.startswith("OnCalendar="):
                    continue
                spec = line[len("OnCalendar="):].replace(" Asia/Kolkata", "")
                r = subprocess.run(["systemd-analyze", "calendar", spec],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0,
                                 f"{timer.name}: invalid spec {spec!r}")


if __name__ == "__main__":
    unittest.main()


class TestDashboardOnlyReducesExposure(unittest.TestCase):
    """The dashboard may halt, resume, edit limits, CANCEL an order pair
    and EXIT a position. It may never place or modify an entry."""

    def source(self):
        with open(os.path.join(ROOT, "indstocks", "dashboard.py"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_the_only_api_write_is_cancel(self):
        text = self.source()
        posts = [ln for ln in text.splitlines() if ".post(" in ln]
        self.assertEqual(len(posts), 1, posts)
        self.assertIn('"/smart/order/cancel"', posts[0])
        for route in ('"/order"', '"/smart/order"', "/order/modify",
                      "/smart/order/modify"):
            self.assertNotIn(route, text, route)
        # The exit sells through flatten.sell, which is SELL-only.
        self.assertIn("from flatten import cancel_resting_legs, open_intraday, sell", text)

    def test_cancel_takes_the_leg_with_the_entry(self):
        text = self.source()
        self.assertIn('str(r.get("parent_order_id") or "") == order_id', text)

    def test_post_routes(self):
        text = self.source()
        start = text.index("def do_POST")
        end = text.index("def log_message", start)
        body = text[start:end]
        for route in ("/halt", "/resume", "/limits", "/cancel", "/exit"):
            self.assertIn(f'path == "{route}"', body)
        self.assertIn("unknown action", body)

    def test_never_binds_all_interfaces(self):
        text = self.source()
        self.assertIn("refusing to bind the dashboard to all interfaces", text)
        self.assertIn('ip.startswith("100.")', text)

    def test_leverage_edit_is_capped(self):
        self.assertIn("leverage above 5x refused", self.source())

    def test_limits_are_validated_before_the_file_is_touched(self):
        text = self.source()
        start = text.index("def write_limits")
        read_at = text.index("ENV_FILE.read_text", start)
        val_at = text.index("not a number", start)
        self.assertLess(val_at, read_at, "validate first, then write")

    def test_bind_refuses_all_interfaces(self):
        import os as _os
        _os.environ["TRADER_DASHBOARD_BIND"] = "0.0.0.0"
        try:
            mod = load("indstocks", "dashboard")
            with self.assertRaises(SystemExit):
                mod.bind_addresses()
        finally:
            _os.environ.pop("TRADER_DASHBOARD_BIND", None)
