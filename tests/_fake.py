"""A scripted stand-in for shoonya.Shoonya: same normalised interface,
records every write, never touches the network."""

import os
import shutil
import sys
import tempfile
from datetime import datetime
from decimal import Decimal as D

# One sandbox for every test module: the programs read these at import.
SANDBOX = tempfile.mkdtemp(prefix="trader-test-")
os.environ.setdefault("TRADER_STATE_DIR", os.path.join(SANDBOX, "auto"))
os.environ.setdefault("TRADER_ENV_FILE", os.path.join(SANDBOX, "env"))
os.environ.setdefault("TRADER_KILL_SWITCH", os.path.join(SANDBOX, "halt"))
os.environ.setdefault("SHOONYA_TOKEN_FILE", os.path.join(SANDBOX, "tok.json"))
os.environ.setdefault("SHOONYA_INSTR_CACHE", os.path.join(SANDBOX, "instr.json"))
os.environ.setdefault("TRADER_TIMER_DROPIN", os.path.join(SANDBOX, "auto.timer.d", "override.conf"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "indstocks"))

from shoonya import Instrument, ShoonyaError  # noqa: E402
from market import to_tick  # noqa: E402

NIFTY = Instrument("NIFTYBEES", "NIFTYBEES-EQ", "10576", "NSE", D("0.01"), 1)
SENSEX = Instrument("SENSEXIETF", "SENSEXIETF-EQ", "11111", "NSE", D("0.01"), 1)


def order(oid, inst, side="B", status="TRIGGER_PENDING", qty=40, filled=0, price="0",
          trigger="0", typ="SL-LMT", remarks="", avg="0"):
    return {"id": str(oid), "symbol": inst.symbol, "tsym": inst.tsym, "token": inst.token,
            "exch": "NSE", "side": side, "type": typ, "product": "I", "status": status,
            "pending": status in ("OPEN", "TRIGGER_PENDING", "PENDING"),
            "dead": status in ("CANCELED", "REJECTED"), "qty": qty, "filled": filled,
            "avg": D(avg), "price": D(price), "trigger": D(trigger), "remarks": remarks,
            "reason": "", "time": ""}


def position(inst, qty, avg, ltp=None, realised="0", unrealised="0", buy_qty=None,
             buy_avg=None, sell_qty=0, sell_avg="0"):
    return {"symbol": inst.symbol, "tsym": inst.tsym, "token": inst.token, "exch": "NSE",
            "product": "I", "qty": qty, "avg": D(avg), "ltp": D(ltp or avg),
            "realised": D(realised), "unrealised": D(unrealised),
            "buy_qty": qty if buy_qty is None else buy_qty, "buy_avg": D(buy_avg or avg),
            "sell_qty": sell_qty, "sell_avg": D(sell_avg), "tick": D("0.01")}


class FakeBroker:
    def __init__(self, *, cash="25000", positions=(), orders=(), quotes=None, candles=None,
                 instruments=(NIFTY, SENSEX), fail_cancel=(), fail_place=False):
        self._cash = D(cash)
        self._positions = list(positions)
        self._orders = list(orders)
        self._quotes = quotes or {}
        self._candles = candles or {}
        self._inst = {i.symbol: i for i in instruments}
        self.fail_cancel = set(fail_cancel)
        self.fail_place = fail_place
        self.placed, self.cancelled, self.modified = [], [], []
        self._next = 900

    # account
    def cash(self):
        return self._cash

    def positions(self, intraday_only=True):
        return list(self._positions)

    def orders(self):
        return list(self._orders)

    def limits(self):
        return {"cash": str(self._cash)}

    # market
    def quote(self, token, exch="NSE"):
        q = {"ltp": D("280"), "uc": D("308"), "lc": D("252"), "bid": D("0"), "ask": D("0"),
             "open": D("0"), "high": D("0"), "low": D("0"), "volume": 0, "tick": D("0")}
        q.update({k: (D(v) if not isinstance(v, int) else v) for k, v in self._quotes.get(str(token), {}).items()})
        return q

    def ltps(self, tokens, exch="NSE"):
        return {str(t): self.quote(t)["ltp"] for t in tokens}

    def instrument(self, symbol, exch="NSE"):
        if symbol.upper() not in self._inst:
            raise KeyError(f"{symbol} not found")
        return self._inst[symbol.upper()]

    def instrument_for(self, row):
        return self._inst.get(row["symbol"]) or Instrument(row["symbol"], row["tsym"], row["token"],
                                                            "NSE", D("0.01"), 1)

    def candles(self, token, start, end, interval=5, exch="NSE"):
        return list(self._candles.get(str(token), []))

    def search(self, text, exch="NSE"):
        return []

    # orders
    def place(self, inst, *, side, qty, price, trigger=None, remarks=""):
        if self.fail_place:
            raise ShoonyaError("PlaceOrder: RMS:Margin Exceeds")
        self._next += 1
        oid = str(self._next)
        self.placed.append({"id": oid, "symbol": inst.symbol, "side": side, "qty": qty,
                            "price": D(price), "trigger": None if trigger is None else D(trigger),
                            "remarks": remarks})
        self._orders.append(order(oid, inst, side=side, qty=qty, price=price,
                                  trigger=trigger or "0",
                                  typ="SL-LMT" if trigger is not None else "LMT",
                                  status="TRIGGER_PENDING" if trigger is not None else "OPEN",
                                  remarks=remarks))
        return oid

    def modify(self, o, inst, *, qty, price, trigger=None):
        self.modified.append({"id": o["id"], "qty": qty, "price": D(price),
                              "trigger": None if trigger is None else D(trigger)})
        return o["id"]

    def cancel(self, oid):
        if str(oid) in self.fail_cancel:
            raise ShoonyaError("CancelOrder: Rejected : ORA:Order not found")
        self.cancelled.append(str(oid))
        for o in self._orders:
            if o["id"] == str(oid):
                o["status"], o["pending"], o["dead"] = "CANCELED", False, True

    def marketable(self, inst, side, q=None):
        q = q or self.quote(inst.token)
        if side == "S":
            return to_tick(q["ltp"] * D("0.997"), inst.tick, mode="down")
        return to_tick(q["ltp"] * D("1.003"), inst.tick, mode="up")


def reset(**env):
    """Fresh log, env file and kill switch for one test."""
    import common
    shutil.rmtree(common.STATE_DIR, ignore_errors=True)
    for f in (common.KILL_SWITCH,):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass
    lines = [f"{k}={v}" for k, v in {"TRADER_LIVE": "true", "TRADER_SYMBOLS": "NIFTYBEES,SENSEXIETF",
                                      "TRADER_INTERNALS": "false", "TRADER_KICK_SCAN": "false",
                                      **env}.items()]
    common.ENV_FILE.write_text("\n".join(lines) + "\n")


def candle(day, hh, mm, o, h, l, c, v=10000):
    from market import Candle
    import session as sess
    return Candle(datetime(day.year, day.month, day.day, hh, mm, tzinfo=sess.IST),
                  D(str(o)), D(str(h)), D(str(l)), D(str(c)), v, 5)


def opening(day, hi="281.00", lo="279.60", hh=9, mm=15):
    """Three 5-minute candles making an opening range of exactly lo..hi."""
    hi, lo = D(hi), D(lo)
    mid = (hi + lo) / 2
    return [candle(day, hh, mm, mid, hi, mid, mid),
            candle(day, hh, mm + 5, mid, mid, lo, mid),
            candle(day, hh, mm + 10, mid, mid, mid, mid)]
