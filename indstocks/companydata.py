"""
Company data from free, official exchange sources (NSE's public site), for the
AI's context and the Desk: today's bulk and block deals, the latest quarterly
results, and the shareholding pattern (promoter / FII / DII / public) of every
stock on the tape. Pulled a few times a day, not per scan.

Not used, and why (owner's list, 24 Nov): SEC EDGAR and WhaleWisdom cover US
companies only; Yahoo Finance's data needs no second source next to Shoonya's
live feed; Screener.in and Trendlyne do not permit copying their data into
another database, so they stay tools for the owner's own reading.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PROBE = ("snapshot-capital-market-largedeal",
         "corporates-financial-results?index=equities&period=Quarterly",
         "corporate-share-holdings-master?index=equities&symbol=HDFCBANK")


def probe() -> None:
    import sentiment
    for api in PROBE:
        try:
            d = sentiment.nse_json(api)
        except Exception as e:                           # noqa: BLE001
            print(api, "ERROR", str(e)[:200])
            continue
        if isinstance(d, dict):
            print(api, "dict keys:", list(d)[:12])
            v = next((x for x in d.values() if isinstance(x, list) and x), None)
            print("  row:", json.dumps(v[0])[:600] if v else json.dumps(d)[:600])
        elif isinstance(d, list):
            print(api, "list", len(d))
            print("  keys:", sorted(d[0]) if d else "empty")
            hit = [x for x in d if str(x.get("symbol", "")).upper() in ("HDFCBANK", "RELIANCE", "TCS")][:2]
            print("  ours:", json.dumps(hit)[:700])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        probe()
