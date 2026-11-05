"""The fill monitor: cancel-on-fill, manual-position protection, near-close
sweep. It is the piece that must react in seconds, so it is mechanical."""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _load import ROOT, load  # noqa: E402

os.environ["TRADER_STATE_DIR"] = tempfile.mkdtemp()
os.environ["TRADER_MAX_CONCURRENT"] = "2"
os.environ["TRADER_ENV_FILE"] = os.path.join(tempfile.mkdtemp(), "none")
_f = load("indstocks", "fillmonitor")
_s = load("indstocks", "session")


class Broker:
    """Scripted broker: an order book, positions, LTPs; records POSTs."""

    def __init__(self, book=(), positions=(), ltp=None):
        self.book, self.positions, self.ltp = list(book), list(positions), ltp or {}
        self.posts = []

    cash = "25000"

    def get(self, path, params=None):
        if path == "/funds":
            return {"data": {"detailed_avl_balance": {"eq_mis": self.cash}}}
        if path == "/order-book":
            return {"data": self.book}
        if path == "/portfolio/positions":
            return {"data": self.positions}
        if path == "/market/quotes/ltp":
            return {"data": {c: {"live_price": str(self.ltp[c.split('_', 1)[1]])}
                             for c in params["scrip-codes"].split(",")
                             if c.split("_", 1)[1] in self.ltp}}
        raise AssertionError(path)

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {"data": {"order_id": "X"}}


def entry(oid, sid, name, traded=0, status="PENDING", price="100", qty=10):
    return {"id": oid, "security_id": sid, "name": name, "txn_type": "BUY",
            "status": status, "traded_qty": traded, "requested_qty": qty,
            "trigger_price": price,
            "remarks": f"auto/{name}/2026-11-05", "order_type": "TRIGGER"}


def leg(oid, parent, sid, status="PENDING"):
    return {"id": oid, "parent_order_id": parent, "security_id": sid,
            "txn_type": "SELL", "status": status, "traded_qty": 0}


def pos(sid, name, qty, avg):
    return {"security_id": sid, "name": name, "net_qty": qty, "avg_price": avg,
            "product": "INTRADAY", "exchange": "NSE"}


MID = datetime(2026, 11, 5, 11, 0, tzinfo=_s.IST)
LATE = datetime(2026, 11, 5, 14, 45, tzinfo=_s.IST)


class TestCancelOnFill(unittest.TestCase):
    def test_nothing_happens_under_the_cap(self):
        b = Broker(book=[entry("P1", "1", "A"), entry("P2", "2", "B"), entry("P3", "3", "C")],
                   positions=[pos("1", "A", 10, "100")])
        # A is both held (filled) and has a leg, so it is protected
        b.book.append(leg("L1", "P1", "1"))
        b.book[0]["traded_qty"] = 10
        out = _f.tick(b, MID)
        self.assertEqual(out["cancelled"], 0)
        self.assertEqual(b.posts, [])

    def test_every_other_resting_entry_goes_when_the_cap_is_reached(self):
        b = Broker(book=[entry("P1", "1", "A", traded=10), leg("L1", "P1", "1"),
                         entry("P2", "2", "B", traded=10), leg("L2", "P2", "2"),
                         entry("P3", "3", "C"), entry("P4", "4", "D")],
                   positions=[pos("1", "A", 10, "100"), pos("2", "B", 10, "50")])
        out = _f.tick(b, MID)
        self.assertEqual(out["cancelled"], 2)
        cancelled = sorted(body["order_id"] for path, body in b.posts
                           if path == "/smart/order/cancel")
        self.assertEqual(cancelled, ["P3", "P4"], "the filled ones stay")

    def test_a_manual_position_counts_toward_the_cap(self):
        # 1 share of RELIANCE bought by hand + one bot fill = cap of 2.
        b = Broker(book=[entry("P1", "1", "A", traded=10), leg("L1", "P1", "1"),
                         entry("P3", "3", "C")],
                   positions=[pos("1", "A", 10, "100"), pos("2885", "RELIANCE", 1, "1316.6")],
                   ltp={"2885": "1310"})
        out = _f.tick(b, MID)
        self.assertEqual(out["cancelled"], 1)

    def test_never_cancels_a_leg_or_a_sell(self):
        b = Broker(book=[entry("P1", "1", "A", traded=10), leg("L1", "P1", "1"),
                         entry("P2", "2", "B", traded=10), leg("L2", "P2", "2")],
                   positions=[pos("1", "A", 10, "100"), pos("2", "B", 10, "50")])
        _f.tick(b, MID)
        self.assertEqual(b.posts, [], "legs are what protect the fills")


class TestCancelOnExposure(unittest.TestCase):
    def test_resting_order_that_would_breach_leverage_is_cancelled(self):
        # One fill of 20,000 held; a resting 13,000 would make 33,000 > 25,000.
        b = Broker(book=[entry("P1", "1", "A", traded=10, price="2000"), leg("L1", "P1", "1"),
                         entry("P2", "2", "B", price="1300", qty=10),
                         entry("P3", "3", "C", price="400", qty=10)],      # 4,000 fits
                   positions=[pos("1", "A", 10, "2000")])
        out = _f.tick(b, MID)
        self.assertEqual(out["cancelled"], 1)
        self.assertEqual([body["order_id"] for _, body in b.posts], ["P2"])

    def test_limits_are_read_fresh_each_tick(self):
        path = str(_f.ENV_FILE)
        with open(path, "w") as fh:
            fh.write("TRADER_MAX_CONCURRENT=1\n")
        try:
            self.assertEqual(_f.max_concurrent(), 1)
        finally:
            os.unlink(path)
        self.assertEqual(_f.max_concurrent(), 2)


class TestManualPositions(unittest.TestCase):
    def test_bare_position_up_past_target_is_sold(self):
        b = Broker(positions=[pos("2885", "RELIANCE", 1, "1316.60")],
                   ltp={"2885": "1330.00"})            # +1.02%
        out = _f.tick(b, MID)
        self.assertEqual(out["sold"], 1)
        sells = [body for path, body in b.posts if path == "/order"]
        self.assertEqual(sells[0]["txn_type"], "SELL")
        self.assertEqual(sells[0]["qty"], 1)

    def test_bare_position_down_past_stop_is_sold(self):
        b = Broker(positions=[pos("2885", "RELIANCE", 1, "1316.60")],
                   ltp={"2885": "1309.00"})            # -0.58%
        self.assertEqual(_f.tick(b, MID)["sold"], 1)

    def test_bare_position_inside_the_band_is_held(self):
        b = Broker(positions=[pos("2885", "RELIANCE", 1, "1316.60")],
                   ltp={"2885": "1312.00"})            # -0.35%
        self.assertEqual(_f.tick(b, MID)["sold"], 0)
        self.assertEqual(b.posts, [])

    def test_a_position_with_its_own_sell_leg_is_left_to_it(self):
        b = Broker(book=[entry("P1", "2885", "RELIANCE", traded=1), leg("L1", "P1", "2885")],
                   positions=[pos("2885", "RELIANCE", 1, "1316.60")],
                   ltp={"2885": "1400.00"})
        self.assertEqual(_f.tick(b, MID)["sold"], 0)

    def test_decision_arithmetic(self):
        self.assertIsNone(_f.manual_decision(Decimal("100"), Decimal("100.9")))
        self.assertIn("target", _f.manual_decision(Decimal("100"), Decimal("101")))
        self.assertIn("stop", _f.manual_decision(Decimal("100"), Decimal("99.5")))
        self.assertIsNone(_f.manual_decision(Decimal("0"), Decimal("1")))


class TestNearClose(unittest.TestCase):
    def test_resting_entries_are_swept_after_last_entry(self):
        b = Broker(book=[entry("P1", "1", "A"), entry("P2", "2", "B")])
        out = _f.tick(b, LATE)
        self.assertEqual(out["cancelled"], 2)

    def test_idle_outside_the_session(self):
        b = Broker(book=[entry("P1", "1", "A")])
        self.assertTrue(_f.tick(b, datetime(2026, 11, 5, 8, 0, tzinfo=_s.IST))["idle"])
        self.assertEqual(b.posts, [])


class TestService(unittest.TestCase):
    def test_unit_file_and_deploy(self):
        with open(os.path.join(ROOT, "scripts", "fillmonitor.service")) as fh:
            unit = fh.read()
        self.assertIn("Restart=always", unit)
        self.assertIn("EnvironmentFile=/etc/indstocks.env", unit)
        with open(os.path.join(ROOT, ".github", "workflows", "deploy.yml")) as fh:
            self.assertIn("fillmonitor", fh.read())


if __name__ == "__main__":
    unittest.main()
