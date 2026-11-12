"""Real-time news: match, dedupe, score, alert, start a scan."""

import json
import os
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import reset  # noqa: E402

import common  # noqa: E402
import newswatch  # noqa: E402
import session as sess  # noqa: E402

AT = datetime(2026, 11, 12, 13, 58, tzinfo=sess.IST)


def rss(*titles):
    from xml.sax.saxutils import escape
    items = "".join(f"<item><title>{escape(t)}</title><pubDate>Thu, 12 Nov 2026 08:2{i}:00 GMT</pubDate></item>"
                    for i, t in enumerate(titles))
    return f'<?xml version="1.0"?><rss><channel>{items}</channel></rss>'.encode()


class Match(unittest.TestCase):
    def test_aliases(self):
        syms = ["LT", "SBIN", "RELIANCE", "INFY"]
        self.assertEqual(newswatch.match("Karnataka blacklists L&T from state projects", syms)["symbols"], ["LT"])
        self.assertEqual(newswatch.match("Larsen & Toubro shares slump", syms)["symbols"], ["LT"])
        self.assertEqual(newswatch.match("SBI raises lending rates", syms)["symbols"], ["SBIN"])
        self.assertEqual(newswatch.match("RIL to demerge retail", syms)["symbols"], ["RELIANCE"])
        self.assertTrue(newswatch.match("RBI holds repo rate", syms)["macro"])
        self.assertEqual(newswatch.match("Weather turns pleasant in Delhi", syms), {"symbols": [], "macro": False})


class Cycle(unittest.TestCase):
    def setUp(self):
        reset(TRADER_SYMBOLS="LT,SBIN,NIFTYBEES")
        sess.now = lambda clock=None: AT

    def run_cycle(self, titles, reply, nse=()):
        calls = {"n": 0}
        def run(prompt):
            calls["n"] += 1
            calls["prompt"] = prompt
            return json.dumps(reply)
        out = newswatch.cycle(AT, fetch=lambda url, **k: rss(*titles), get=lambda api: list(nse), run=run, state={})
        return out, calls

    def test_first_sweep_is_backlog_then_fresh_material_news_alerts(self):
        self.run_cycle(["Nifty opens flat"], {"items": []})                    # first sweep: backlog only
        out, calls = self.run_cycle(["Nifty opens flat", "Karnataka blacklists L&T from state contracts"],
                                    {"items": [{"i": 0, "symbols": ["LT"], "score": -0.9, "material": True,
                                                "why": "state blacklisting"}]})
        self.assertEqual(out["relevant"], 1, "only the new headline is scored")
        self.assertIn("untrusted", calls["prompt"])
        a = common.events("news_alert")
        self.assertEqual((a[0]["symbol"], a[0]["score"]), ("LT", -0.9))

    def test_commentary_is_not_an_alert(self):
        self.run_cycle(["x"], {"items": []})
        self.run_cycle(["x", "L&T among stocks to watch today"],
                       {"items": [{"i": 0, "symbols": ["LT"], "score": -0.2, "material": False, "why": "watchlist"}]})
        self.assertEqual(common.events("news_alert"), [])

    def test_nse_filings_are_matched_by_symbol(self):
        self.run_cycle(["x"], {"items": []})
        out, _ = self.run_cycle(["x"], {"items": []},
                                nse=[{"symbol": "SBIN", "desc": "Outcome of Board Meeting", "attchmntText": "results", "an_dt": "12-Nov-2026 13:57:00"},
                                     {"symbol": "TCS", "desc": "x", "attchmntText": "y", "an_dt": "12-Nov-2026 13:57:00"}])
        self.assertEqual(out["relevant"], 1)

    def test_bad_scores_are_bounded(self):
        self.run_cycle(["x"], {"items": []})
        self.run_cycle(["x", "SBI shock"], {"items": [{"i": 0, "symbols": ["SBIN", "EVIL"], "score": -9, "material": True, "why": "w"}]})
        a = common.events("news_alert")
        self.assertEqual((a[0]["symbol"], a[0]["score"]), ("SBIN", -1.0))


if __name__ == "__main__":
    unittest.main()
