"""
The paper broker (the owner's PIP, from 20 Nov 2026): the whole real trading
pipeline (the AI's scans, the fill monitor's stops and trailing lock, the day
lock, the Rs 300 kill, the 14:50 flatten, the nightly learning) runs exactly as
it does with money, but every order is filled here, on paper, at live prices.

  prices   market data still comes from Shoonya (read-only: quotes, candles,
           instruments). Fills use the tape (tape.py): every tick's last price
           and best bid/ask since the order was placed, so a stop that was
           touched for one second between two fill-monitor polls still fills.
  fills    a buy fills at the ask (a sell at the bid) once that is at or
           better than its limit; a stop triggers on the last price crossing
           its trigger and then fills like a limit; a gap past the limit
           leaves it resting, exactly the case the fill monitor chases.
  margin   TRADER_PAPER_CAPITAL (Rs 60,000) cash, MIS margin 20% of the
           order value; an order beyond the free margin is REJECTED.
  charges  the pipeline's own P&L uses charges.py on these fills: the same
           Shoonya charges as a real day.

Real orders are impossible in this mode: Shoonya.place/modify/cancel refuse
whenever TRADER_BROKER=paper, whatever object they are called on.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import session as sess  # noqa: E402
from market import to_tick  # noqa: E402

D = Decimal
PB_DIR = common.STATE_DIR / "paperbroker"
MARGIN_PCT = D("0.20")


def capital() -> Decimal:
    try:
        return D(common.setting("TRADER_PAPER_CAPITAL", "60000"))
    except ArithmeticError:
        return D("60000")


def _now_str(now: datetime) -> str:
    return now.strftime("%H:%M:%S %d-%m-%Y")


class PaperBroker:
    """Same normalised interface as shoonya.Shoonya; orders never leave this box."""

    paper = True

    def __init__(self, data=None, now=None):
        self._data = data
        self._now = now or sess.now

    # -- the read-only market data client -----------------------------------------

    @property
    def data(self):
        if self._data is None:
            import shoonya
            self._data = shoonya.Shoonya(data_only=True)
        return self._data

    def quote(self, token: str, exch: str = "NSE") -> dict:
        return self.data.quote(token, exch)

    def ltps(self, tokens, exch: str = "NSE"):
        return self.data.ltps(tokens, exch)

    def search(self, text, exch: str = "NSE"):
        return self.data.search(text, exch)

    def instrument(self, symbol, exch: str = "NSE"):
        return self.data.instrument(symbol, exch)

    def candles(self, token, start, end, **k):
        return self.data.candles(token, start, end, **k)

    ORDER_ENDPOINTS = ("PlaceOrder", "ModifyOrder", "CancelOrder", "ExitSNOOrder")

    def call(self, endpoint: str, payload: dict, **k):
        """Raw read-only calls (quotes, option chains) go to the real session; order calls refuse."""
        import shoonya
        if endpoint in self.ORDER_ENDPOINTS:
            raise shoonya.ShoonyaError("paper mode: real orders are disabled")
        return self.data.call(endpoint, payload, **k)

    def marketable(self, inst, side, q=None):
        import shoonya
        return shoonya.Shoonya.marketable(self, inst, side, q)

    def instrument_for(self, row):
        import shoonya
        return shoonya.Shoonya.instrument_for(self, row)

    # -- state --------------------------------------------------------------------

    def _day(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _path(self):
        return PB_DIR / f"{self._day()}.json"

    @contextmanager
    def _locked(self):
        PB_DIR.mkdir(parents=True, exist_ok=True)
        with open(PB_DIR / ".lock", "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                try:
                    st = json.loads(self._path().read_text())
                except (OSError, ValueError):
                    st = {"orders": [], "fills": [], "offsets": {}, "seq": 0}
                yield st
                tmp = self._path().with_suffix(".tmp")
                tmp.write_text(json.dumps(st, default=str))
                tmp.replace(self._path())
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    # -- prices -------------------------------------------------------------------

    def _tape_file(self, symbol: str):
        return common.STATE_DIR / "tape" / self._day() / f"{symbol}.jsonl"

    def _ticks_since(self, st: dict, symbol: str) -> List[dict]:
        """New tape records for symbol since the shared offset (advances it)."""
        f = self._tape_file(symbol)
        off = int(st["offsets"].get(symbol, 0))
        try:
            size = f.stat().st_size
        except OSError:
            return []
        if size < off:
            off = 0
        out = []
        with open(f, "rb") as fh:
            fh.seek(off)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        for line in chunk[:end].splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        st["offsets"][symbol] = off + end + 1
        return out

    def _tape_fresh(self, symbol: str, max_age_s: float = 30) -> bool:
        try:
            return self._now().timestamp() - self._tape_file(symbol).stat().st_mtime <= max_age_s
        except OSError:
            return False

    def _tape_size(self, symbol: str) -> int:
        try:
            return self._tape_file(symbol).stat().st_size
        except OSError:
            return 0

    # -- matching ------------------------------------------------------------------

    @staticmethod
    def _try_fill(o: dict, lp: Optional[D], bid: Optional[D], ask: Optional[D]) -> Optional[D]:
        """Fill price for one order at one tick, or None. Triggers the stop if crossed."""
        if lp is None:
            return None
        if o["status"] == "TRIGGER_PENDING":
            trg = D(o["trigger"])
            hit = lp >= trg if o["side"] == "B" else lp <= trg
            if not hit:
                return None
            o["status"] = "OPEN"
        px = D(o["price"])
        if o["side"] == "B":
            touch = ask if ask and ask > 0 else lp
            return touch if touch <= px else None
        touch = bid if bid and bid > 0 else lp
        return touch if touch >= px else None

    def _fill(self, st: dict, o: dict, px: D, at: datetime) -> None:
        o.update(status="COMPLETE", filled=o["qty"], avg=str(px), time=_now_str(at))
        st["fills"].append({"id": o["id"], "token": o["token"], "symbol": o["symbol"], "tsym": o["tsym"],
                            "side": o["side"], "qty": o["qty"], "price": str(px), "time": _now_str(at),
                            "tick": o.get("tick", "0.01")})

    def _match(self, st: dict) -> None:
        pend = defaultdict(list)
        for o in st["orders"]:
            if o["status"] in ("OPEN", "TRIGGER_PENDING"):
                pend[o["symbol"]].append(o)
        for sym, orders in pend.items():
            ticks = self._ticks_since(st, sym)
            if not ticks and self._tape_fresh(sym):
                continue                                         # the tape is live; nothing new yet
            if not ticks:
                # no tape (it is down, or the stock is not recorded): one live quote
                try:
                    q = self.data.quote(orders[0]["token"])
                    ticks = [{"t": self._now().timestamp(), "lp": q["ltp"], "bp1": q["bid"], "sp1": q["ask"]}]
                except Exception:                                # noqa: BLE001
                    continue
            for r in ticks:
                lp, bid, ask = (D(str(r[k])) if r.get(k) not in (None, "") else None for k in ("lp", "bp1", "sp1"))
                at = datetime.fromtimestamp(float(r.get("t") or self._now().timestamp()), sess.IST)
                for o in orders:
                    if o["status"] not in ("OPEN", "TRIGGER_PENDING") or float(r.get("t") or 0) < o["placed_t"]:
                        continue
                    px = self._try_fill(o, lp, bid, ask)
                    if px is not None:
                        self._fill(st, o, px, at)

    # -- account --------------------------------------------------------------------

    @staticmethod
    def _book(fills: List[dict]) -> Dict[str, dict]:
        """FIFO positions per token from the day's fills: net qty, net avg, realised."""
        out: Dict[str, dict] = {}
        for f in fills:
            p = out.setdefault(f["token"], {"symbol": f["symbol"], "tsym": f["tsym"], "token": f["token"],
                                            "tick": f.get("tick", "0.01"), "lots": [], "realised": D(0),
                                            "buy_qty": 0, "buy_val": D(0), "sell_qty": 0, "sell_val": D(0)})
            q, px = int(f["qty"]), D(f["price"])
            sgn = 1 if f["side"] == "B" else -1
            if f["side"] == "B":
                p["buy_qty"] += q; p["buy_val"] += px * q
            else:
                p["sell_qty"] += q; p["sell_val"] += px * q
            left = q
            while left and p["lots"] and (p["lots"][0][0] > 0) != (sgn > 0):
                lq, lpx = p["lots"][0]
                take = min(left, abs(lq))
                p["realised"] += (px - lpx) * take if lq > 0 else (lpx - px) * take
                lq = lq - take if lq > 0 else lq + take
                left -= take
                if lq == 0:
                    p["lots"].pop(0)
                else:
                    p["lots"][0] = (lq, lpx)
            if left:
                p["lots"].append((sgn * left, px))
        return out

    def _margin_used(self, st: dict) -> D:
        used = D(0)
        for p in self._book(st["fills"]).values():
            used += sum(abs(q) * px for q, px in p["lots"]) * MARGIN_PCT
        for o in st["orders"]:
            if o["status"] in ("OPEN", "TRIGGER_PENDING") and not o.get("closing"):
                used += D(o["price"]) * o["qty"] * MARGIN_PCT
        return used

    def limits(self) -> dict:
        with self._locked() as st:
            self._match(st)
            return {"cash": str(capital()), "payin": "0", "payout": "0", "marginused": str(self._margin_used(st))}

    def cash(self) -> D:
        return capital()

    def free_margin(self) -> D:
        lim = self.limits()
        return D(lim["cash"]) - D(lim["marginused"])

    def order_margin(self, inst, *, side, qty, price, trigger=None):
        return (D(str(price)) * qty * MARGIN_PCT).quantize(D("0.01"))

    def positions(self, *, intraday_only: bool = True) -> List[dict]:
        with self._locked() as st:
            self._match(st)
            book = self._book(st["fills"])
        out = []
        for tok, p in book.items():
            net = sum(q for q, _ in p["lots"])
            avg = (sum(abs(q) * px for q, px in p["lots"]) / abs(net)) if net else D(0)
            try:
                ltp = self.data.quote(tok)["ltp"]
            except Exception:                                    # noqa: BLE001
                ltp = avg
            unreal = (ltp - avg) * net if net else D(0)
            out.append({"symbol": p["symbol"], "tsym": p["tsym"], "token": tok, "exch": "NSE", "product": "I",
                        "qty": net, "avg": avg.quantize(D("0.01")) if net else D(0), "ltp": ltp,
                        "realised": p["realised"].quantize(D("0.01")), "unrealised": unreal.quantize(D("0.01")),
                        "buy_qty": p["buy_qty"], "buy_avg": (p["buy_val"] / p["buy_qty"]).quantize(D("0.01")) if p["buy_qty"] else D(0),
                        "sell_qty": p["sell_qty"], "sell_avg": (p["sell_val"] / p["sell_qty"]).quantize(D("0.01")) if p["sell_qty"] else D(0),
                        "tick": D(str(p["tick"]))})
        return out

    def orders(self) -> List[dict]:
        with self._locked() as st:
            self._match(st)
            rows = [dict(o) for o in st["orders"]]
        out = []
        for o in rows:
            out.append({"id": o["id"], "symbol": o["symbol"], "tsym": o["tsym"], "token": o["token"], "exch": "NSE",
                        "side": o["side"], "type": o["type"], "product": "I", "status": o["status"],
                        "pending": o["status"] in ("OPEN", "TRIGGER_PENDING"),
                        "dead": o["status"] in ("CANCELED", "REJECTED"),
                        "qty": o["qty"], "filled": o.get("filled", 0), "avg": D(o.get("avg") or "0"),
                        "price": D(o["price"]), "trigger": D(o.get("trigger") or "0"),
                        "remarks": o.get("remarks", ""), "reason": o.get("reason", ""), "time": o["time"]})
        return out

    # -- orders ---------------------------------------------------------------------

    def place(self, inst, *, side: str, qty: int, price, trigger=None, remarks: str = "") -> str:
        import shoonya
        if side not in ("B", "S"):
            raise shoonya.ShoonyaError(f"side must be B or S, not {side!r}")
        if qty <= 0:
            raise shoonya.ShoonyaError("quantity must be positive")
        now = self._now()
        with self._locked() as st:
            self._match(st)
            st["seq"] += 1
            oid = f"P{now.strftime('%y%m%d')}{st['seq']:05d}"
            book = self._book(st["fills"])
            held = sum(q for q, _ in book.get(str(inst.token), {}).get("lots", []))
            closing = (held > 0 and side == "S" and qty <= held) or (held < 0 and side == "B" and qty <= -held)
            o = {"id": oid, "symbol": inst.symbol, "tsym": inst.tsym, "token": str(inst.token), "side": side,
                 "qty": int(qty), "price": str(to_tick(D(str(price)), inst.tick)),
                 "trigger": str(to_tick(D(str(trigger)), inst.tick)) if trigger is not None else "0",
                 "type": "SL-LMT" if trigger is not None else "LMT",
                 "status": "TRIGGER_PENDING" if trigger is not None else "OPEN",
                 "filled": 0, "avg": "0", "remarks": remarks[:60], "time": _now_str(now),
                 "placed_t": now.timestamp(), "tick": str(inst.tick), "closing": closing}
            if not closing and D(o["price"]) * qty * MARGIN_PCT > capital() - self._margin_used(st):
                o.update(status="REJECTED", reason="paper: insufficient margin")
            elif not any(x["status"] in ("OPEN", "TRIGGER_PENDING") and x["symbol"] == inst.symbol
                         for x in st["orders"]):
                st["offsets"][inst.symbol] = self._tape_size(inst.symbol)    # only ticks from now on
            st["orders"].append(o)
            if o["status"] != "REJECTED" and trigger is None:
                # a marketable limit fills on the current touch, like the exchange would
                try:
                    q = self.data.quote(str(inst.token))
                    px = self._try_fill(o, q["ltp"], q["bid"], q["ask"])
                    if px is not None:
                        self._fill(st, o, px, now)
                except Exception:                                # noqa: BLE001
                    pass
        common.log("paperbroker", {"event": "paper_order", "order_id": oid, "symbol": inst.symbol, "side": side,
                                   "qty": qty, "price": o["price"], "trigger": o["trigger"], "status": o["status"],
                                   "remarks": remarks[:60]})
        return oid

    def modify(self, order: dict, inst, *, qty: int, price, trigger=None) -> str:
        import shoonya
        with self._locked() as st:
            self._match(st)
            o = next((x for x in st["orders"] if x["id"] == order["id"]), None)
            if o is None or o["status"] not in ("OPEN", "TRIGGER_PENDING"):
                raise shoonya.ShoonyaError(f"paper: order {order['id']} is not open")
            o.update(qty=int(qty), price=str(to_tick(D(str(price)), inst.tick)),
                     trigger=str(to_tick(D(str(trigger)), inst.tick)) if trigger is not None else "0",
                     type="SL-LMT" if trigger is not None else "LMT")
            if trigger is None:
                o["status"] = "OPEN"
        return order["id"]

    def cancel(self, order_id: str) -> None:
        import shoonya
        with self._locked() as st:
            self._match(st)
            o = next((x for x in st["orders"] if x["id"] == str(order_id)), None)
            if o is None or o["status"] not in ("OPEN", "TRIGGER_PENDING"):
                raise shoonya.ShoonyaError(f"paper: order {order_id} is not open")
            o["status"] = "CANCELED"
