"""Sentiment: collected best-effort, reduced to bounded numbers, and only
ever restricts or shrinks a trade."""

import json
import os
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, SENSEX, FakeBroker, opening, reset  # noqa: E402

import autotrader  # noqa: E402
import brain  # noqa: E402
import common  # noqa: E402
import sentiment  # noqa: E402
import session as sess  # noqa: E402

DAY = date(2026, 11, 11)
AT = datetime(2026, 11, 11, 10, 0, tzinfo=sess.IST)

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Sensex tanks 800 pts as FIIs dump banks</title><pubDate>Wed, 11 Nov 2026 03:50:00 GMT</pubDate></item>
<item><title>Old news from last week</title><pubDate>Wed, 04 Nov 2026 03:50:00 GMT</pubDate></item>
<item><title>RBI keeps rates &amp; stance</title><pubDate>Wed, 11 Nov 2026 04:10:00 GMT</pubDate></item>
</channel></rss>"""


class Chain:
    """Shoonya call()/search() for VIX, Nifty and a small option chain."""
    def __init__(self):
        self.calls = []

    def search(self, text, exch="NSE"):
        return [{"tsym": "NIFTY24NOV26C25000"}, {"tsym": "NIFTY17NOV26P25000"},
                {"tsym": "NIFTY10NOV26C25000"}, {"tsym": "BANKNIFTY17NOV26C52000"}]

    def call(self, ep, payload, **kw):
        self.calls.append((ep, payload))
        if ep == "GetQuotes" and payload["token"] == "26017":
            return {"lp": "16.5", "c": "15.0"}
        if ep == "GetQuotes" and payload["token"] == "26000":
            return {"lp": "25010", "c": "25200"}
        if ep == "GetOptionChain":
            return {"values": [{"token": "1", "optt": "CE", "strprc": "25000"},
                               {"token": "2", "optt": "CE", "strprc": "25100"},
                               {"token": "3", "optt": "PE", "strprc": "25000"},
                               {"token": "4", "optt": "PE", "strprc": "24900"}]}
        oi = {"1": ("900", "800"), "2": ("1500", "1000"), "3": ("700", "700"), "4": ("400", "300")}[payload["token"]]
        return {"oi": oi[0], "poi": oi[1]}


class Collect(unittest.TestCase):
    def test_nearest_unexpired_nifty_option(self):
        rows = Chain().search("NIFTY")
        self.assertEqual(sentiment.nearest_nifty_option(rows, DAY), "NIFTY17NOV26P25000")

    def test_positioning(self):
        b = Chain()
        p = sentiment.positioning(b, DAY)
        self.assertEqual(p["vix"]["change_pct"], 10.0)
        o = p["options"]
        self.assertEqual((o["pcr"], o["call_wall"], o["put_wall"], o["atm"]), (0.46, 25100.0, 25000.0, 25000))
        chain = next(x for x in b.calls if x[0] == "GetOptionChain")[1]
        self.assertEqual(chain["tsym"], "NIFTY17NOV26P25000")

    def test_rss_keeps_recent_and_unescapes(self):
        items = sentiment.parse_rss("ET", RSS, datetime(2026, 11, 10, 18, 0, tzinfo=sess.IST))
        self.assertEqual([i["h"] for i in items], ["Sensex tanks 800 pts as FIIs dump banks", "RBI keeps rates & stance"])

    def test_flows(self):
        f = sentiment.flows(lambda api: [{"category": "FII/FPI", "netValue": "-2,345.6", "date": "10-Nov-2026"},
                                         {"category": "DII", "netValue": "1800", "date": "10-Nov-2026"}])
        self.assertEqual((f["fii"]["net_cr"], f["dii"]["net_cr"]), (-2345.6, 1800.0))

    def test_unreachable_sources_do_not_fail(self):
        def boom(*a, **k):
            raise OSError("blocked")
        self.assertIn("error", sentiment.flows(boom))
        self.assertIn("error", sentiment.calendar(DAY, ["SBIN"], boom))

    def test_calendar_results_and_expiry(self):
        ev = sentiment.calendar(date(2026, 11, 24), ["SBIN", "INFY"],
                                lambda api: [{"symbol": "SBIN", "date": "24-Nov-2026", "purpose": "Financial Results"},
                                             {"symbol": "TCS", "date": "24-Nov-2026", "purpose": "Financial Results"}])
        self.assertTrue(ev["stocks"]["SBIN"]["results"])
        self.assertNotIn("TCS", ev["stocks"])
        self.assertIn("Nifty monthly expiry", [e["what"] for e in ev["market"]])

    def test_rbi_day_has_a_window(self):
        ev = sentiment.calendar(date(2026, 12, 4), [], lambda api: [])
        self.assertTrue(any(e["window"] and e["at"] == "10:00" for e in ev["market"]))


class Clean(unittest.TestCase):
    def test_reply_is_bounded_and_filtered(self):
        out = sentiment.clean_news({"market": {"score": -7, "summary": "x" * 999},
                                    "stocks": {"sbin": {"score": 3, "material": 1, "why": "y"},
                                               "EVIL": {"score": 1}}}, ["SBIN"])
        self.assertEqual(out["market"]["score"], -1.0)
        self.assertEqual(len(out["market"]["summary"]), 240)
        self.assertEqual(list(out["stocks"]), ["SBIN"])
        self.assertEqual(out["stocks"]["SBIN"]["score"], 1.0)


def snap(**kw):
    s = {"ts": AT.isoformat(), "day": DAY.isoformat(),
         "positioning": {"vix": {"level": 14.0, "change_pct": 1.0}, "options": {"pcr": 1.0}},
         "flows": {"fii": {"net_cr": 0}}, "events": {"market": [], "stocks": {}},
         "news": {"market": {"score": 0.0}, "stocks": {}}}
    for k, v in kw.items():
        s[k] = v
    s["tilt"] = sentiment.tilt(s)
    return s


class Tilt(unittest.TestCase):
    def test_bias_weights(self):
        s = snap(news={"market": {"score": -1.0}, "stocks": {}}, flows={"fii": {"net_cr": -3000}},
                 positioning={"options": {"pcr": 0.6}})
        self.assertEqual(s["tilt"]["bias"], -1.0)

    def test_results_day_blocks_both_sides(self):
        s = snap(events={"market": [], "stocks": {"SBIN": {"what": "Financial Results", "results": True}}})
        self.assertIn("results day", sentiment.check("BUY", "SBIN", s, AT)[0])
        self.assertIn("results day", sentiment.check("SELL", "SBIN", s, AT)[0])

    def test_material_bad_news_blocks_longs_only(self):
        s = snap(news={"market": {"score": 0.0}, "stocks": {"INFY": {"score": -0.8, "material": True, "why": "guidance cut"}}})
        self.assertIn("guidance cut", sentiment.check("BUY", "INFY", s, AT)[0])
        self.assertIsNone(sentiment.check("SELL", "INFY", s, AT)[0])

    def test_half_size_against_bias_full_with_it(self):
        s = snap(news={"market": {"score": -0.8}, "stocks": {}})
        self.assertEqual(sentiment.check("BUY", "X", s, AT)[1], 0.5)
        self.assertEqual(sentiment.check("SELL", "X", s, AT)[1], 1.0)

    def test_high_vix_shrinks(self):
        s = snap(positioning={"vix": {"level": 22.0, "change_pct": 3.0}})
        self.assertEqual(sentiment.check("BUY", "X", s, AT)[1], 0.75)

    def test_macro_window(self):
        s = snap(events={"market": [{"what": "RBI policy decision", "at": "10:00", "window": True}], "stocks": {}})
        self.assertIn("RBI", sentiment.check("BUY", "X", s, AT)[0])
        self.assertIsNone(sentiment.check("BUY", "X", s, AT.replace(hour=11))[0])

    def test_forming_follows_a_clear_bias(self):
        s = snap(news={"market": {"score": -0.9}, "stocks": {}}, flows={})
        self.assertIsNotNone(sentiment.check("BUY", "X", s, AT, "FORMING")[0])
        self.assertIsNone(sentiment.check("SELL", "X", s, AT, "FORMING")[0])
        self.assertIsNone(sentiment.check("BUY", "X", s, AT, "MIXED")[0])

    def test_no_snapshot_no_effect(self):
        self.assertEqual(sentiment.check("BUY", "X", None, AT)[:2], (None, 1.0))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0")
        sess.now = lambda clock=None: AT
        brain.find_claude = lambda: "/x/claude"

    def collect(self, reply):
        def fetch(url, **k):
            return RSS
        nse = lambda api: [] if "event" in api or "announce" in api else (_ for _ in ()).throw(OSError("x"))
        return sentiment.run(Chain(), AT, news_run=lambda p: json.dumps(reply), fetch_fn=fetch, nse=nse)

    def test_run_writes_a_fresh_snapshot_and_reuses_news(self):
        s = self.collect({"market": {"score": -0.5, "summary": "risk-off"}, "stocks": {}})
        self.assertEqual(s["news"]["market"]["summary"], "risk-off")
        self.assertEqual(sentiment.load(AT)["tilt"]["bias"], s["tilt"]["bias"])
        again = sentiment.run(Chain(), AT, news_run=lambda p: (_ for _ in ()).throw(AssertionError("re-read")),
                              fetch_fn=lambda *a, **k: RSS, nse=lambda api: [])
        self.assertEqual(again["news"]["market"]["summary"], "risk-off")
        self.assertIsNone(sentiment.load(AT.replace(hour=13)), "stale after 90 minutes")

    def test_headlines_are_framed_as_untrusted(self):
        seen = {}
        def run(p):
            seen["p"] = p
            return "{}"
        sentiment.run(Chain(), AT, news_run=run, fetch_fn=lambda *a, **k: RSS, nse=lambda api: [])
        self.assertIn("untrusted", seen["p"])
        self.assertIn("Sensex tanks", seen["p"])

    def enter(self, side, entry, stop, target):
        reply = json.dumps({"view": "", "decisions": [{"action": "enter", "symbol": "NIFTYBEES", "side": side,
                                                       "order": "stop", "entry": entry, "stop": stop,
                                                       "target": target, "size": 1.0, "reason": "t"}]})
        return lambda *a, **k: brain.decide(*a, **k, run=lambda p: reply)

    def broker(self):
        return FakeBroker(candles={NIFTY.token: opening(DAY), SENSEX.token: opening(DAY, "900.00", "896.00")},
                          quotes={NIFTY.token: {"ltp": "280.50", "uc": "308", "lc": "252"},
                                  SENSEX.token: {"ltp": "898.00", "uc": "990", "lc": "810"}})

    def test_bad_news_refuses_the_long(self):
        self.collect({"market": {"score": 0}, "stocks": {"NIFTYBEES": {"score": -0.9, "material": True, "why": "crash"}}})
        b = self.broker()
        autotrader.run(b, now=AT, decide=self.enter("BUY", 281.05, 280.30, 282.80))
        self.assertEqual(b.placed, [])
        self.assertIn("crash", common.events("refused", DAY.isoformat())[-1]["reason"])

    def test_against_sentiment_is_half_size_and_recorded(self):
        b1 = self.broker()
        autotrader.run(b1, now=AT, decide=self.enter("BUY", 281.05, 280.30, 282.80))
        full = b1.placed[0]["qty"]
        reset(TRADER_DECIDER="claude", TRADER_MAX_LEVERAGE="2.0")
        self.collect({"market": {"score": -1.0}, "stocks": {}})
        b2 = self.broker()
        autotrader.run(b2, now=AT, decide=self.enter("BUY", 281.05, 280.30, 282.80))
        self.assertLess(b2.placed[0]["qty"], full)
        rec = common.events("placed", DAY.isoformat())[-1]
        self.assertIn("half size", rec["sentiment_note"])


if __name__ == "__main__":
    unittest.main()


class NseChain(unittest.TestCase):
    def chain(self):
        rows = []
        for k, ce, pe in ((24900, 100, 900), (25000, 500, 700), (25100, 1200, 200), (26000, 99999, 1)):
            rows.append({"strikePrice": k, "expiryDate": "17-Nov-2026",
                         "CE": {"openInterest": ce, "changeinOpenInterest": 10},
                         "PE": {"openInterest": pe, "changeinOpenInterest": 5}})
        return {"records": {"underlyingValue": 25010, "data": rows}}

    def test_v3_with_contract_info(self):
        def get(api):
            if api.startswith("option-chain-contract-info"):
                return {"expiryDates": ["10-Nov-2026", "17-Nov-2026", "24-Nov-2026"]}
            self.assertIn("expiry=17-Nov-2026", api)
            return self.chain()
        o = sentiment.options_from_nse(get, date(2026, 11, 11), width=1)
        self.assertEqual((o["atm"], o["pcr"], o["call_wall"], o["put_wall"]), (25000.0, 1.0, 25100.0, 24900.0))

    def test_falls_back_to_the_legacy_endpoint(self):
        def get(api):
            if api.startswith("option-chain-indices"):
                return self.chain()
            raise OSError("404")
        self.assertEqual(sentiment.options_from_nse(get, date(2026, 11, 11), width=1)["expiry"], "17-Nov-2026")

    def test_positioning_uses_nse_when_shoonya_has_no_fo(self):
        class NoFO(Chain):
            def search(self, text, exch="NSE"):
                return []
        def get(api):
            if api.startswith("option-chain-indices"):
                return self.chain()
            raise OSError("x")
        p = sentiment.positioning(NoFO(), date(2026, 11, 11), get)
        self.assertEqual(p["options"]["source"], "NSE")
        self.assertNotIn("options_error", p)
