import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from _load import load

_s = load("indstocks", "session")
_b = load("indstocks", "bars")
_st = load("indstocks", "strategy")
_r = load("indstocks", "risk")

IST = _s.IST
DAY = date(2026, 10, 30)          # a Friday, not an NSE holiday


def t(hh, mm):
    return datetime(2026, 10, 30, hh, mm, tzinfo=IST)


def candle(hh, mm, o, h, l, c, v=100000, minutes=5):
    return _b.Candle(opens_at=t(hh, mm), open=D(str(o)), high=D(str(h)),
                     low=D(str(l)), close=D(str(c)), volume=v,
                     interval_minutes=minutes)


class TestSession(unittest.TestCase):
    def test_weekend_is_not_a_trading_day(self):
        self.assertFalse(_s.is_trading_day(date(2026, 10, 31)))   # Saturday
        self.assertTrue(_s.is_trading_day(DAY))

    def test_listed_holiday_is_not_a_trading_day(self):
        self.assertFalse(_s.is_trading_day(date(2026, 1, 26)))     # Republic Day

    def test_phases_across_the_day(self):
        s = _s.Session(DAY)
        for hh, mm, expected in (
            (9, 0, "pre_open"), (9, 20, "opening_range"), (9, 30, "entry_window"),
            (14, 0, "entry_window"), (14, 45, "no_new_entries"),
            (15, 20, "flatten"), (15, 45, "closed"),
        ):
            self.assertEqual(s.phase(t(hh, mm)), expected, f"{hh}:{mm}")

    def test_opening_range_ends_at_0930(self):
        self.assertEqual(_s.Session(DAY).opening_range_end, t(9, 30))

    def test_previous_trading_days_skips_the_weekend(self):
        prev = _s.previous_trading_days(date(2026, 10, 26), 3)   # a Monday
        self.assertEqual(prev[0], date(2026, 10, 23))            # Friday
        self.assertEqual(prev[1], date(2026, 10, 22))

    def test_request_ms_vs_candle_seconds(self):
        moment = t(9, 15)
        self.assertEqual(_s.epoch_ms(moment), int(moment.timestamp()) * 1000)
        self.assertEqual(_s.from_candle_ts(int(moment.timestamp())), moment)


class TestBars(unittest.TestCase):
    def test_candle_close_time_and_completeness(self):
        c = candle(9, 20, 100, 102, 99, 101)
        self.assertEqual(c.closes_at, t(9, 25))
        self.assertFalse(c.is_complete_at(t(9, 24)))
        self.assertTrue(c.is_complete_at(t(9, 25)))

    def test_parse_uses_success_not_status(self):
        payload = {"success": True, "data": {"NSE_1594": {"candles": [
            {"ts": int(t(9, 15).timestamp()), "o": 1007, "h": 1013.9,
             "l": 999.3, "c": 1000.4, "v": 2847163}]}}}
        out = _b.parse_response(payload, "5minute")
        self.assertEqual(out["NSE_1594"][0].high, D("1013.9"))
        self.assertEqual(out["NSE_1594"][0].opens_at, t(9, 15))

    def test_unsuccessful_payload_raises(self):
        with self.assertRaises(RuntimeError):
            _b.parse_response({"success": False, "message": "nope"}, "5minute")

    def test_batches_five_codes_per_call(self):
        calls = []

        class C:
            def get(self, path, params=None):
                calls.append(params["scrip-codes"].split(","))
                return {"success": True, "data": {}}

        codes = [f"NSE_{i}" for i in range(12)]
        out = _b.Bars(C()).fetch(codes, t(9, 15), t(15, 30), sleep=lambda s: None)
        self.assertEqual(len(calls[0]), 5, "first pass batches by five")
        self.assertEqual(len(out), 12)
        self.assertEqual(out["NSE_0"], [], "absent codes become explicit empties")

    def test_never_exceeds_the_documented_maximum(self):
        self.assertLessEqual(_b.MAX_CODES_PER_CALL, 5)

    def test_empty_result_is_retried_individually(self):
        # The endpoint answers success:true with no candles when throttled,
        # so an empty array must not be taken at face value.
        attempts = {"n": 0}

        def payload(codes):
            return {"success": True, "data": {c: {"candles": [
                {"ts": int(t(9, 15).timestamp()), "o": 1, "h": 2, "l": 1,
                 "c": 2, "v": 10}]} for c in codes}}

        class C:
            def get(self, path, params=None):
                attempts["n"] += 1
                codes = params["scrip-codes"].split(",")
                if attempts["n"] == 1:          # first batch comes back empty
                    return {"success": True, "data": {}}
                return payload(codes)

        out = _b.Bars(C()).fetch(["NSE_1", "NSE_2"], t(9, 15), t(15, 30),
                                 sleep=lambda s: None)
        self.assertEqual(len(out["NSE_1"]), 1)
        self.assertEqual(len(out["NSE_2"]), 1)

    def test_codes_still_empty_after_retries_are_surfaced(self):
        class C:
            def get(self, path, params=None):
                return {"success": True, "data": {}}

        bars = _b.Bars(C())
        bars.fetch(["NSE_1"], t(9, 15), t(15, 30), sleep=lambda s: None)
        self.assertEqual(bars.unresolved, ["NSE_1"],
                         "a persistent empty must be reported, not read as 'no setup'")

    def test_a_successful_code_is_not_retried(self):
        calls = []

        class C:
            def get(self, path, params=None):
                calls.append(params["scrip-codes"])
                return {"success": True, "data": {"NSE_1": {"candles": [
                    {"ts": int(t(9, 15).timestamp()), "o": 1, "h": 2,
                     "l": 1, "c": 2, "v": 10}]}}}

        _b.Bars(C()).fetch(["NSE_1"], t(9, 15), t(15, 30), sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_opening_range_only_counts_candles_inside_the_window(self):
        s = _s.Session(DAY)
        cs = [candle(9, 15, 100, 105, 99, 104),
              candle(9, 20, 104, 106, 103, 105),
              candle(9, 25, 105, 107, 104, 106),
              candle(9, 30, 106, 120, 106, 119)]   # after the range
        hi, lo = _b.opening_range(cs, s)
        self.assertEqual((hi, lo), (D("107"), D("99")))


class TestEntry(unittest.TestCase):
    def setUp(self):
        self.s = _s.Session(DAY)
        self.rng = [candle(9, 15, 100, 105, 99, 104),
                    candle(9, 20, 104, 106, 103, 105),
                    candle(9, 25, 105, 107, 104, 106)]

    def test_waits_while_the_range_is_forming(self):
        sig = _st.check_entry(self.rng, self.s, t(9, 22))
        self.assertEqual(sig.state, _st.WAITING)

    def test_close_above_the_range_high_triggers(self):
        cs = self.rng + [candle(9, 30, 106, 110, 106, 108)]
        sig = _st.check_entry(cs, self.s, t(9, 35))
        self.assertTrue(sig.triggered)
        self.assertEqual(sig.price, D("108"))
        self.assertEqual(sig.at, t(9, 35))

    def test_a_touch_without_a_close_above_does_not_trigger(self):
        # High pokes through 107 but the candle closes back under it.
        cs = self.rng + [candle(9, 30, 106, 112, 105, 106.5)]
        sig = _st.check_entry(cs, self.s, t(9, 35))
        self.assertEqual(sig.state, _st.WAITING)
        self.assertEqual(sig.distance, D("0.5"))

    def test_a_forming_candle_never_triggers(self):
        cs = self.rng + [candle(9, 30, 106, 110, 106, 108)]
        # 09:33: the 09:30 candle has not closed yet.
        sig = _st.check_entry(cs, self.s, t(9, 33))
        self.assertEqual(sig.state, _st.WAITING)

    def test_no_entry_after_the_window_closes(self):
        cs = self.rng + [candle(14, 40, 106, 110, 106, 108)]
        sig = _st.check_entry(cs, self.s, t(14, 50))
        self.assertEqual(sig.state, _st.OUTSIDE_WINDOW)

    def test_volume_filter_suppresses_a_thin_breakout(self):
        cs = self.rng + [candle(9, 30, 106, 110, 106, 108, v=10)]
        self.assertEqual(_st.check_entry(cs, self.s, t(9, 35), min_volume=1000).state,
                         _st.WAITING)

    def test_no_range_when_there_are_no_candles(self):
        self.assertEqual(_st.check_entry([], self.s, t(10, 0)).state, _st.NO_RANGE)


class TestExitPlan(unittest.TestCase):
    def test_midpoint_stop_and_2r_target(self):
        p = _st.plan_exit(D("108"), D("107"), D("99"), D("0.05"))
        self.assertEqual(p.stop, D("103.00"))        # midpoint of 107/99
        self.assertEqual(p.risk_per_share, D("5.00"))
        self.assertEqual(p.target, D("118.00"))      # 2R
        self.assertEqual(p.reward_to_risk, D("2"))

    def test_rounding_never_tightens_the_stop(self):
        # Midpoint 103.03 must round DOWN to the 0.10 grid, not to nearest.
        p = _st.plan_exit(D("108"), D("107.06"), D("99"), D("0.10"))
        self.assertEqual(p.stop, D("103.00"))

    def test_stop_at_or_above_entry_is_refused(self):
        with self.assertRaises(_st.PlanError) as cm:
            _st.plan_exit(D("100"), D("107"), D("99"), D("0.05"))
        self.assertIn("not below entry", str(cm.exception))

    def test_risk_tighter_than_two_ticks_is_refused(self):
        with self.assertRaises(_st.PlanError) as cm:
            _st.plan_exit(D("103.05"), D("107"), D("99"), D("0.05"))
        self.assertIn("too tight", str(cm.exception))

    def test_or_low_stop_mode(self):
        p = _st.plan_exit(D("108"), D("107"), D("99"), D("0.05"), stop_mode="or-low")
        self.assertEqual(p.stop, D("99.00"))


class TestSizingAndRisk(unittest.TestCase):
    def plan(self, entry="108", stop="103", target="118"):
        return _st.ExitPlan(D(entry), D(stop), D(target),
                            D(entry) - D(stop),
                            (D(target) - D(entry)) / (D(entry) - D(stop)))

    def test_max_loss_decides_the_size(self):
        s = _r.size_position(self.plan(), D("100000"), _r.Limits())
        self.assertEqual(s.qty, 100)                  # 500 / 5
        self.assertEqual(s.limit_hit, "max loss per trade")
        self.assertEqual(s.loss_at_stop, D("500"))

    def test_cash_buffer_is_respected(self):
        s = _r.size_position(self.plan(), D("5000"), _r.Limits())
        self.assertEqual(s.limit_hit, "cash after buffer")
        self.assertEqual(s.qty, 41)                   # 4500 / 108

    def test_round_trip_cost_counts_two_orders(self):
        self.assertEqual(_r.size_position(self.plan(), D("100000"), _r.Limits())
                         .round_trip_cost, D("20"))

    def test_stop_too_far_refuses_rather_than_shrinking(self):
        wide = self.plan(entry="108", stop="8", target="208")
        s = _r.size_position(wide, D("100000"), _r.Limits(max_loss_per_trade=D("50")))
        self.assertFalse(s.ok)

    def gate(self, **kw):
        base = dict(symbol="RELIANCE", plan=self.plan(),
                    sizing=_r.size_position(self.plan(), D("100000"), _r.Limits()),
                    session=_s.Session(DAY), now=t(10, 0),
                    day=_r.DayState(), limits=_r.Limits())
        base.update(kw)
        return _r.check_entry_order(**base)

    def test_a_clean_order_passes(self):
        self.assertTrue(self.gate().passed)

    def test_blocked_outside_the_entry_window(self):
        v = self.gate(now=t(15, 0))
        self.assertFalse(v.passed)
        self.assertIn("no new entries", str(v))

    def test_trade_count_limit_blocks(self):
        v = self.gate(day=_r.DayState(trades_taken=3))
        self.assertFalse(v.passed)
        self.assertIn("already took 3", str(v))

    def test_daily_risk_limit_blocks(self):
        v = self.gate(day=_r.DayState(risk_committed=D("1400")))
        self.assertFalse(v.passed)
        self.assertIn("daily limit", str(v))

    def test_reward_to_risk_floor_blocks(self):
        v = self.gate(plan=self.plan(target="109"),
                      limits=_r.Limits(min_reward_to_risk=D("1.5")))
        self.assertFalse(v.passed)
        self.assertIn("reward:risk", str(v))

    def test_doubling_an_open_position_blocks(self):
        v = self.gate(day=_r.DayState(open_symbols=("RELIANCE",)))
        self.assertFalse(v.passed)
        self.assertIn("already holding", str(v))

    def test_blocked_symbol(self):
        v = self.gate(limits=_r.Limits(blocked_symbols=("RELIANCE",)))
        self.assertFalse(v.passed)

    def test_getting_out_is_never_blocked_by_day_limits(self):
        self.assertTrue(_r.check_exit_order(symbol="RELIANCE", qty=100,
                                            held_qty=100).passed)

    def test_cannot_sell_more_than_held(self):
        v = _r.check_exit_order(symbol="RELIANCE", qty=200, held_qty=100)
        self.assertFalse(v.passed)
        self.assertIn("only 100 held", str(v))


if __name__ == "__main__":
    unittest.main()
