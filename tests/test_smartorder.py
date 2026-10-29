import unittest
from decimal import Decimal as D

from _load import load

_m = load("indstocks", "smartorder")
SmartOrder = _m.SmartOrder
SmartOrderError = _m.SmartOrderError
build_long_day_trade = _m.build_long_day_trade
on_tick = _m.on_tick
to_tick = _m.to_tick


class TestTick(unittest.TestCase):
    def test_float_modulo_would_lie(self):
        # The reason everything here is Decimal.
        self.assertNotEqual(358.35 % 0.05, 0.0)
        self.assertTrue(on_tick(D("358.35"), D("0.05")))

    def test_rounding_direction(self):
        self.assertEqual(to_tick(D("358.32"), D("0.05"), mode="down"), D("358.30"))
        self.assertEqual(to_tick(D("358.32"), D("0.05"), mode="up"), D("358.35"))


class TestLegRules(unittest.TestCase):
    def base(self, **kw):
        args = dict(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="LIMIT", security_id="3045", qty=10,
            limit_price=D("1500.00"), tick_size=D("0.05"),
        )
        args.update(kw)
        return SmartOrder(**args)

    def test_buy_ordering_is_enforced(self):
        o = self.base(
            sl_trigger_price=D("1480.00"), sl_limit_price=D("1479.50"),
            tgt_trigger_price=D("1540.00"), tgt_limit_price=D("1540.50"),
        )
        self.assertTrue(o.validate())

    def test_buy_stop_above_entry_rejected(self):
        o = self.base(sl_trigger_price=D("1520.00"), sl_limit_price=D("1519.50"))
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "MaxSlTriggerPrice")

    def test_buy_stop_limit_above_trigger_rejected(self):
        o = self.base(sl_trigger_price=D("1480.00"), sl_limit_price=D("1480.50"))
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "MaxSlLimitPrice")

    def test_buy_target_limit_below_trigger_rejected(self):
        o = self.base(tgt_trigger_price=D("1540.00"), tgt_limit_price=D("1539.50"))
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "MinTgtLimitPrice")

    def test_sell_is_the_mirror_image(self):
        o = self.base(
            txn_type="SELL",
            sl_trigger_price=D("1520.00"), sl_limit_price=D("1520.50"),
            tgt_trigger_price=D("1460.00"), tgt_limit_price=D("1459.50"),
        )
        self.assertTrue(o.validate())

    def test_half_a_leg_is_rejected(self):
        o = self.base(sl_trigger_price=D("1480.00"))
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "StopLossLegIncomplete")

    def test_off_tick_leg_rejected(self):
        o = self.base(sl_trigger_price=D("1480.03"), sl_limit_price=D("1479.50"))
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "LegPriceTickSize")


class TestOrderTypes(unittest.TestCase):
    def test_trigger_rejects_limit_price(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="TRIGGER", security_id="3045", qty=10,
            trigger_price=D("1520.00"), limit_price=D("1520.00"),
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "LimitPriceNotAllowedForTrigger")

    def test_buy_trigger_must_exceed_cmp(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="TRIGGER", security_id="3045", qty=10,
            trigger_price=D("1500.00"), cmp=D("1510.00"),
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "TriggerPriceVsCMP")

    def test_trigger_entry_price_comes_from_trigger_limit(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="TRIGGER", security_id="3045", qty=10,
            trigger_price=D("1520.00"), trigger_limit_price=D("1521.00"),
            cmp=D("1510.00"),
        )
        self.assertEqual(o.entry_price(), D("1521.00"))

    def test_market_warns_about_conversion(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="MARKET", security_id="3045", qty=10, cmp=D("1500.00"),
        )
        o.validate()
        self.assertTrue(any("converted to LIMIT" in w for w in o.warnings))

    def test_market_without_cmp_cannot_be_validated(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="MARKET", security_id="3045", qty=10,
            sl_trigger_price=D("1480.00"), sl_limit_price=D("1479.50"),
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "MarketEntryNeedsCMP")


class TestMisc(unittest.TestCase):
    def test_equity_slicing_rejected(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="LIMIT", security_id="3045", qty=10,
            limit_price=D("1500.00"), slice_order=True,
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "SlicingNotAllowedForEquity")

    def test_long_remarks_rejected_not_truncated(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="EQUITY", product="INTRADAY",
            order_type="LIMIT", security_id="3045", qty=10,
            limit_price=D("1500.00"), remarks="x" * 101,
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "RemarksTooLong")

    def test_lot_size_enforced_on_derivatives(self):
        o = SmartOrder(
            txn_type="BUY", exchange="NSE", segment="DERIVATIVE", product="MARGIN",
            order_type="LIMIT", security_id="51011", qty=70,
            limit_price=D("37.00"), lot_size=75,
        )
        with self.assertRaises(SmartOrderError) as cm:
            o.validate()
        self.assertEqual(cm.exception.code, "QtyMultipleOfLotSize")

    def test_bse_algo_id_default(self):
        o = SmartOrder(
            txn_type="BUY", exchange="BSE", segment="EQUITY", product="INTRADAY",
            order_type="LIMIT", security_id="500325", qty=10,
            limit_price=D("1500.00"),
        )
        self.assertEqual(o.payload()["algo_id"], "9999999999999999")


class TestBuilder(unittest.TestCase):
    def test_three_numbers_become_four_legal_prices(self):
        o = build_long_day_trade(
            security_id="3045", qty=8,
            entry=D("364.18"), stop=D("358.32"), target=D("370.04"),
            remarks="orb-v1/sig-0001",
        )
        p = o.payload()
        self.assertEqual(p["sl_trigger_price"], 358.30)
        self.assertEqual(p["sl_limit_price"], 358.20)
        self.assertEqual(p["tgt_trigger_price"], 370.05)
        self.assertEqual(p["tgt_limit_price"], 370.15)
        self.assertEqual(p["product"], "INTRADAY")
        self.assertEqual(p["algo_id"], "99999")
        self.assertEqual(p["remarks"], "orb-v1/sig-0001")
        self.assertNotIn("trigger_price", p)

    def test_builder_output_survives_validation(self):
        o = build_long_day_trade(
            security_id="3045", qty=8,
            entry=D("1500.00"), stop=D("1480.00"), target=D("1540.00"),
        )
        self.assertTrue(o.validate())


if __name__ == "__main__":
    unittest.main()
