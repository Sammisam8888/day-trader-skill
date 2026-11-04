import os
import unittest
from pathlib import Path

from _load import ROOT, load

_w = load("indstocks", "watcher")


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


def parent(oid, name, traded=0, child=None, status="PENDING", sid="1"):
    return {"id": oid, "name": name, "traded_qty": traded, "status": status,
            "child_order_id": child, "security_id": sid,
            "remarks": f"auto/{name}/2026-11-04"}


def child(oid, parent_id, status="PENDING", sid="1"):
    return {"id": oid, "name": "leg", "status": status, "security_id": sid,
            "parent_order_id": parent_id, "remarks": "auto/x/2026-11-04"}


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


if __name__ == "__main__":
    unittest.main()
