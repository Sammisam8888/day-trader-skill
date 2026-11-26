"""
F&O sleeves for the paper bench (owner, 26 Nov): every strategy's stock signal is
also traded, side by side, in

  <model>~fut   the stock's near-month FUTURE, 1 lot, long or short as the signal
  <model>~opt   the stock's at-the-money OPTION bought: the call on a BUY signal,
                the put on a SELL signal

each sleeve with its own paper wallet (TRADER_FNO_CAPITAL, Rs 1,00,000) and day
target (TRADER_BENCH_TARGET). The sleeve opens when the stock trade opens and
closes when the stock trade closes (stop, target, 14:50, day target): the same
signal, a different instrument. Paper only; prices are live NFO quotes.

  contracts   Shoonya's daily NFO master (token, lot size, expiry, strike). The
              near month rolls to the next when it expires within 2 days.
  sizing      futures: 1 lot if its margin (TRADER_FNO_FUT_MARGIN_PCT, 20% of the
              lot value) fits the free wallet, else the signal is logged as skipped;
              options: as many lots as TRADER_FNO_OPT_BUDGET (Rs 20,000) of premium
              buys, at least 1 if the wallet can pay for it.
  fills       buys at the ask, sells at the bid (the last price if the book is empty).
  charges     Shoonya F&O, from 1 Apr 2026: STT 0.05% of futures sell value, 0.15% of
              option premium sold; exchange 0.00173% (futures) / 0.03503% (option
              premium); stamp 0.002% / 0.003% on the buy side; SEBI Rs 10 a crore;
              brokerage Rs 5 an order; GST 18% on brokerage, exchange and SEBI fees.
              Check these against a contract note before trusting a tight margin.
  state       STATE_DIR/fno/DAY.json survives restarts (F&O prices cannot be
              replayed from the equity tape, so this book is never rebuilt).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal as D
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import session as sess  # noqa: E402

MASTER_URL = "https://api.shoonya.com/NFO_symbols.txt.zip"
FNO_DIR = common.STATE_DIR / "fno"
SLEEVES = ("fut", "opt")


def _d(key: str, default: str) -> D:
    try:
        return D(common.setting(key, default))
    except ArithmeticError:
        return D(default)


def capital() -> D:
    return _d("TRADER_FNO_CAPITAL", "100000")


def enabled() -> bool:
    return common.setting("TRADER_FNO", "true").lower() in ("1", "true", "yes")


# ------------------------------------------------------------------ charges

def _r(x: D) -> D:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def charges(kind: str, buy_value: D, sell_value: D) -> D:
    """All-in charges of one F&O round trip (kind 'fut' or 'opt'; values are
    lot value for futures, premium x quantity for options)."""
    turnover = buy_value + sell_value
    brokerage = D("10")                                       # Rs 5 x 2 orders
    if kind == "fut":
        stt = sell_value * D("0.0005")
        exch = turnover * D("0.0000173")
        stamp = buy_value * D("0.00002")
    else:
        stt = sell_value * D("0.0015")
        exch = turnover * D("0.0003503")
        stamp = buy_value * D("0.00003")
    sebi = turnover * D("0.000001")
    gst = (brokerage + exch + sebi) * D("0.18")
    return _r(brokerage + stt + exch + stamp + sebi + gst)


# ------------------------------------------------------------------ contracts

def _master_path(day: str):
    return FNO_DIR / f"master-{day}.csv"


def load_master(day: Optional[str] = None, fetch: Optional[Callable[[], bytes]] = None) -> List[dict]:
    """Today's NFO stock contracts (FUTSTK, OPTSTK), cached on disk for the day."""
    day = day or sess.now().strftime("%Y-%m-%d")
    p = _master_path(day)
    if not p.exists():
        raw = (fetch or (lambda: urllib.request.urlopen(MASTER_URL, timeout=30).read()))()
        z = zipfile.ZipFile(io.BytesIO(raw))
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
        keep = [l for l in text.splitlines() if ",FUTSTK," in l or ",OPTSTK," in l or l.startswith("Exchange,")]
        FNO_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(keep))
        for old in FNO_DIR.glob("master-*.csv"):
            if old.name != p.name:
                old.unlink()
    rows = []
    for r in csv.DictReader(io.StringIO(p.read_text())):
        try:
            rows.append({"token": r["Token"], "lot": int(r["LotSize"]), "symbol": r["Symbol"], "tsym": r["TradingSymbol"],
                         "expiry": datetime.strptime(r["Expiry"], "%d-%b-%Y").date(), "inst": r["Instrument"],
                         "opt": r["OptionType"] if r["OptionType"] in ("CE", "PE") else None,
                         "strike": D(r["StrikePrice"]) if r["StrikePrice"] not in ("", "-1", "0") else None,
                         "tick": D(r.get("TickSize") or "0.05")})
        except (KeyError, ValueError, ArithmeticError):
            continue
    return rows


class Contracts:
    def __init__(self, rows: List[dict], today: date):
        self.today = today
        self.by_sym: Dict[str, List[dict]] = {}
        for r in rows:
            self.by_sym.setdefault(r["symbol"], []).append(r)

    def _expiry(self, rows: List[dict]) -> Optional[date]:
        exps = sorted({r["expiry"] for r in rows if r["expiry"] >= self.today})
        for e in exps:
            if (e - self.today).days >= 2:                    # roll off a contract about to expire
                return e
        return exps[0] if exps else None

    def future(self, sym: str) -> Optional[dict]:
        rows = [r for r in self.by_sym.get(sym, []) if r["inst"] == "FUTSTK"]
        e = self._expiry(rows)
        return next((r for r in rows if r["expiry"] == e), None)

    def atm_option(self, sym: str, spot: float, side: str) -> Optional[dict]:
        """The near-month at-the-money call (BUY signal) or put (SELL signal)."""
        want = "CE" if side == "B" else "PE"
        rows = [r for r in self.by_sym.get(sym, []) if r["inst"] == "OPTSTK" and r["opt"] == want and r["strike"]]
        e = self._expiry(rows)
        rows = [r for r in rows if r["expiry"] == e]
        if not rows:
            return None
        return min(rows, key=lambda r: abs(float(r["strike"]) - spot))


# ------------------------------------------------------------------ the book

def _now_str(t: float) -> str:
    return datetime.fromtimestamp(t, sess.IST).strftime("%H:%M:%S")


class FnoBook:
    """The F&O sleeves' positions for one day, persisted to disk after every change."""

    def __init__(self, day: str, quote: Callable[[str], dict], contracts: Contracts, write: bool = True):
        self.day, self.quote, self.contracts, self.write = day, quote, contracts, write
        self.st = {"open": {}, "realised": {}, "trades": {}, "wins": {}, "done": {}, "skipped": {}}
        self._load()

    # persistence
    def _path(self):
        return FNO_DIR / f"{self.day}.json"

    def _load(self) -> None:
        try:
            self.st.update(json.loads(self._path().read_text()))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.write:
            return
        FNO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, default=str))
        tmp.replace(self._path())

    def _journal(self, sleeve: str, at: float, **kw) -> None:
        if not self.write:
            return
        import journal
        journal.row(sleeve, self.day, time=_now_str(at), **kw)

    # prices
    def _entry_px(self, c: dict, side: str, spot: float, kind: str) -> tuple:
        """(price, None) or (None, why): a two-sided book, a sane spread, a price that
        fits the stock (26 Nov: a stale few-paise option quote with an empty book
        "bought" hundreds of lots and showed lakhs of fake profit)."""
        try:
            q = self.quote(c["token"])
        except Exception as exc:                                  # noqa: BLE001
            return None, f"no quote for {c['tsym']}: {str(exc)[:60]}"
        bid, ask = (D(str(q.get(k) or 0)) for k in ("bid", "ask"))
        if bid <= 0 or ask <= 0:
            return None, f"{c['tsym']}: no two-sided book (bid {bid}, ask {ask})"
        limit = _d("TRADER_FNO_MAX_SPREAD_FUT", "0.5") if kind == "fut" else _d("TRADER_FNO_MAX_SPREAD_OPT", "5")
        if (ask - bid) / ask > limit / 100:
            return None, f"{c['tsym']}: spread {bid}/{ask} too wide"
        px = ask if side == "B" else bid
        spot_d = D(str(spot))
        if kind == "fut" and abs(px - spot_d) / spot_d > D("0.03"):
            return None, f"{c['tsym']}: future {px} is more than 3% from the stock {spot_d}"
        if kind == "opt" and not (spot_d * D("0.002") <= px <= spot_d * D("0.15")):
            return None, f"{c['tsym']}: premium {px} implausible for an ATM option on {spot_d}"
        return px, None

    def _px(self, token: str, side: str) -> Optional[D]:
        """What a buy (side B) or a sell (S) would fill at now."""
        try:
            q = self.quote(token)
        except Exception:                                         # noqa: BLE001
            return None
        ltp, bid, ask = (D(str(q.get(k) or 0)) for k in ("ltp", "bid", "ask"))
        px = (ask if ask > 0 else ltp) if side == "B" else (bid if bid > 0 else ltp)
        return px if px > 0 else None

    # wallet
    def _used(self, sleeve: str) -> D:
        used = D(0)
        for k, p in self.st["open"].items():
            if k.split("|")[0] == sleeve:
                used += D(p["margin"])
        return used

    def free(self, sleeve: str) -> D:
        return capital() + D(self.st["realised"].get(sleeve, "0")) - self._used(sleeve)

    def day_net(self, sleeve: str, marks: Optional[Dict[str, D]] = None) -> D:
        net = D(self.st["realised"].get(sleeve, "0"))
        for k, p in self.st["open"].items():
            if k.split("|")[0] == sleeve and marks and p["token"] in marks:
                net += self._net(p, marks[p["token"]])
        return net

    @staticmethod
    def _net(p: dict, exit_px: D) -> D:
        qty, entry = int(p["qty"]), D(p["entry"])
        if p["kind"] == "opt":                                    # options are always bought here
            gross = (exit_px - entry) * qty
            return _r(gross - charges("opt", entry * qty, exit_px * qty))
        gross = (exit_px - entry) * qty if p["side"] == "B" else (entry - exit_px) * qty
        buy, sell = (entry * qty, exit_px * qty) if p["side"] == "B" else (exit_px * qty, entry * qty)
        return _r(gross - charges("fut", buy, sell))

    # open / close, driven by the stock trade
    def on_open(self, model: str, sym: str, side: str, spot: float, at: float) -> None:
        if not enabled():
            return
        for kind in SLEEVES:
            sleeve = f"{model}~{kind}"
            key = f"{sleeve}|{sym}"
            if key in self.st["open"] or sleeve in self.st["done"]:
                continue
            c = self.contracts.future(sym) if kind == "fut" else self.contracts.atm_option(sym, spot, side)
            if not c:
                self._skip(sleeve, at, sym, f"no {kind} contract for {sym}")
                continue
            buy_side = side if kind == "fut" else "B"
            px, why = self._entry_px(c, buy_side, spot, kind)
            if px is None:
                self._skip(sleeve, at, sym, why)
                continue
            lot = c["lot"]
            if kind == "fut":
                margin = px * lot * _d("TRADER_FNO_FUT_MARGIN_PCT", "20") / 100
                if margin > self.free(sleeve):
                    self._skip(sleeve, at, sym, f"1 lot of {c['tsym']} needs Rs {margin:.0f} margin, free {self.free(sleeve):.0f}")
                    continue
                lots = 1
            else:
                per_lot = px * lot
                budget = min(_d("TRADER_FNO_OPT_BUDGET", "20000"), self.free(sleeve))
                lots = min(int(budget // per_lot) if per_lot > 0 else 0, int(_d("TRADER_FNO_MAX_LOTS", "5")))
                if lots < 1:
                    if per_lot <= self.free(sleeve):
                        lots = 1
                    else:
                        self._skip(sleeve, at, sym, f"1 lot of {c['tsym']} costs Rs {per_lot:.0f}")
                        continue
                margin = per_lot * lots
            qty = lot * lots
            p = {"kind": kind, "model": model, "sym": sym, "token": c["token"], "tsym": c["tsym"], "side": buy_side,
                 "qty": qty, "lots": lots, "entry": str(px), "margin": str(_r(margin)), "opened": at,
                 "signal": "BUY" if side == "B" else "SELL", "spot": spot}
            self.st["open"][key] = p
            self._journal(sleeve, at, event="open", stock=c["tsym"], side={"B": "BUY", "S": "SELL"}[buy_side], qty=qty,
                          price=str(px), note=f"{p['signal']} signal on {sym} at {spot:.2f}; {lots} lot(s); "
                                              f"{'margin' if kind == 'fut' else 'premium paid'} Rs {_r(margin)}")
        self._save()

    def _skip(self, sleeve: str, at: float, sym: str, why: str) -> None:
        self.st["skipped"][sleeve] = int(self.st["skipped"].get(sleeve, 0)) + 1
        self._journal(sleeve, at, event="skipped", stock=sym, note=why)

    def on_close(self, model: str, sym: str, why: str, at: float) -> None:
        for kind in SLEEVES:
            key = f"{model}~{kind}|{sym}"
            if key in self.st["open"]:
                self._close(key, why, at)
        self._save()

    def _close(self, key: str, why: str, at: float, px: Optional[D] = None) -> None:
        p = self.st["open"][key]
        sleeve = key.split("|")[0]
        exit_side = "S" if p["side"] == "B" else "B"
        px = px if px is not None else self._px(p["token"], exit_side)
        if px is None:
            return                                                # no quote: try again on the next mark
        net = self._net(p, px)
        self.st["open"].pop(key)
        self.st["realised"][sleeve] = str(D(self.st["realised"].get(sleeve, "0")) + net)
        self.st["trades"][sleeve] = int(self.st["trades"].get(sleeve, 0)) + 1
        if net > 0:
            self.st["wins"][sleeve] = int(self.st["wins"].get(sleeve, 0)) + 1
        self._journal(sleeve, at, event="close", stock=p["tsym"], side={"B": "BUY", "S": "SELL"}[exit_side],
                      qty=p["qty"], price=str(px), net=str(net),
                      note=f"{why}; entry {p['entry']}; day so far {D(self.st['realised'][sleeve]):.0f}")

    def mark(self, at: float, flat: bool = False) -> Dict[str, D]:
        """Price every open position; close a sleeve at its day target; close all at 14:50."""
        marks: Dict[str, D] = {}
        for key, p in list(self.st["open"].items()):
            px = self._px(p["token"], "S" if p["side"] == "B" else "B")
            if px is not None:
                marks[p["token"]] = px
            if flat:
                self._close(key, "flat 14:50", at, px)
        tgt = _d("TRADER_BENCH_TARGET", "0")
        if tgt > 0 and not flat:
            for sleeve in {k.split("|")[0] for k in self.st["open"]} | set(self.st["realised"]):
                if sleeve in self.st["done"]:
                    continue
                if self.day_net(sleeve, marks) >= tgt:
                    for key in [k for k in self.st["open"] if k.split("|")[0] == sleeve]:
                        self._close(key, "day target", at, marks.get(self.st["open"][key]["token"]))
                    self.st["done"][sleeve] = _now_str(at)
                    if self.write:
                        import journal
                        journal.note(sleeve, self.day, f"DAY TARGET reached at {_now_str(at)[:5]}: net "
                                     f"{D(self.st['realised'].get(sleeve, '0')):.0f}. Done for the day.")
        self._save()
        return marks

    def summary(self, marks: Optional[Dict[str, D]] = None) -> Dict[str, dict]:
        out = {}
        sleeves = set(self.st["realised"]) | {k.split("|")[0] for k in self.st["open"]} | set(self.st["skipped"])
        for s in sleeves:
            out[s] = {"net": str(_r(self.day_net(s, marks))), "realised": self.st["realised"].get(s, "0"),
                      "trades": int(self.st["trades"].get(s, 0)), "wins": int(self.st["wins"].get(s, 0)),
                      "open_trades": sum(1 for k in self.st["open"] if k.split("|")[0] == s),
                      "skipped": int(self.st["skipped"].get(s, 0)), "target_hit": self.st["done"].get(s)}
        return out


class WrongInstrument(RuntimeError):
    pass


def checked(raw: dict, token: str) -> dict:
    """A quote only if it is for the contract asked for. 26 Nov: Shoonya's GetQuotes for
    the ITC 307.5 put (token 104380) answered with ITC's share quote (token 1660,
    ITC-EQ at 307.55); the sleeves sold Rs 10 options at the share price and showed
    lakhs of profit. A quote for any other token or exchange is no quote."""
    if str(raw.get("token")) != str(token) or str(raw.get("exch", "NFO")).upper() != "NFO":
        raise WrongInstrument(f"asked for NFO {token}, got {raw.get('exch')} {raw.get('token')} {raw.get('tsym')}")
    from shoonya import D as SD
    return {"ltp": SD(raw.get("lp")), "bid": SD(raw.get("bp1")), "ask": SD(raw.get("sp1")), "tsym": raw.get("tsym")}


def live_quote() -> Callable[[str], dict]:
    from shoonya import Shoonya
    b = Shoonya(data_only=True)

    def q(token: str) -> dict:
        try:
            return checked(b.call("GetQuotes", {"exch": "NFO", "token": str(token)}), token)
        except WrongInstrument as exc:
            common.log("paper", {"event": "fno_wrong_quote", "detail": str(exc)[:200]})
            raise
    return q


def models_with_sleeves() -> List[str]:
    import paper
    return [f"{m}~{k}" for m in paper.MODELS for k in SLEEVES]


def probe() -> None:
    rows = load_master()
    c = Contracts(rows, sess.now().date())
    print("contracts", len(rows))
    for sym in ("RELIANCE", "HDFCBANK", "SBIN"):
        f = c.future(sym)
        o = c.atm_option(sym, 1300.0 if sym == "RELIANCE" else 800.0, "B")
        print(sym, "fut", f and (f["tsym"], f["lot"], str(f["expiry"])), "| atm call", o and (o["tsym"], str(o["strike"])))
    from shoonya import Shoonya
    b = Shoonya(data_only=True)
    for sym, spot in (("BEL", 405.0), ("ITC", 308.0)):
        for side in ("B", "S"):
            o = c.atm_option(sym, spot, side)
            if o:
                raw = b.call("GetQuotes", {"exch": "NFO", "token": str(o["token"])})
                print("raw", o["tsym"], o["token"], json.dumps({k: raw.get(k) for k in ("tsym", "token", "lp", "bp1", "sp1", "c", "o", "h", "l", "strprc", "exd", "instname")}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        probe()
