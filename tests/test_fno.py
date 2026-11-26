"""F&O sleeves: contract choice, charges, sizing, following the stock trade, target, restart."""

import io
import os
import sys
import unittest
import zipfile
from datetime import date, datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402,F401
from _fake import reset  # noqa: E402

import fno  # noqa: E402
import journal  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402

DAY = "2026-11-26"
AT = datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST).timestamp()
MASTER = """Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize,
NFO,1001,500,RELIANCE,RELIANCE27NOV26F,27-NOV-2026,FUTSTK,XX,-1,0.05,
NFO,1002,500,RELIANCE,RELIANCE29DEC26F,29-DEC-2026,FUTSTK,XX,-1,0.05,
NFO,2001,500,RELIANCE,RELIANCE29DEC26C1300,29-DEC-2026,OPTSTK,CE,1300,0.05,
NFO,2002,500,RELIANCE,RELIANCE29DEC26C1320,29-DEC-2026,OPTSTK,CE,1320,0.05,
NFO,2003,500,RELIANCE,RELIANCE29DEC26P1300,29-DEC-2026,OPTSTK,PE,1300,0.05,
NFO,9999,500,RELIANCE,RELIANCE,29-DEC-2026,EQ,XX,-1,0.05,
"""


def zipped(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("NFO_symbols.txt", text)
    return b.getvalue()


class Quotes:
    def __init__(self):
        self.q = {"1002": {"ltp": D("1310"), "bid": D("1309.9"), "ask": D("1310.1")},
                  "2001": {"ltp": D("25"), "bid": D("24.9"), "ask": D("25.1")},
                  "2003": {"ltp": D("20"), "bid": D("19.9"), "ask": D("20.1")}}

    def __call__(self, token):
        return dict(self.q[token])


class Fno(unittest.TestCase):
    def setUp(self):
        reset(TRADER_FNO_CAPITAL="100000", TRADER_BENCH_TARGET="1000", TRADER_FNO_OPT_BUDGET="20000", TRADER_FNO_FUT_MARGIN_PCT="15")
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        rows = fno.load_master(DAY, fetch=lambda: zipped(MASTER))
        self.c = fno.Contracts(rows, date(2026, 11, 26))
        self.q = Quotes()
        self.book = fno.FnoBook(DAY, self.q, self.c)

    def test_contracts_roll_off_an_expiring_month_and_pick_the_atm(self):
        self.assertEqual(self.c.future("RELIANCE")["tsym"], "RELIANCE29DEC26F", "27 Nov expires in 1 day: roll")
        self.assertEqual(self.c.atm_option("RELIANCE", 1304.0, "B")["tsym"], "RELIANCE29DEC26C1300")
        self.assertEqual(self.c.atm_option("RELIANCE", 1304.0, "S")["tsym"], "RELIANCE29DEC26P1300")

    def test_charges_use_the_2026_stt(self):
        c = fno.charges("fut", D("655000"), D("660000"))
        self.assertGreater(c, D("330"))            # 0.05% of 6.6 lakh = 330 of STT alone
        o = fno.charges("opt", D("12500"), D("15000"))
        self.assertGreater(o, D("22.5"))           # 0.15% of 15,000 = 22.5

    def test_a_buy_signal_opens_both_sleeves_and_the_stock_exit_closes_them(self):
        self.book.on_open("orb15", "RELIANCE", "B", 1304.0, AT)
        o = self.book.st["open"]
        self.assertEqual(o["orb15~fut|RELIANCE"]["qty"], 500)
        self.assertEqual(o["orb15~opt|RELIANCE"]["tsym"], "RELIANCE29DEC26C1300")
        self.assertEqual(o["orb15~opt|RELIANCE"]["lots"], 1, "Rs 20,000 budget buys one Rs 12,550 lot")
        self.q.q["1002"].update(bid=D("1316"), ask=D("1316.2"))
        self.q.q["2001"].update(bid=D("29"), ask=D("29.2"))
        self.book.on_close("orb15", "RELIANCE", "target", AT + 600)
        self.assertEqual(self.book.st["open"], {})
        self.assertGreater(D(self.book.st["realised"]["orb15~fut"]), D("2500"))
        self.assertGreater(D(self.book.st["realised"]["orb15~opt"]), D("1900"))
        self.assertIn("RELIANCE29DEC26F", journal.log_file("orb15~fut", DAY).read_text())

    def test_a_futures_lot_beyond_the_wallet_is_skipped_and_logged(self):
        reset(TRADER_FNO_CAPITAL="50000")
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        book = fno.FnoBook(DAY, self.q, self.c)
        book.on_open("orb15", "RELIANCE", "B", 1304.0, AT)
        self.assertNotIn("orb15~fut|RELIANCE", book.st["open"])
        self.assertIn("needs Rs", journal.log_file("orb15~fut", DAY).read_text())

    def test_target_and_flat_and_restart(self):
        self.book.on_open("orb15", "RELIANCE", "S", 1304.0, AT)
        self.q.q["1002"].update(bid=D("1306"), ask=D("1307"))       # short future +1,500ish
        self.book.mark(AT + 60)
        self.assertIn("orb15~fut", self.book.st["done"])
        self.assertIn("orb15~fut|RELIANCE", self.book.st["open"], "the target is a milestone; it keeps trading")
        again = fno.FnoBook(DAY, self.q, self.c)
        self.assertIn("orb15~opt|RELIANCE", again.st["open"], "the book survives a restart")
        again.mark(AT + 120, flat=True)
        self.assertEqual(again.st["open"], {})


class BadQuotes(unittest.TestCase):
    def setUp(self):
        reset(TRADER_FNO_CAPITAL="100000", TRADER_FNO_FUT_MARGIN_PCT="15")
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        self.c = fno.Contracts(fno.load_master(DAY, fetch=lambda: zipped(MASTER)), date(2026, 11, 26))

    def test_a_stale_paise_quote_or_an_empty_book_is_never_bought(self):
        q = Quotes()
        q.q["2001"] = {"ltp": D("0.05"), "bid": D("0"), "ask": D("0.05")}       # 26 Nov: empty book, stale price
        q.q["1002"] = {"ltp": D("1310"), "bid": D("1290"), "ask": D("1330")}    # 3% wide
        b = fno.FnoBook(DAY, q, self.c)
        b.on_open("rsi2", "RELIANCE", "B", 1304.0, AT)
        self.assertEqual(b.st["open"], {})
        log = journal.log_file("rsi2~opt", DAY).read_text()
        self.assertIn("no two-sided book", log)

    def test_option_lots_are_capped(self):
        reset(TRADER_FNO_CAPITAL="100000", TRADER_FNO_OPT_BUDGET="100000", TRADER_FNO_MAX_LOTS="5")
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        q = Quotes()
        q.q["2001"] = {"ltp": D("3"), "bid": D("2.95"), "ask": D("3.0")}
        b = fno.FnoBook(DAY, q, self.c)
        b.on_open("rsi2", "RELIANCE", "B", 1304.0, AT)
        self.assertEqual(b.st["open"]["rsi2~opt|RELIANCE"]["lots"], 5)


class WrongInstrumentQuote(unittest.TestCase):
    def test_a_quote_for_another_instrument_is_refused(self):
        reset()
        good = {"exch": "NFO", "token": "104379", "tsym": "ITC29DEC26C307.5", "lp": "7.95", "bp1": "7.85", "sp1": "7.95"}
        self.assertEqual(fno.checked(good, "104379")["bid"], D("7.85"))
        bad = {"exch": "NSE", "token": "1660", "tsym": "ITC-EQ", "lp": "307.55", "bp1": "307.55", "sp1": "307.60"}
        with self.assertRaises(fno.WrongInstrument):
            fno.checked(bad, "104380")

    def test_a_refused_quote_closes_nothing_at_a_wrong_price(self):
        reset(TRADER_FNO_CAPITAL="100000", TRADER_FNO_FUT_MARGIN_PCT="15")
        sess.now = lambda clock=None: datetime(2026, 11, 26, 10, 0, tzinfo=sess.IST)
        c = fno.Contracts(fno.load_master(DAY, fetch=lambda: zipped(MASTER)), date(2026, 11, 26))
        q = Quotes()
        b = fno.FnoBook(DAY, q, c)
        b.on_open("rsi2", "RELIANCE", "B", 1304.0, AT)

        def wrong(token):
            raise fno.WrongInstrument("got the share")
        b.quote = wrong
        b.on_close("rsi2", "RELIANCE", "stop", AT + 60)
        self.assertIn("rsi2~opt|RELIANCE", b.st["open"], "kept open until a true quote arrives")
        self.assertEqual(b.st["realised"], {})


class BenchDrivesFno(unittest.TestCase):
    def test_replayed_trades_never_touch_fno_live_ones_do(self):
        reset(TRADER_BENCH_CAPITAL="100000", TRADER_PAPER_RISK="500")
        calls = []

        class Spy:
            def on_open(self, *a):
                calls.append(("open", a[0], a[1]))

            def on_close(self, *a):
                calls.append(("close", a[0], a[1]))

        b = paper.Bench(DAY, write=False)
        b.fno = Spy()
        b.open[("orb15", "AAA")] = paper.Trade("orb15", "AAA", "B", 10, 100.0, 95.0, 120.0, AT)
        b.close(("orb15", "AAA"), 101.0, "stop", AT + 60)
        self.assertEqual(calls, [], "not live: replay")
        b.live = True
        b.open[("orb15", "AAA")] = paper.Trade("orb15", "AAA", "B", 10, 100.0, 95.0, 120.0, AT)
        b.close(("orb15", "AAA"), 101.0, "stop", AT + 60)
        self.assertEqual(calls, [("close", "orb15", "AAA")])


if __name__ == "__main__":
    unittest.main()
