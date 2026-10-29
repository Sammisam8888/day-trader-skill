import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

from _load import load

_t = load("indstocks", "ticket")
_so = load("indstocks", "smartorder")
_r = load("indstocks", "risk")


class Inst:
    security_id = "2885"
    tick_size = D("0.10")
    exchange = "NSE"


def order():
    return _so.build_for_instrument(Inst(), qty=10, entry=D("1502.40"),
                                    stop=D("1488.10"), target=D("1530.60"),
                                    remarks="orb-v1/RELIANCE")


def verdict(passed=True, reasons=None):
    return _r.Verdict(passed, reasons or [])


def ticket(passed=True):
    return _t.build_ticket("RELIANCE", order(), risk_verdict=verdict(passed),
                           max_loss=D("143"))


class TestApproval(unittest.TestCase):
    def test_plain_yes_is_consent(self):
        tk = _t.approve(ticket(), "yes")
        self.assertTrue(tk.approved)

    def test_ambiguous_replies_are_not_consent(self):
        for reply in ("sure", "ok", "yeah go ahead", "yes to all", "y3s",
                      "yes buy TCS instead", "", "no"):
            with self.assertRaises(_t.NotApproved, msg=reply):
                _t.approve(ticket(), reply)

    def test_approval_cannot_override_a_risk_block(self):
        with self.assertRaises(_t.NotApproved) as cm:
            _t.approve(ticket(passed=False), "yes")
        self.assertIn("cannot override", str(cm.exception))

    def test_fingerprint_changes_with_quantity(self):
        a = order().payload()
        b = dict(a, qty=20)
        self.assertNotEqual(_t.fingerprint(a), _t.fingerprint(b))

    def test_fingerprint_changes_with_stop_price(self):
        a = order().payload()
        b = dict(a, sl_trigger_price=1400.0)
        self.assertNotEqual(_t.fingerprint(a), _t.fingerprint(b))


class FakeClient:
    def __init__(self, place_resp=None, book=None):
        self.place_resp = place_resp or {
            "status": "success",
            "data": {"order_data": [{"order_id": "EQ-1", "order_status": "CREATED",
                                     "child_order_details": {"order_id": "GTT-9",
                                                             "order_status": "CREATED"}}]}}
        self.book = book or []
        self.posted = []

    def post(self, path, body=None):
        self.posted.append((path, body))
        return self.place_resp

    def get(self, path, params=None):
        return {"data": self.book}


class TestPlacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = _t.STATE_DIR
        _t.STATE_DIR = Path(self.tmp.name)

    def tearDown(self):
        _t.STATE_DIR = self._old
        self.tmp.cleanup()

    def test_unapproved_ticket_is_never_sent(self):
        c = FakeClient()
        with self.assertRaises(_t.NotApproved):
            _t.place(c, ticket())
        self.assertEqual(c.posted, [], "nothing may reach the broker")

    def test_risk_blocked_ticket_is_never_sent(self):
        c = FakeClient()
        tk = ticket(passed=False)
        tk.approved_reply, tk.approved_at = "yes", "now"   # forged
        with self.assertRaises(_t.NotApproved):
            _t.place(c, tk)
        self.assertEqual(c.posted, [])

    def test_payload_edited_after_approval_is_rejected(self):
        c = FakeClient()
        tk = _t.approve(ticket(), "yes")
        tk.payload["qty"] = 1000          # tampered between yes and send
        with self.assertRaises(_t.NotApproved) as cm:
            _t.place(c, tk)
        self.assertIn("changed after approval", str(cm.exception))
        self.assertEqual(c.posted, [])

    def test_approved_ticket_is_sent_once(self):
        c = FakeClient()
        tk = _t.place(c, _t.approve(ticket(), "yes"))
        self.assertEqual(len(c.posted), 1)
        self.assertEqual(c.posted[0][0], "/smart/order")
        self.assertEqual(tk.parent_order_id, "EQ-1")
        self.assertEqual(tk.child_order_id, "GTT-9")

    def test_one_yes_cannot_place_a_second_order(self):
        c = FakeClient()
        tk = _t.place(c, _t.approve(ticket(), "yes"))
        with self.assertRaises(_t.TicketError):
            _t.place(c, tk)
        self.assertEqual(len(c.posted), 1)


class TestProtection(unittest.TestCase):
    def tk(self):
        t = ticket()
        t.parent_order_id, t.child_order_id = "EQ-1", "GTT-9"
        return t

    def test_filled_with_a_live_child_is_protected(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "SUCCESS", "traded_qty": 10},
                             {"id": "GTT-9", "status": "PENDING"}])
        p = _t.confirm_protection(c, self.tk())
        self.assertTrue(p.protected)
        self.assertEqual(p.filled_qty, 10)

    def test_filled_with_a_dead_child_is_flagged_unprotected(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "SUCCESS", "traded_qty": 10},
                             {"id": "GTT-9", "status": "CANCELLED"}])
        p = _t.confirm_protection(c, self.tk())
        self.assertFalse(p.protected)
        self.assertIn("UNPROTECTED", p.message)

    def test_filled_with_no_child_at_all_is_flagged(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "SUCCESS", "traded_qty": 10}])
        p = _t.confirm_protection(c, self.tk())
        self.assertFalse(p.protected)
        self.assertIn("UNPROTECTED", p.message)

    def test_partial_fill_still_needs_protection(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "PARTIALLY FILLED",
                              "traded_qty": 4}])
        self.assertFalse(_t.confirm_protection(c, self.tk()).protected)

    def test_unfilled_working_order_is_not_an_alarm(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "PENDING", "traded_qty": 0}])
        p = _t.confirm_protection(c, self.tk())
        self.assertTrue(p.protected)
        self.assertIn("still working", p.message)

    def test_rejected_entry_is_not_an_alarm(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "REJECTED", "traded_qty": 0}])
        p = _t.confirm_protection(c, self.tk())
        self.assertTrue(p.protected)
        self.assertIn("did not fill", p.message)

    def test_missing_parent_is_reported_not_assumed_safe(self):
        p = _t.confirm_protection(FakeClient(book=[]), self.tk())
        self.assertFalse(p.protected)

    def test_wait_for_fill_stops_once_filled(self):
        c = FakeClient(book=[{"id": "EQ-1", "status": "SUCCESS", "traded_qty": 10},
                             {"id": "GTT-9", "status": "PENDING"}])
        slept = []
        p = _t.wait_for_fill(c, self.tk(), sleep=slept.append)
        self.assertEqual(p.filled_qty, 10)
        self.assertEqual(slept, [], "no polling needed when already filled")


class TestFlatten(unittest.TestCase):
    def test_flatten_is_a_plain_market_sell(self):
        o = _t.build_flatten(Inst(), 10)
        self.assertEqual(o["txn_type"], "SELL")
        self.assertEqual(o["order_type"], "MARKET")
        self.assertEqual(o["qty"], 10)
        for leg in ("sl_trigger_price", "tgt_trigger_price", "limit_price"):
            self.assertNotIn(leg, o)


if __name__ == "__main__":
    unittest.main()
