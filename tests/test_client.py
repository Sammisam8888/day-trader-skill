import io
import json
import tempfile
import unittest
import urllib.error
from decimal import Decimal as D
from pathlib import Path

from _load import load

_c = load("indstocks", "client")
_i = load("indstocks", "instruments")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTokens:
    def __init__(self):
        self.generated = 0

    def auth_header(self):
        return {"Authorization": "TKN", "User-Agent": "test-agent"}

    def generate(self):
        self.generated += 1
        return "TKN2"


class TestRouting(unittest.TestCase):
    def test_paths_map_to_the_right_budget(self):
        for path, cat in (
            ("/order", "order"),
            ("/order/modify", "order"),
            ("/smart/order", "order"),
            ("/market/historical/5m", "data"),
            ("/market/instruments", "data"),
            ("/market/quotes/ltp", "quote"),
            ("/order-book", "non_trading"),
            ("/funds", "non_trading"),
            ("/portfolio/positions", "non_trading"),
        ):
            self.assertEqual(_c.category_for(path), cat, path)

    def test_unknown_path_is_treated_as_non_trading(self):
        self.assertEqual(_c.category_for("/something/new"), "non_trading")


class TestGovernor(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.slept = []

    def gov(self):
        def clock():
            return self.now[0]

        def sleep(s):
            self.slept.append(s)
            self.now[0] += s

        return _c.Governor(clock=clock, sleep=sleep, day_clock=lambda: 0.0)

    def test_burst_up_to_the_rate_then_throttles(self):
        rate = _c.LIMITS["data"][0]
        g = self.gov()
        for _ in range(rate):
            g.acquire("data")
        self.assertEqual(self.slept, [])
        g.acquire("data")           # one past the rate must wait
        self.assertTrue(self.slept and self.slept[0] > 0)

    def test_data_rate_stays_under_the_documented_five(self):
        # At the documented 5/sec the historical endpoint starts returning
        # success:true with empty candle arrays instead of 429.
        self.assertLess(_c.LIMITS["data"][0], 5)

    def test_orders_get_a_bigger_bucket_than_data(self):
        g = self.gov()
        for _ in range(10):
            g.acquire("order")
        self.assertEqual(self.slept, [])

    def test_budgets_are_independent(self):
        g = self.gov()
        for _ in range(_c.LIMITS["data"][0]):
            g.acquire("data")
        g.acquire("order")          # separate bucket, no wait
        self.assertEqual(self.slept, [])

    def test_daily_cap_raises_rather_than_sleeping(self):
        g = self.gov()
        g._used_today["data"] = 100_000
        with self.assertRaises(_c.DailyBudgetExhausted):
            g.acquire("data")

    def test_daily_counter_resets_on_a_new_day(self):
        day = [0.0]
        g = _c.Governor(clock=lambda: self.now[0], sleep=lambda s: None,
                        day_clock=lambda: day[0])
        g._used_today["data"] = 100_000
        day[0] = 86400 * 2
        g.acquire("data")           # new day, budget restored
        self.assertEqual(g.used_today("data"), 1)


class TestClient(unittest.TestCase):
    def client(self, opener, **kw):
        return _c.Client(tokens=FakeTokens(), governor=_c.Governor(sleep=lambda s: None),
                         opener=opener, **kw)

    def test_sends_auth_and_user_agent(self):
        seen = {}

        def opener(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["ua"] = req.get_header("User-agent")
            return FakeResp(b'{"status":"success"}')

        self.client(opener).get("/funds")
        self.assertEqual(seen["auth"], "TKN")
        self.assertEqual(seen["ua"], "test-agent")

    def test_token_exception_regenerates_once_and_retries(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    "u", 403, "Forbidden", {}, io.BytesIO(b'{"error":"TokenException"}'))
            return FakeResp(b'{"status":"success"}')

        t = FakeTokens()
        c = _c.Client(tokens=t, governor=_c.Governor(sleep=lambda s: None), opener=opener)
        self.assertEqual(c.get("/funds")["status"], "success")
        self.assertEqual(t.generated, 1)

    def test_cloudflare_block_is_named_as_such(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {},
                                         io.BytesIO(b"error code: 1010"))

        with self.assertRaises(Exception) as cm:
            self.client(opener).get("/funds")
        self.assertIn("1010", str(cm.exception))

    def test_raw_returns_bytes_for_the_csv_master(self):
        def opener(req, timeout=None):
            return FakeResp(b"EXCH,SEGMENT\nNSE,E\n")

        out = self.client(opener).get("/market/instruments",
                                      params={"source": "equity"}, raw=True)
        self.assertIsInstance(out, bytes)


EQUITY_CSV = """EXCH,SEGMENT,SECURITY_ID,INSTRUMENT_NAME,EXPIRY_CODE,TRADING_SYMBOL,LOT_UNITS,CUSTOM_SYMBOL,EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SERIES,SYMBOL_NAME,UNDERLYING_SCRIP_NAME,FREEZE_QTY,UPPER_LIMIT,LOWER_LIMIT,ISIN,GENERAL_FACTOR,DELIVERY_UNIT,INTRADAY_LEVERAGE,HAIRCUT_PERCENTAGE,PLEDGE_ELIGIBLE
NSE,E,2885,EQUITY,0,RELIANCE,1,Reliance Industries,,,,10,,ES,EQ,RELIANCE,,100000,1500.00,1200.00,INE002A01018,0,,5,20,true
NSE,E,11536,EQUITY,0,TCS,1,Tata Consultancy,,,,10,,ES,EQ,TCS,,50000,4000.00,3200.00,INE467B01029,0,,5,20,true
BSE,E,500325,EQUITY,0,RELIANCE,1,Reliance Industries,,,,10,,ES,EQ,RELIANCE,,100000,1500.00,1200.00,INE002A01018,0,,5,20,true
NSE,E,9999,EQUITY,0,PENNYCO,1,Penny Co,,,,1,,ES,BE,PENNYCO,,10000,12.00,8.00,INE999A01011,0,,1,50,false
"""


class TestInstruments(unittest.TestCase):
    def load(self):
        return _i.Instruments(source="equity").index(_i.parse(EQUITY_CSV))

    def test_resolves_symbol_to_security_id(self):
        self.assertEqual(self.load().resolve("TCS").security_id, "11536")

    def test_prefers_nse_over_bse_for_a_dual_listed_name(self):
        self.assertEqual(self.load().resolve("RELIANCE").exchange, "NSE")
        self.assertEqual(self.load().resolve("RELIANCE").security_id, "2885")

    def test_tick_size_is_converted_from_paise_to_rupees(self):
        inst = self.load()
        # Master says 10 paise; RELIANCE does not move in ten-rupee steps.
        self.assertEqual(inst.resolve("RELIANCE").tick_size, D("0.10"))
        self.assertEqual(inst.resolve("PENNYCO").tick_size, D("0.01"))

    def test_implausible_tick_raises_rather_than_pricing_off_it(self):
        bad = EQUITY_CSV.replace(",10,,ES,EQ,TCS", ",99999999,,ES,EQ,TCS")
        with self.assertRaises(ValueError) as cm:
            _i.parse(bad)
        self.assertIn("implausible tick", str(cm.exception))

    def test_strips_yahoo_style_suffixes(self):
        self.assertEqual(self.load().resolve("RELIANCE.NS").security_id, "2885")

    def test_series_distinguishes_eq_from_be(self):
        inst = self.load()
        self.assertTrue(inst.resolve("TCS").is_equity_eq)
        self.assertFalse(inst.resolve("PENNYCO").is_equity_eq)

    def test_freeze_qty_and_lot_units_parsed(self):
        r = self.load().resolve("TCS")
        self.assertEqual(r.freeze_qty, 50000)
        self.assertEqual(r.lot_units, 1)

    def test_unknown_symbol_names_itself(self):
        with self.assertRaises(KeyError) as cm:
            self.load().resolve("NOTREAL")
        self.assertIn("NOTREAL", str(cm.exception))

    def test_membership(self):
        inst = self.load()
        self.assertIn("TCS", inst)
        self.assertNotIn("NOTREAL", inst)

    def test_missing_tick_falls_back_rather_than_crashing(self):
        bad = EQUITY_CSV.replace(",10,,ES,EQ,TCS", ",,,ES,EQ,TCS")
        rows = _i.parse(bad)
        tcs = [r for r in rows if r.trading_symbol == "TCS"][0]
        self.assertEqual(tcs.tick_size, D("0.05"))

    def test_cache_is_written_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            class C:
                def get(self, path, params=None, raw=False):
                    calls.append(path)
                    return EQUITY_CSV.encode()

            mk = lambda: _i.Instruments(client=C(), source="equity",
                                        cache_dir=Path(tmp), today=lambda: "2026-10-30")
            self.assertEqual(mk().load().resolve("TCS").security_id, "11536")
            self.assertEqual(mk().load().resolve("TCS").security_id, "11536")
            self.assertEqual(len(calls), 1, "second load must come from cache")

    def test_stale_day_cache_is_removed_on_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "instruments-equity-2026-10-29.csv"
            old.write_text("stale", encoding="utf-8")

            class C:
                def get(self, path, params=None, raw=False):
                    return EQUITY_CSV.encode()

            _i.Instruments(client=C(), source="equity", cache_dir=Path(tmp),
                           today=lambda: "2026-10-30").load()
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
