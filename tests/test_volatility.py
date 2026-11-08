import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

from _load import load

_v = load("indstocks", "volatility")
_b = load("indstocks", "market")
_s = load("indstocks", "session")


def day(n, o, h, l, c, v=1_000_000):
    return _b.Candle(
        opens_at=datetime(2026, 1, 1, 9, 15, tzinfo=_s.IST) + timedelta(days=n),
        open=D(str(o)), high=D(str(h)), low=D(str(l)), close=D(str(c)),
        volume=v, interval_minutes=375)


class TestProfile(unittest.TestCase):
    def test_up_pct_is_open_to_high(self):
        p = _v.profile("X", [day(0, 100, 105, 99, 103)])
        self.assertEqual(p.median_up, D("5.00"))
        self.assertEqual(p.median_range, D("6.00"))

    def test_hit_rate_counts_days_clearing_each_threshold(self):
        candles = [day(0, 100, 101, 99, 100),    # 1%
                   day(1, 100, 103, 99, 102),    # 3%
                   day(2, 100, 106, 99, 105),    # 6%
                   day(3, 100, 100.5, 99, 100)]  # 0.5%
        p = _v.profile("X", candles)
        self.assertEqual(p.hits(D("1")), D("75.0"))
        self.assertEqual(p.hits(D("3")), D("50.0"))
        self.assertEqual(p.hits(D("5")), D("25.0"))
        self.assertEqual(p.hits(D("10")), D("0.0"))

    def test_zero_open_is_skipped_not_a_division_error(self):
        p = _v.profile("X", [day(0, 0, 0, 0, 0), day(1, 100, 102, 99, 101)])
        self.assertEqual(p.days, 1)

    def test_empty_history_is_safe(self):
        p = _v.profile("X", [])
        self.assertEqual(p.days, 0)
        self.assertEqual(p.median_up, D("0"))
        self.assertEqual(p.hits(D("5")), D("0"))


class TestRank(unittest.TestCase):
    def mk(self, sym, up, days=250, value=10**9):
        candles = [day(i, 100, 100 + up, 99, 100 + up) for i in range(days)]
        p = _v.profile(sym, candles)
        return _v.Profile(sym, p.days, p.median_up, p.median_range, p.p90_up,
                          p.hit_rates, D(str(value)))

    def test_most_volatile_first(self):
        out = _v.rank([self.mk("CALM", 1), self.mk("WILD", 8),
                       self.mk("MID", 4)])
        self.assertEqual([p.symbol for p in out], ["WILD", "MID", "CALM"])

    def test_thin_names_are_excluded_however_volatile(self):
        # A stock can swing 8% and still be untradeable: a stop-limit in a
        # thin book does not fill at its trigger.
        out = _v.rank([self.mk("THIN", 8, value=1000),
                       self.mk("LIQUID", 4, value=10**9)])
        self.assertEqual([p.symbol for p in out], ["LIQUID"])

    def test_short_history_is_excluded(self):
        out = _v.rank([self.mk("NEW", 8, days=20), self.mk("OLD", 4)])
        self.assertEqual([p.symbol for p in out], ["OLD"])


if __name__ == "__main__":
    unittest.main()
