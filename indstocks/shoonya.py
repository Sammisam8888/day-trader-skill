"""
Shoonya (Finvasia) adapter: OAuth login, the NorenOMS REST calls, and a
normalised view of funds, positions, orders, quotes and candles.

Everything else in this package talks to the broker ONLY through this file,
so a broker quirk is fixed in one place.

Contract, from https://shoonya.com/api-documentation (Nov 2026):

  Login     OAuth only. Browser -> /OAuthlogin/authorize/oauth?client_id=..
            -> redirect with ?code=.. (single use, ~2-3 min) -> POST
            GenAcsTok jData={"code","checksum"} where checksum =
            sha256(client_id + secret_code + code). Returns `susertoken`,
            valid until the nightly settlement (~23:30 IST).
  Calls     POST <base>/<Endpoint>, body jData=<json>&jKey=<token>.
            The docs disagree on where the token goes (the structure page
            says an Authorization: Bearer header, every endpoint page says
            jKey in the body), so both are sent. Neither harms the other.
  Envelope  {"stat": "Ok" | "Not_Ok", "emsg": ...}. HTTP 200 either way:
            always check `stat`. Book endpoints answer an empty account
            with Not_Ok "no data", which is not an error.
  Orders    prctyp LMT or SL-LMT only. MARKET IS REJECTED, so every exit
            is a marketable limit priced through the touch. prd "I" is
            intraday (MIS). Bracket and cover orders are not supported.
  Static IP Every call must come from the IP registered on the API Key
            page (13.127.164.72 for this box).
  Algo ID   SEBI: API orders from an automated strategy carry an
            exchange-issued Algo ID (`algo_id`), from SHOONYA_ALGO_ID.

Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session as sess                                  # noqa: E402
from market import Candle, to_tick                      # noqa: E402

BASE = os.environ.get("SHOONYA_BASE", "https://api.shoonya.com/NorenWClientAPI/")
AUTHORIZE = "https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={client_id}"
STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto")).parent
TOKEN_FILE = Path(os.environ.get("SHOONYA_TOKEN_FILE", str(STATE_DIR / "shoonya_token.json")))
INSTR_CACHE = Path(os.environ.get("SHOONYA_INSTR_CACHE", str(STATE_DIR / "shoonya_instruments.json")))
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# How far through the touch a "market" exit is priced. Shoonya has no market
# order; a sell limit this far under the bid fills like one on a liquid ETF
# and still refuses to sell into a vacuum.
MARKETABLE_PCT = Decimal(os.environ.get("TRADER_MARKETABLE_PCT", "0.3"))

PENDING = ("OPEN", "TRIGGER_PENDING", "PENDING")
FILLED = "COMPLETE"
DEAD = ("CANCELED", "CANCELLED", "REJECTED")


class ShoonyaError(RuntimeError):
    pass


class NetworkError(ShoonyaError):
    """The request may or may not have reached Shoonya."""


class LoginRequired(ShoonyaError):
    """No valid session. Someone has to log in (dashboard -> Log in)."""


def env(key: str, default: str = "") -> str:
    """Read fresh from the env file, so credentials saved on the dashboard
    apply without restarting the services."""
    import common
    return common.setting(key, default)


def D(x, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x if x not in (None, "") else default))
    except InvalidOperation:
        return Decimal(default)


def I(x) -> int:
    try:
        return int(Decimal(str(x or 0)))
    except InvalidOperation:
        return 0


# ------------------------------------------------------------------ auth

def checksum(client_id: str, secret: str, code: str) -> str:
    return hashlib.sha256((client_id.strip() + secret.strip() + code.strip())
                          .encode("utf-8")).hexdigest()


def authorize_url() -> str:
    cid = env("SHOONYA_CLIENT_ID")
    if not cid:
        raise LoginRequired("SHOONYA_CLIENT_ID is not set; run scripts/setup-shoonya.sh")
    return AUTHORIZE.format(client_id=cid)


def totp(secret_b32: str, *, at: Optional[float] = None, step: int = 30,
         digits: int = 6) -> str:
    """RFC 6238, HMAC-SHA1: what an authenticator app shows."""
    s = secret_b32.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    key = base64.b32decode(s, casefold=True)
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def session_cutoff(now: datetime) -> datetime:
    """Tokens die in the nightly settlement window. A token minted before
    today's 06:30 IST is treated as yesterday's."""
    return datetime.combine(now.date(), datetime.min.time().replace(hour=6, minute=30),
                            tzinfo=sess.IST)


def load_token(now: Optional[datetime] = None) -> Optional[dict]:
    try:
        rec = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    now = now or sess.now()
    try:
        at = datetime.fromisoformat(rec["at"])
    except (KeyError, ValueError):
        return None
    if at < session_cutoff(now) or not rec.get("token"):
        return None
    return rec


def save_token(token: str, uid: str, actid: str) -> dict:
    rec = {"token": token, "uid": uid, "actid": actid,
           "at": sess.now().isoformat(timespec="seconds")}
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, TOKEN_FILE)
    return rec


def clear_token() -> None:
    try:
        TOKEN_FILE.unlink()
    except FileNotFoundError:
        pass


def extract_code(text: str) -> str:
    """Accept a bare code or the whole redirected URL pasted from a browser."""
    text = text.strip()
    if "code=" in text:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(text).query)
        if q.get("code"):
            return q["code"][0].strip()
        return text.split("code=", 1)[1].split("&", 1)[0].strip()
    return text


def _http(endpoint: str, body: str, token: Optional[str] = None) -> object:
    headers = {"Content-Type": "text/plain", "User-Agent": USER_AGENT,
               "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + endpoint, data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise ShoonyaError(f"{endpoint}: HTTP {exc.code}: {raw[:200]}") from exc
    except (urllib.error.URLError, OSError) as exc:     # timeout, reset, DNS: the call's fate is unknown
        raise NetworkError(f"{endpoint}: network: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ShoonyaError(f"{endpoint}: not JSON: {raw[:200]}") from exc


def exchange_code(code: str, http: Callable = _http) -> dict:
    """Trade a one-time OAuth code for today's session token."""
    cid, secret = env("SHOONYA_CLIENT_ID"), env("SHOONYA_SECRET_CODE")
    if not cid or not secret:
        raise LoginRequired("Client ID / Secret Code not set; run scripts/setup-shoonya.sh")
    code = extract_code(code)
    body = "jData=" + json.dumps({"code": code, "checksum": checksum(cid, secret, code)},
                                 separators=(",", ":"))
    resp = http("GenAcsTok", body, None)
    if not isinstance(resp, dict) or resp.get("stat") != "Ok" or not resp.get("susertoken"):
        emsg = resp.get("emsg") if isinstance(resp, dict) else str(resp)[:200]
        raise ShoonyaError(f"login failed: {emsg}")
    uid = resp.get("uid") or resp.get("USERID") or env("SHOONYA_USER_ID")
    actid = resp.get("actid") or uid
    return save_token(resp["susertoken"], uid, actid)


# ------------------------------------------------------------------ data

@dataclass(frozen=True)
class Instrument:
    symbol: str          # NIFTYBEES
    tsym: str            # NIFTYBEES-EQ
    token: str
    exch: str
    tick: Decimal
    lot: int


def norm_order(r: dict) -> dict:
    status = str(r.get("status") or "").upper()
    return {
        "id": str(r.get("norenordno") or ""),
        "symbol": str(r.get("tsym") or "").replace("-EQ", ""),
        "tsym": r.get("tsym") or "", "token": str(r.get("token") or ""),
        "exch": r.get("exch") or "NSE",
        "side": str(r.get("trantype") or "").upper(),            # B / S
        "type": str(r.get("prctyp") or "").upper(),              # LMT / SL-LMT
        "product": str(r.get("prd") or "").upper(),              # I / C / M
        "status": status,
        "pending": status in PENDING,
        "dead": status in DEAD,
        "qty": I(r.get("qty")), "filled": I(r.get("fillshares")),
        "avg": D(r.get("avgprc")), "price": D(r.get("prc")), "trigger": D(r.get("trgprc")),
        "remarks": str(r.get("remarks") or ""),
        "reason": r.get("rejreason") or "",
        "time": r.get("norentm") or r.get("exch_tm") or "",
    }


def norm_position(r: dict) -> dict:
    return {
        "symbol": str(r.get("tsym") or "").replace("-EQ", ""),
        "tsym": r.get("tsym") or "", "token": str(r.get("token") or ""),
        "exch": r.get("exch") or "NSE",
        "product": str(r.get("prd") or "").upper(),
        "qty": I(r.get("netqty")), "avg": D(r.get("netavgprc")),
        "ltp": D(r.get("lp")),
        "realised": D(r.get("rpnl")), "unrealised": D(r.get("urmtom")),
        "buy_qty": I(r.get("daybuyqty")), "buy_avg": D(r.get("daybuyavgprc")),
        "sell_qty": I(r.get("daysellqty")), "sell_avg": D(r.get("daysellavgprc")),
        "tick": D(r.get("ti"), "0"),           # 0 = unknown: instrument_for asks the instrument cache
    }


def parse_candles(rows, interval: int) -> List[Candle]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("stat") not in (None, "Ok"):
            continue
        try:
            t = datetime.strptime(r["time"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=sess.IST)
            out.append(Candle(t, D(r["into"]), D(r["inth"]), D(r["intl"]), D(r["intc"]),
                              I(r.get("intv") or r.get("v")), interval))
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda c: c.opens_at)
    return out


class Shoonya:
    """One logged-in session. Raises LoginRequired when there is none."""

    MIN_GAP = {"order": 0.12, "data": 0.35}

    def __init__(self, *, token: Optional[dict] = None, http: Callable = _http,
                 sleep: Callable = time.sleep):
        self._tok = token
        self._http = http
        self._sleep = sleep
        self._last = {"order": 0.0, "data": 0.0}

    # -- plumbing ---------------------------------------------------------

    @property
    def tok(self) -> dict:
        if self._tok is None:
            self._tok = load_token()
        if not self._tok:
            raise LoginRequired("not logged in to Shoonya today")
        return self._tok

    @property
    def uid(self) -> str:
        return self.tok["uid"]

    @property
    def actid(self) -> str:
        return self.tok.get("actid") or self.uid

    def call(self, endpoint: str, payload: dict, *, kind: str = "data",
             empty_ok: bool = False):
        tok = self.tok["token"]
        body = ("jData=" + json.dumps({"uid": self.uid, **payload}, separators=(",", ":"))
                + "&jKey=" + tok)
        for attempt in range(4):
            gap = self.MIN_GAP[kind] - (time.monotonic() - self._last[kind])
            if gap > 0:
                self._sleep(gap)
            self._last[kind] = time.monotonic()
            resp = self._http(endpoint, body, tok)
            if isinstance(resp, dict) and resp.get("stat") == "Not_Ok":
                emsg = str(resp.get("emsg") or "")
                low = emsg.lower()
                if "session" in low and ("expired" in low or "invalid" in low):
                    clear_token()
                    self._tok = None
                    raise LoginRequired(f"Shoonya session ended: {emsg}")
                if low.startswith("rate_limited") and attempt < 3:
                    self._sleep(0.5 * (2 ** attempt))
                    continue
                if empty_ok and "no data" in low:
                    return []
                raise ShoonyaError(f"{endpoint}: {emsg}")
            return resp
        raise ShoonyaError(f"{endpoint}: rate limited after retries")

    # -- account ----------------------------------------------------------

    def limits(self) -> dict:
        return self.call("Limits", {"actid": self.actid})

    def cash(self) -> Decimal:
        """Account cash for sizing: opening cash plus today's pay-in. The
        leverage cap is applied against this, not against broker margin."""
        lim = self.limits()
        return D(lim.get("cash")) + D(lim.get("payin")) - D(lim.get("payout"))

    def free_margin(self) -> Decimal:
        """Margin Shoonya will still let us use now: cash plus pay-in, less pay-out
        and everything already blocked (open positions AND pending orders)."""
        lim = self.limits()
        return D(lim.get("cash")) + D(lim.get("payin")) - D(lim.get("payout")) - D(lim.get("marginused"))

    def order_margin(self, inst: "Instrument", *, side: str, qty: int, price: Decimal,
                     trigger: Optional[Decimal] = None) -> Optional[Decimal]:
        """The margin Shoonya would block for this order (GetOrderMargin), or None."""
        payload = {"actid": self.actid, "exch": inst.exch, "tsym": inst.tsym, "qty": str(qty),
                   "prc": str(to_tick(price, inst.tick)), "prd": "I", "trantype": side,
                   "prctyp": "SL-LMT" if trigger is not None else "LMT"}
        if trigger is not None:
            payload["trgprc"] = str(to_tick(trigger, inst.tick))
        try:
            resp = self.call("GetOrderMargin", payload)
        except ShoonyaError:
            return None
        m = D(resp.get("ordermargin")) if isinstance(resp, dict) else Decimal(0)
        return m if m > 0 else None

    def positions(self, *, intraday_only: bool = True) -> List[dict]:
        rows = self.call("PositionBook", {"actid": self.actid}, empty_ok=True)
        out = [norm_position(r) for r in rows if isinstance(r, dict)]
        return [p for p in out if p["product"] == "I"] if intraday_only else out

    def orders(self) -> List[dict]:
        rows = self.call("OrderBook", {}, empty_ok=True)
        return [norm_order(r) for r in rows if isinstance(r, dict)]

    # -- market -----------------------------------------------------------

    def quote(self, token: str, exch: str = "NSE") -> dict:
        q = self.call("GetQuotes", {"exch": exch, "token": str(token)})
        return {"ltp": D(q.get("lp")), "uc": D(q.get("uc")), "lc": D(q.get("lc")),
                "bid": D(q.get("bp1")), "ask": D(q.get("sp1")),
                "open": D(q.get("o")), "high": D(q.get("h")), "low": D(q.get("l")),
                "volume": I(q.get("v")), "tick": D(q.get("ti"), "0")}

    def ltps(self, tokens: List[str], exch: str = "NSE") -> Dict[str, Decimal]:
        out = {}
        for t in dict.fromkeys(str(x) for x in tokens if x):
            try:
                out[t] = self.quote(t, exch)["ltp"]
            except ShoonyaError:
                continue
        return out

    def search(self, text: str, exch: str = "NSE") -> List[dict]:
        resp = self.call("SearchScrip", {"exch": exch, "stext": text}, empty_ok=True)
        return (resp.get("values") or []) if isinstance(resp, dict) else []

    def instrument(self, symbol: str, exch: str = "NSE") -> Instrument:
        symbol = symbol.strip().upper()
        cache = _load_cache()
        key = f"{exch}:{symbol}"
        if key in cache:
            c = cache[key]
            return Instrument(symbol, c["tsym"], c["token"], exch, D(c["tick"], "0.01"), I(c["lot"]) or 1)
        rows = self.search(symbol, exch)
        pick = None
        for want in (f"{symbol}-EQ", symbol):
            pick = next((r for r in rows if str(r.get("tsym", "")).upper() == want), None)
            if pick:
                break
        if pick is None:
            raise KeyError(f"{symbol} not found on {exch}")
        inst = Instrument(symbol, pick["tsym"], str(pick["token"]), exch,
                          D(pick.get("ti"), "0.01"), I(pick.get("ls")) or 1)
        cache[key] = {"tsym": inst.tsym, "token": inst.token, "tick": str(inst.tick), "lot": inst.lot}
        _save_cache(cache)
        return inst

    def candles(self, token: str, start: datetime, end: datetime, *,
                interval: int = 5, exch: str = "NSE") -> List[Candle]:
        rows = self.call("TPSeries", {"exch": exch, "token": str(token),
                                      "st": str(int(start.timestamp())),
                                      "et": str(int(end.timestamp())),
                                      "intrv": str(interval)}, empty_ok=True)
        return parse_candles(rows, interval)

    # -- orders -----------------------------------------------------------

    def place(self, inst: Instrument, *, side: str, qty: int, price: Decimal,
              trigger: Optional[Decimal] = None, remarks: str = "") -> str:
        if side not in ("B", "S"):
            raise ShoonyaError(f"side must be B or S, not {side!r}")
        if qty <= 0:
            raise ShoonyaError("quantity must be positive")
        payload = {"actid": self.actid, "exch": inst.exch, "tsym": inst.tsym,
                   "qty": str(qty), "dscqty": "0", "prc": str(to_tick(price, inst.tick)),
                   "prd": "I", "trantype": side,
                   "prctyp": "SL-LMT" if trigger is not None else "LMT",
                   "ret": "DAY", "ordersource": "API", "remarks": remarks[:60]}
        if trigger is not None:
            payload["trgprc"] = str(to_tick(trigger, inst.tick))
        algo = env("SHOONYA_ALGO_ID")
        if algo:
            payload["algo_id"] = algo
        try:
            resp = self.call("PlaceOrder", payload, kind="order")
        except NetworkError:
            # It may have been placed. Look before anyone retries: a blind retry
            # doubles the order, a blind give-up leaves an untracked one.
            self._sleep(1.5)
            found = self._find_just_placed(inst, side, qty, remarks[:60])
            if found:
                return found
            raise
        oid = str(resp.get("norenordno") or "")
        if not oid:
            raise ShoonyaError(f"PlaceOrder returned no order number: {resp}")
        return oid

    def _find_just_placed(self, inst: Instrument, side: str, qty: int, remarks: str) -> Optional[str]:
        try:
            rows = self.orders()
        except ShoonyaError:
            return None
        now = sess.now()
        for o in reversed(rows):
            if o["tsym"] != inst.tsym or o["side"] != side or o["qty"] != qty or o["remarks"] != remarks:
                continue
            try:
                t = datetime.strptime(o["time"], "%H:%M:%S %d-%m-%Y").replace(tzinfo=sess.IST)
            except ValueError:
                continue
            if abs((now - t).total_seconds()) <= 30:
                return o["id"]
        return None

    def modify(self, order: dict, inst: Instrument, *, qty: int, price: Decimal,
               trigger: Optional[Decimal] = None) -> str:
        payload = {"norenordno": order["id"], "exch": inst.exch, "tsym": inst.tsym,
                   "qty": str(qty), "prc": str(to_tick(price, inst.tick)),
                   "prctyp": "SL-LMT" if trigger is not None else "LMT",
                   "ret": "DAY", "ordersource": "API"}
        if trigger is not None:
            payload["trgprc"] = str(to_tick(trigger, inst.tick))
        resp = self.call("ModifyOrder", payload, kind="order")
        return str(resp.get("result") or order["id"])

    def cancel(self, order_id: str) -> None:
        self.call("CancelOrder", {"norenordno": str(order_id), "ordersource": "API"},
                  kind="order")

    # -- helpers ----------------------------------------------------------

    def marketable(self, inst: Instrument, side: str, q: Optional[dict] = None) -> Decimal:
        """A limit price that crosses the spread now, inside the circuit."""
        q = q or self.quote(inst.token, inst.exch)
        pct = MARKETABLE_PCT / 100
        if side == "S":
            ref = q["bid"] if q["bid"] > 0 else q["ltp"]
            px = to_tick(ref * (1 - pct), inst.tick, mode="down")
            return max(px, q["lc"]) if q["lc"] > 0 else px
        ref = q["ask"] if q["ask"] > 0 else q["ltp"]
        px = to_tick(ref * (1 + pct), inst.tick, mode="up")
        return min(px, q["uc"]) if q["uc"] > 0 else px

    def instrument_for(self, row: dict) -> Instrument:
        """An Instrument for a position or order row (both carry tsym/token)."""
        tick = D(row.get("tick"), "0") or _tick_from_cache(row)
        if not tick:
            try:                                        # a wrong tick gets every exit rejected (L&T is 0.10)
                tick = self.quote(row["token"], row.get("exch") or "NSE")["tick"]
            except ShoonyaError:
                tick = None
        return Instrument(row["symbol"], row["tsym"], row["token"], row.get("exch") or "NSE",
                          tick or Decimal("0.01"), 1)


def _load_cache() -> dict:
    try:
        data = json.loads(INSTR_CACHE.read_text(encoding="utf-8"))
        if data.get("_day") == sess.now().strftime("%Y-%m-%d"):
            return data
    except (OSError, ValueError):
        pass
    return {"_day": sess.now().strftime("%Y-%m-%d")}


def _save_cache(data: dict) -> None:
    try:
        INSTR_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INSTR_CACHE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _tick_from_cache(row: dict) -> Optional[Decimal]:
    for k, v in _load_cache().items():
        if isinstance(v, dict) and v.get("token") == row.get("token"):
            return D(v.get("tick"), "0.01")
    return None
