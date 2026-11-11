"""The strategy book: winners shown, losers dropped, a losing setup's
winning fade shown in its place, re-decided from weeks of data."""

import os
import sys
import unittest
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402

import strategy_book as sb  # noqa: E402


def r(setup, side, net):
    return {"setup": setup, "side": side, "net": D(str(net))}


class Build(unittest.TestCase):
    def test_statuses(self):
        rows = ([r("orb", "BUY", 50)] * 6 + [r("orb", "BUY", -20)] * 4 +          # winner
                [r("orb", "SELL", -30)] * 6 + [r("orb", "SELL", 20)] * 4 +        # loser
                [r("orb_fade", "BUY", 30)] * 6 + [r("orb_fade", "BUY", -20)] * 4 +  # fade of the short wins
                [r("day_break", "BUY", 90)] * 3)                                    # too few trades
        v = sb.build(rows, 20)["variants"]
        self.assertEqual(v["orb long"]["status"], "active")
        self.assertEqual(v["orb short"]["status"], "off")
        self.assertEqual(v["orb_fade long"]["status"], "active")
        self.assertEqual(v["day_break long"]["status"], "off")

    def test_a_setup_and_its_fade_are_never_both_active(self):
        rows = [r("orb", "BUY", 40)] * 10 + [r("orb_fade", "SELL", 60)] * 10
        v = sb.build(rows, 20)["variants"]
        self.assertEqual((v["orb long"]["status"], v["orb_fade short"]["status"]), ("off", "active"))


class Apply(unittest.TestCase):
    S = {"setup": "orb", "side": "SELL", "order": "stop", "entry": "99.99", "stop": "100.50",
         "target": "98.97", "why": "break below 100"}

    def test_no_book_shows_everything_untested(self):
        self.assertEqual(sb.apply([self.S], D("0.01"), None)[0]["edge"], "untested")

    def test_active_setup_shown_with_its_edge(self):
        book = {"variants": {"orb short": {"status": "active", "trades": 12, "win_pct": 50, "net": "300", "pf": "1.4"}}}
        out = sb.apply([self.S], D("0.01"), book)
        self.assertEqual((out[0]["setup"], out[0]["side"]), ("orb", "SELL"))
        self.assertIn("PF 1.4", out[0]["edge"])

    def test_a_losing_setup_is_replaced_by_its_winning_fade(self):
        book = {"variants": {"orb short": {"status": "off"},
                             "orb_fade long": {"status": "active", "trades": 10, "win_pct": 45, "net": "120", "pf": "1.3"}}}
        out = sb.apply([self.S], D("0.01"), book)
        f = out[0]
        self.assertEqual((f["setup"], f["side"], f["order"], f["entry"], f["stop"], f["target"]),
                         ("orb_fade", "BUY", "limit", "99.99", "99.48", "101.01"))

    def test_neither_earning_means_not_shown(self):
        self.assertEqual(sb.apply([self.S], D("0.01"), {"variants": {"orb short": {"status": "off"}}}), [])


if __name__ == "__main__":
    unittest.main()
