"""Company data from NSE filings: deals, results dates, promoter holding, the AI's view."""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import companydata as cd  # noqa: E402
import session as sess  # noqa: E402

NOW = datetime(2026, 11, 24, 8, 40, tzinfo=sess.IST)


def fake_get(api):
    if api.startswith("snapshot-capital-market-largedeal"):
        return {"as_on_date": "23-Nov-2026",
                "BULK_DEALS_DATA": [{"symbol": "TCS", "buySell": "BUY", "clientName": "BIG FUND", "qty": "500000", "watp": "4100"},
                                    {"symbol": "AJMERA", "buySell": "SELL", "clientName": "X", "qty": "1000", "watp": "160"}],
                "BLOCK_DEALS_DATA": []}
    if api.startswith("corporates-financial-results"):
        return [{"symbol": "TCS", "broadCastDate": "09-Oct-2026 18:01:02", "relatingTo": "Second Quarter", "toDate": "30-Sep-2026"},
                {"symbol": "TCS", "broadCastDate": "10-Jul-2026 18:00:00", "relatingTo": "First Quarter", "toDate": "30-Jun-2026"},
                {"symbol": "INFY", "broadCastDate": "20-Nov-2026 16:00:00", "relatingTo": "Second Quarter", "toDate": "30-Sep-2026"}]
    if api.startswith("corporate-share-holdings-master"):
        sym = api.split("symbol=")[1]
        if sym == "INFY":
            return [{"date": "30-JUN-2026", "pr_and_prgrp": "14.6", "public_val": "85.4"},
                    {"date": "30-SEP-2026", "pr_and_prgrp": "13.1", "public_val": "86.9"}]
        return []
    raise KeyError(api)


class CompanyData(unittest.TestCase):
    def setUp(self):
        reset(TRADER_SYMBOLS="NIFTYBEES,TCS,INFY,HDFCBANK")
        sess.now = lambda clock=None: NOW

    def test_refresh_and_the_ais_compact_view(self):
        out = cd.refresh(get=fake_get, now=NOW, pause=0)
        self.assertEqual([x["symbol"] for x in out["deals"]["ours"]], ["TCS"])
        self.assertEqual(out["deals"]["biggest"][0]["value_cr"], 205.0)
        self.assertEqual(out["results"]["TCS"]["relating_to"], "Second Quarter")
        self.assertEqual(out["results"]["INFY"]["days_ago"], 4)
        self.assertEqual(out["holdings"]["INFY"]["promoter_change"], -1.5)
        v = cd.for_prompt()["stocks"]
        self.assertIn("results_filed", v["INFY"])
        self.assertEqual(v["INFY"]["promoter_change_pts"], -1.5)
        self.assertNotIn("results_filed", v.get("TCS", {}), "results 46 days old are not news")
        self.assertIn("big_deals", v["TCS"])

    def test_a_failing_source_does_not_stop_the_rest(self):
        def broken(api):
            if api.startswith("snapshot"):
                raise RuntimeError("nse down")
            return fake_get(api)
        out = cd.refresh(get=broken, now=NOW, pause=0)
        self.assertIsNone(out["deals"])
        self.assertIn("INFY", out["holdings"])
        self.assertTrue(out["errors"])


if __name__ == "__main__":
    unittest.main()
