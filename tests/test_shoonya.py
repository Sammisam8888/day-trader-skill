"""The Shoonya adapter: login, request format, envelope handling, order
payloads, normalisation. All offline against a scripted HTTP function."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fake import NIFTY  # noqa: E402  (sets the sandbox first)
TMP = tempfile.mkdtemp()
os.environ["SHOONYA_TOKEN_FILE"] = os.path.join(TMP, "tok.json")
os.environ["SHOONYA_INSTR_CACHE"] = os.path.join(TMP, "instr.json")
import shoonya as sh  # noqa: E402
import session as sess  # noqa: E402

sh.TOKEN_FILE = sh.Path(os.environ["SHOONYA_TOKEN_FILE"])
sh.INSTR_CACHE = sh.Path(os.environ["SHOONYA_INSTR_CACHE"])
IST = sess.IST


class Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, body, token):
        self.calls.append((endpoint, body, token))
        r = self.responses.pop(0) if self.responses else {"stat": "Ok"}
        return r(endpoint, body) if callable(r) else r


def broker(http):
    return sh.Shoonya(token={"token": "TOK", "uid": "FN245398", "actid": "FN245398"},
                      http=http, sleep=lambda s: None)


class TestLogin(unittest.TestCase):
    def setUp(self):
        import common
        common.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        common.ENV_FILE.write_text("SHOONYA_CLIENT_ID=FN245398_U\nSHOONYA_SECRET_CODE=SECRET\nSHOONYA_USER_ID=FN245398\n")
        sh.clear_token()

    def test_checksum_is_sha256_of_client_secret_code(self):
        self.assertEqual(sh.checksum("A", "B", "C"), hashlib.sha256(b"ABC").hexdigest())

    def test_authorize_url_carries_the_client_id(self):
        self.assertEqual(sh.authorize_url(),
                         "https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id=FN245398_U")

    def test_code_is_extracted_from_a_pasted_redirect(self):
        self.assertEqual(sh.extract_code("https://x/cb?code=abc-123&state=1"), "abc-123")
        self.assertEqual(sh.extract_code("  abc-123 "), "abc-123")

    def test_exchange_sends_code_and_checksum_and_saves_the_token(self):
        http = Http({"stat": "Ok", "susertoken": "S1", "uid": "FN245398", "actid": "FN245398"})
        rec = sh.exchange_code("https://x/?code=CODE1", http=http)
        endpoint, body, token = http.calls[0]
        self.assertEqual(endpoint, "GenAcsTok")
        self.assertIsNone(token)
        sent = json.loads(body[len("jData="):])
        self.assertEqual(sent, {"code": "CODE1", "checksum": sh.checksum("FN245398_U", "SECRET", "CODE1")})
        self.assertEqual(rec["token"], "S1")
        self.assertEqual(sh.load_token()["token"], "S1")
        self.assertEqual(oct(os.stat(sh.TOKEN_FILE).st_mode & 0o777), "0o600")

    def test_failed_exchange_raises_with_the_broker_message(self):
        with self.assertRaisesRegex(sh.ShoonyaError, "Invalid Checksum"):
            sh.exchange_code("C", http=Http({"stat": "Not_Ok", "emsg": "Invalid Checksum"}))

    def test_token_from_before_todays_settlement_is_stale(self):
        sh.save_token("S", "U", "U")
        rec = json.loads(sh.TOKEN_FILE.read_text())
        rec["at"] = datetime(2026, 11, 7, 20, 0, tzinfo=IST).isoformat()
        sh.TOKEN_FILE.write_text(json.dumps(rec))
        self.assertIsNone(sh.load_token(datetime(2026, 11, 8, 9, 0, tzinfo=IST)))
        rec["at"] = datetime(2026, 11, 8, 8, 0, tzinfo=IST).isoformat()
        sh.TOKEN_FILE.write_text(json.dumps(rec))
        self.assertIsNotNone(sh.load_token(datetime(2026, 11, 8, 9, 0, tzinfo=IST)))

    def test_no_token_means_login_required(self):
        with self.assertRaises(sh.LoginRequired):
            sh.Shoonya(http=Http()).positions()


class TestRequests(unittest.TestCase):
    def test_body_is_jdata_and_jkey_and_the_token_is_also_bearer(self):
        http = Http([])
        broker(http).positions()
        endpoint, body, token = http.calls[0]
        self.assertEqual(endpoint, "PositionBook")
        self.assertTrue(body.startswith('jData={"uid":"FN245398","actid":"FN245398"}'))
        self.assertTrue(body.endswith("&jKey=TOK"))
        self.assertEqual(token, "TOK")

    def test_session_expired_clears_the_token_and_asks_for_login(self):
        sh.save_token("S", "U", "U")
        with self.assertRaises(sh.LoginRequired):
            broker(Http({"stat": "Not_Ok", "emsg": "Session Expired :  Invalid Session Key"})).orders()
        self.assertIsNone(sh.load_token())

    def test_empty_book_is_not_an_error(self):
        self.assertEqual(broker(Http({"stat": "Not_Ok", "emsg": "Error Occurred : 5 \"no data\""})).orders(), [])

    def test_rate_limit_is_retried(self):
        http = Http({"stat": "Not_Ok", "emsg": "Rate_Limited: too many requests, retry after backoff"}, [])
        self.assertEqual(broker(http).orders(), [])
        self.assertEqual(len(http.calls), 2)

    def test_other_errors_raise(self):
        with self.assertRaisesRegex(sh.ShoonyaError, "RMS:Margin Exceeds"):
            broker(Http({"stat": "Not_Ok", "emsg": "RMS:Margin Exceeds"})).place(
                NIFTY, side="B", qty=1, price=D("280"))


class TestOrders(unittest.TestCase):
    def sent(self, http):
        return json.loads(http.calls[-1][1].split("&jKey=")[0][len("jData="):])

    def test_trigger_entry_is_an_intraday_sl_lmt_buy(self):
        http = Http({"stat": "Ok", "norenordno": "24110800001"})
        oid = broker(http).place(NIFTY, side="B", qty=40, price=D("281.03"), trigger=D("281.01"),
                                 remarks="auto/NIFTYBEES")
        p = self.sent(http)
        self.assertEqual(oid, "24110800001")
        self.assertEqual(http.calls[-1][0], "PlaceOrder")
        self.assertEqual((p["prd"], p["prctyp"], p["trantype"], p["ret"], p["ordersource"]),
                         ("I", "SL-LMT", "B", "DAY", "API"))
        self.assertEqual((p["prc"], p["trgprc"], p["qty"], p["tsym"]), ("281.03", "281.01", "40", "NIFTYBEES-EQ"))

    def test_there_is_no_market_order(self):
        http = Http({"stat": "Ok", "norenordno": "1"})
        broker(http).place(NIFTY, side="S", qty=1, price=D("279.5"))
        self.assertEqual(self.sent(http)["prctyp"], "LMT")
        self.assertNotIn("trgprc", self.sent(http))

    def test_algo_id_is_attached_when_configured(self):
        import common
        common.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        common.ENV_FILE.write_text("SHOONYA_ALGO_ID=ALGO42\n")
        http = Http({"stat": "Ok", "norenordno": "1"})
        broker(http).place(NIFTY, side="B", qty=1, price=D("280"))
        self.assertEqual(self.sent(http)["algo_id"], "ALGO42")
        common.ENV_FILE.write_text("")

    def test_prices_are_snapped_to_tick(self):
        http = Http({"stat": "Ok", "norenordno": "1"})
        broker(http).place(NIFTY, side="B", qty=1, price=D("280.004"))
        self.assertEqual(self.sent(http)["prc"], "280.00")

    def test_cancel_and_modify_payloads(self):
        http = Http({"stat": "Ok", "result": "1"}, {"stat": "Ok", "result": "1"})
        b = broker(http)
        b.cancel("777")
        self.assertEqual(http.calls[-1][0], "CancelOrder")
        self.assertEqual(self.sent(http)["norenordno"], "777")
        b.modify({"id": "777"}, NIFTY, qty=40, price=D("279.50"), trigger=D("279.60"))
        p = self.sent(http)
        self.assertEqual(http.calls[-1][0], "ModifyOrder")
        self.assertEqual((p["prctyp"], p["trgprc"], p["prc"], p["qty"]), ("SL-LMT", "279.60", "279.50", "40"))

    def test_place_without_order_number_raises(self):
        with self.assertRaises(sh.ShoonyaError):
            broker(Http({"stat": "Ok"})).place(NIFTY, side="B", qty=1, price=D("280"))

    def test_quantity_must_be_positive(self):
        with self.assertRaises(sh.ShoonyaError):
            broker(Http()).place(NIFTY, side="B", qty=0, price=D("280"))


class TestNormalisation(unittest.TestCase):
    def test_order_row(self):
        o = sh.norm_order({"norenordno": "5", "tsym": "NIFTYBEES-EQ", "token": "10576", "trantype": "B",
                           "prctyp": "SL-LMT", "prd": "I", "status": "TRIGGER_PENDING", "qty": "40",
                           "fillshares": "0", "prc": "281.03", "trgprc": "281.01", "remarks": "auto/NIFTYBEES"})
        self.assertEqual((o["symbol"], o["pending"], o["dead"], o["qty"], o["trigger"]),
                         ("NIFTYBEES", True, False, 40, D("281.01")))
        self.assertTrue(sh.norm_order({"status": "CANCELED"})["dead"])
        self.assertFalse(sh.norm_order({"status": "COMPLETE"})["pending"])

    def test_position_row(self):
        p = sh.norm_position({"tsym": "NIFTYBEES-EQ", "token": "10576", "prd": "I", "netqty": "40",
                              "netavgprc": "281.02", "lp": "282.10", "rpnl": "0", "urmtom": "43.20",
                              "daybuyqty": "40", "daybuyavgprc": "281.02", "ti": "0.01"})
        self.assertEqual((p["qty"], p["avg"], p["unrealised"], p["tick"]), (40, D("281.02"), D("43.20"), D("0.01")))

    def test_intraday_filter(self):
        rows = [{"tsym": "A-EQ", "prd": "I", "netqty": "1"}, {"tsym": "B-EQ", "prd": "C", "netqty": "1"}]
        b = broker(Http(rows))
        self.assertEqual([p["symbol"] for p in b.positions()], ["A"])

    def test_candles_are_parsed_and_sorted_oldest_first(self):
        rows = [{"stat": "Ok", "time": "08-11-2026 14:25:00", "into": "281", "inth": "281.5", "intl": "280.9",
                 "intc": "281.4", "intv": "1000"},
                {"stat": "Ok", "time": "08-11-2026 14:20:00", "into": "280.5", "inth": "281.1", "intl": "280.4",
                 "intc": "281", "intv": "2000"}]
        cs = sh.parse_candles(rows, 5)
        self.assertEqual([c.opens_at.minute for c in cs], [20, 25])
        self.assertEqual(cs[0].high, D("281.1"))
        self.assertEqual(cs[0].opens_at.tzinfo, IST)

    def test_instrument_prefers_the_eq_series(self):
        http = Http({"stat": "Ok", "values": [
            {"tsym": "NIFTYBEES-BL", "token": "1", "ti": "0.01", "ls": "1"},
            {"tsym": "NIFTYBEES-EQ", "token": "10576", "ti": "0.01", "ls": "1"}]})
        sh.INSTR_CACHE.unlink(missing_ok=True)
        i = broker(http).instrument("niftybees")
        self.assertEqual((i.tsym, i.token, i.tick), ("NIFTYBEES-EQ", "10576", D("0.01")))
        self.assertEqual(broker(Http()).instrument("NIFTYBEES").token, "10576", "cached for the day")

    def test_marketable_sell_is_under_the_bid_and_inside_the_band(self):
        b = broker(Http())
        q = {"ltp": D("280"), "bid": D("279.98"), "ask": D("280.02"), "uc": D("308"), "lc": D("279.50")}
        px = b.marketable(NIFTY, "S", q)
        self.assertLess(px, D("279.98"))
        self.assertGreaterEqual(px, D("279.50"), "never below the lower circuit")
        self.assertGreater(b.marketable(NIFTY, "B", q), D("280.02"))


if __name__ == "__main__":
    unittest.main()
