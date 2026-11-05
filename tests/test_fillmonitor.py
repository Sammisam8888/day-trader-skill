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


class TestRestingTTL(unittest.TestCase):
    def _log_placed(self, oid, ts):
        import json as _j
        d = _f.STATE_DIR / f"{_s.now():%Y-%m-%d}.jsonl"
        d.parent.mkdir(parents=True, exist_ok=True)
        with open(d, "a") as fh:
            fh.write(_j.dumps({"ts": ts, "event": "placed", "parent_order_id": oid}) + "\n")

    def test_old_resting_entry_is_cancelled_young_one_kept(self):
        now = _s.now()
        self._log_placed("OLD", (now - __import__("datetime").timedelta(minutes=45)).isoformat())
        self._log_placed("NEW", (now - __import__("datetime").timedelta(minutes=5)).isoformat())
        b = Broker(book=[entry("OLD", "1", "A"), entry("NEW", "2", "B")])
        n = _f.expire_stale(b, b.book, now)
        self.assertEqual(n, 1)
        self.assertEqual([body["order_id"] for _, body in b.posts], ["OLD"])

    def test_ttl_zero_means_never(self):
        with open(str(_f.ENV_FILE), "w") as fh:
            fh.write("TRADER_RESTING_TTL_MIN=0\n")
        try:
            b = Broker(book=[entry("OLD", "1", "A")])
            self.assertEqual(_f.expire_stale(b, b.book, _s.now()), 0)
        finally:
            os.unlink(str(_f.ENV_FILE))


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


class TestCancelPayload(unittest.TestCase):
    def test_segment_is_sent(self):
        # 2026-11-05 13:50: {"order_id": ...} alone is a 400 at the broker.
        b = Broker(book=[entry("P1", "1", "A")])
        _f.cancel_entry(b, b.book[0])
        self.assertEqual(b.posts, [("/smart/order/cancel",
                                    {"order_id": "P1", "segment": "EQUITY"})])

    def test_every_cancel_in_the_codebase_goes_through_one_function(self):
        import glob
        hits = []
        for path in glob.glob(os.path.join(ROOT, "indstocks", "*.py")):
            with open(path, encoding="utf-8") as fh:
                for i, ln in enumerate(fh, 1):
                    if 'post("/smart/order/cancel"' in ln:
                        hits.append(f"{os.path.basename(path)}:{i}")
        self.assertEqual(hits, ["flatten.py:" + hits[0].split(":")[1]] if hits else [], hits)


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


class TestTrailingStop(unittest.TestCase):
    """Breakeven at +1R, then trail 1R below price. Never down."""
    E, S, T, TICK = Decimal("759.20"), Decimal("755.70"), Decimal("766.50"), Decimal("0.05")

    def test_under_one_r_does_not_move(self):
        self.assertIsNone(_f.new_stop(self.E, self.S, Decimal("761.00"), self.TICK, self.S))

    def test_at_one_r_locks_breakeven(self):
        # R = 3.50; +1R = 762.70
        self.assertEqual(_f.new_stop(self.E, self.S, Decimal("762.70"), self.TICK, self.S),
                         Decimal("759.20"))

    def test_beyond_one_and_a_half_r_trails(self):
        # +2R = 766.20 -> stop at ltp - 1R = 762.70
        self.assertEqual(_f.new_stop(self.E, self.S, Decimal("766.20"), self.TICK, self.S),
                         Decimal("762.70"))

    def test_never_moves_down(self):
        self.assertIsNone(_f.new_stop(self.E, self.S, Decimal("762.70"), self.TICK,
                                      current_sl=Decimal("763.00")))

    def test_rounds_down_to_tick(self):
        got = _f.new_stop(Decimal("100"), Decimal("99"), Decimal("102.333"), Decimal("0.05"), Decimal("99"))
        self.assertEqual(got, Decimal("101.30"))

    def test_modify_payload_has_segment_algo_and_both_legs(self):
        b = Broker()
        leg = {"id": "L1", "tgt_limit_price": "767.00"}
        _f.modify_leg(b, leg, Decimal("759.20"), self.TICK, self.T)
        path, body = b.posts[0]
        self.assertEqual(path, "/smart/order/modify")
        self.assertEqual(body["segment"], "EQUITY")
        self.assertEqual(body["algo_id"], "99999")
        self.assertEqual(body["sl_trigger_price"], 759.20)
        self.assertLess(body["sl_limit_price"], 759.20)
        self.assertEqual(body["tgt_trigger_price"], 766.50)

    def test_manage_stops_end_to_end(self):
        import json as _j
        d = _f.STATE_DIR / f"{_s.now():%Y-%m-%d}.jsonl"
        with open(d, "a") as fh:
            fh.write(_j.dumps({"ts": "x", "event": "placed", "parent_order_id": "PX", "symbol": "HDFCBANK",
                               "entry": "759.20", "stop": "755.70", "target": "766.50", "tick": "0.05"}) + "\n")
        b = Broker(book=[entry("PX", "1333", "HDFC Bank", traded=31),
                         {**leg("LX", "PX", "1333"), "name": "HDFC Bank", "sl_trigger_price": "755.70"}],
                   positions=[pos("1333", "HDFC Bank", 31, "759.30")],
                   ltp={"1333": "763.00"})
        out = _f.tick(b, MID)
        self.assertEqual(out["moved"], 1)
        self.assertEqual(b.posts[0][0], "/smart/order/modify")
        self.assertEqual(b.posts[0][1]["sl_trigger_price"], 759.20)


class TestInvalidateByPrice(unittest.TestCase):
    def test_resting_order_below_range_low_is_cancelled(self):
        import json as _j
        d = _f.STATE_DIR / f"{_s.now():%Y-%m-%d}.jsonl"
        with open(d, "a") as fh:
            fh.write(_j.dumps({"ts": "x", "event": "placed", "parent_order_id": "PR", "symbol": "ITC",
                               "entry": "410.10", "stop": "408.00", "target": "414.30",
                               "range_low": "405.90"}) + "\n")
        b = Broker(book=[entry("PR", "1660", "ITC")], ltp={"1660": "405.00"})
        n = _f.invalidate_by_price(b, b.book, {"1660": Decimal("405.00")})
        self.assertEqual(n, 1)
        self.assertEqual(b.posts[0][1]["order_id"], "PR")

    def test_inside_the_range_is_kept(self):
        b = Broker(book=[entry("PR", "1660", "ITC")])
        self.assertEqual(_f.invalidate_by_price(b, b.book, {"1660": Decimal("407.00")}), 0)
