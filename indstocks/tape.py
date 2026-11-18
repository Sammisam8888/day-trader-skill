"""
The tape: every tick and the 5-level order book for the trading list, live
from Shoonya's websocket, recorded and summarised every second.

  record    STATE_DIR/tape/DAY/SYMBOL.jsonl, one line per feed update
            (time, ltp, cumulative volume, best bid/ask and their sizes,
            total buy/sell quantity waiting). Replayable later: this is
            the second-by-second history the back-tests do not have yet.
  features  STATE_DIR/tape/live.json, rewritten every second, per symbol:
              chg_1m/chg_5m   % move over the last 1 and 5 minutes
              vol_1m, vol_x   shares traded in the last minute, and that
                              minute against the average minute today
              upticks         share of the last minute's price changes up
              imbalance       buy qty waiting / (buy + sell), 0..1
              spread_bps      ask - bid, in basis points of the price
              momentum        a -1..1 summary of the above
            The fill monitor and the AI read this; nothing here trades.

Market data only. Read-only. Uses today's login token; no password.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import session as sess  # noqa: E402

WS_URL = os.environ.get("SHOONYA_WS", "wss://api.shoonya.com/NorenWSAPI/")
TAPE_DIR = common.STATE_DIR / "tape"
LIVE_FILE = TAPE_DIR / "live.json"


# ------------------------------------------------------------ websocket (RFC 6455, client side)

class WS:
    """The smallest client that does the job: TLS, text frames, ping/pong."""

    def __init__(self, url: str, timeout: float = 10.0):
        u = urlparse(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("closed during handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str, opcode: int = 1) -> None:
        self.sock.sendall(encode_frame(text.encode(), opcode))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> Optional[str]:
        """The next text message (None on a control frame)."""
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op, ln = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(ln)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 8:
                raise ConnectionError("server closed")
            if op == 9:
                self.sock.sendall(encode_frame(data, 10))
                return None
            if op == 10:
                return None
            msg += data
            if fin:
                return msg.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def encode_frame(payload: bytes, opcode: int = 1) -> bytes:
    """A masked client frame."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


# ------------------------------------------------------------ features

def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Book:
    """One symbol's live state and its last few minutes of ticks."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state: Dict[str, float] = {}
        self.ticks: deque = deque()          # (epoch, ltp, cum_volume)
        self.first: Optional[tuple] = None   # (epoch, cum_volume) of the day's first update

    def update(self, msg: dict, at: float) -> dict:
        for k in ("lp", "v", "bp1", "sp1", "bq1", "sq1", "tbq", "tsq", "o", "h", "l", "c", "ap"):
            if k in msg:
                v = _f(msg[k])
                if v is not None:
                    self.state[k] = v
        lp, v = self.state.get("lp"), self.state.get("v")
        if lp is not None:
            if self.first is None and v is not None:
                self.first = (at, v)
            self.ticks.append((at, lp, v if v is not None else 0.0))
            while self.ticks and at - self.ticks[0][0] > 330:
                self.ticks.popleft()
        return {"t": round(at, 3), **{k: self.state.get(k) for k in ("lp", "v", "bp1", "sp1", "bq1", "sq1", "tbq", "tsq")}}

    def features(self, now: float) -> Optional[dict]:
        lp = self.state.get("lp")
        if lp is None or not self.ticks:
            return None

        def at_or_before(sec):
            ref = None
            for t, p, v in self.ticks:
                if t <= now - sec:
                    ref = (t, p, v)
                else:
                    break
            return ref or self.ticks[0]

        _, p1, v1 = at_or_before(60)
        _, p5, _ = at_or_before(300)
        vol_now = self.state.get("v") or 0.0
        vol_1m = max(0.0, vol_now - v1)
        avg_min = None
        if self.first and now - self.first[0] > 120:
            avg_min = (vol_now - self.first[1]) / ((now - self.first[0]) / 60)
        last = [p for t, p, _ in self.ticks if t >= now - 60]
        ups = sum(1 for a, b in zip(last, last[1:]) if b > a)
        downs = sum(1 for a, b in zip(last, last[1:]) if b < a)
        upticks = ups / (ups + downs) if ups + downs else 0.5
        tbq, tsq = self.state.get("tbq"), self.state.get("tsq")
        imb = tbq / (tbq + tsq) if tbq is not None and tsq and tbq + tsq > 0 else None
        bp, sp = self.state.get("bp1"), self.state.get("sp1")
        spread = (sp - bp) / lp * 10000 if bp and sp and lp else None
        chg_1m = (lp - p1) / p1 * 100 if p1 else 0.0
        chg_5m = (lp - p5) / p5 * 100 if p5 else 0.0
        vol_x = vol_1m / avg_min if avg_min else None
        return {"ltp": lp, "vol_cum": vol_now, "chg_1m": round(chg_1m, 3), "chg_5m": round(chg_5m, 3),
                "vol_1m": int(vol_1m), "vol_x": round(vol_x, 2) if vol_x is not None else None,
                "upticks": round(upticks, 2), "imbalance": round(imb, 3) if imb is not None else None,
                "spread_bps": round(spread, 1) if spread is not None else None,
                "momentum": momentum(chg_1m, upticks, imb, vol_x), "updated": round(now, 1)}


def momentum(chg_1m: float, upticks: float, imbalance: Optional[float], vol_x: Optional[float]) -> float:
    """-1 (selling hard) .. +1 (buying hard). Direction from price and ticks,
    confirmation from the book, weight from volume."""
    price = max(-1.0, min(1.0, chg_1m / 0.3))            # 0.3% in a minute is a strong move
    ticks = (upticks - 0.5) * 2
    book = (imbalance - 0.5) * 2 if imbalance is not None else 0.0
    raw = 0.45 * price + 0.35 * ticks + 0.20 * book
    weight = 1.0 if vol_x is None else max(0.5, min(1.5, vol_x))
    return round(max(-1.0, min(1.0, raw * weight)), 3)


def read_live(max_age_s: float = 10.0) -> Dict[str, dict]:
    """The latest features per symbol, only those fresher than max_age_s."""
    try:
        d = json.loads(LIVE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {s: f for s, f in d.get("symbols", {}).items() if now - (f.get("updated") or 0) <= max_age_s}


# ------------------------------------------------------------ the recorder

def connect_msg(tok: dict) -> str:
    """OAuth websocket login (Shoonya's NorenRestApiOAuth 0.0.41): t=a with the
    OAuth access token; the session token is the fallback for an older login file."""
    return json.dumps({"t": "a", "uid": tok["uid"], "actid": tok["uid"],
                       "accesstoken": tok.get("access") or tok["token"], "source": "API"})


def symbols() -> list:
    syms = [s.strip().upper() for s in common.setting("TRADER_SYMBOLS", "NIFTYBEES").split(",") if s.strip()]
    for s in ("NIFTYBEES", "BANKBEES"):
        if s not in syms:
            syms.append(s)
    # extra symbols recorded for the paper bench and research only (never traded by the AI)
    for s in common.setting("TRADER_TAPE_EXTRA", "").split(","):
        s = s.strip().upper()
        if s and s not in syms:
            syms.append(s)
    return syms


def _write_live(books: Dict[str, Book], now: float) -> None:
    out = {"at": now, "symbols": {}}
    for s, b in books.items():
        f = b.features(now)
        if f:
            out["symbols"][s] = f
    tmp = LIVE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(LIVE_FILE)


def session_once(stop_at: float) -> None:
    from shoonya import Shoonya, load_token
    tok = load_token()
    if not tok:
        raise RuntimeError("no login token yet today")
    b = Shoonya()
    by_token = {}
    for s in symbols():
        try:
            by_token[str(b.instrument(s).token)] = s
        except Exception:                                    # noqa: BLE001
            continue
    books = {s: Book(s) for s in by_token.values()}
    day = sess.now().strftime("%Y-%m-%d")
    out_dir = TAPE_DIR / day
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {s: open(out_dir / f"{s}.jsonl", "a", encoding="utf-8") for s in books}
    ws = WS(WS_URL)
    try:
        ws.send(connect_msg(tok))
        ack = ws.recv() or ""
        try:
            a = json.loads(ack)
        except ValueError:
            a = {}
        if a.get("t") not in ("ak", "ck") or str(a.get("s", "")).upper() != "OK":
            raise ConnectionError(f"connect refused: {ack[:200]}")
        ws.send(json.dumps({"t": "d", "k": "#".join(f"NSE|{t}" for t in by_token)}))
        common.log("tape", {"event": "tape_connected", "symbols": len(books)})
        ws.sock.settimeout(5)
        last_live = last_beat = last_flush = 0.0
        while time.time() < stop_at:
            try:
                raw = ws.recv()
            except socket.timeout:
                raw = None
            now = time.time()
            if raw:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = {}
                sym = by_token.get(str(msg.get("tk", "")))
                if sym and msg.get("t") in ("dk", "df", "tk", "tf"):
                    rec = books[sym].update(msg, now)
                    files[sym].write(json.dumps(rec, separators=(",", ":")) + "\n")
            if now - last_live >= 1.0:
                _write_live(books, now)
                last_live = now
            if now - last_beat >= 3:
                ws.send(json.dumps({"t": "h"}), opcode=9)       # ping, as Shoonya's own client does
                last_beat = now
            if now - last_flush >= 5:
                for fh in files.values():
                    fh.flush()
                last_flush = now
    finally:
        ws.close()
        for fh in files.values():
            fh.close()


def main() -> None:
    backoff = 5
    while True:
        now = sess.now()
        if now.weekday() >= 5 or not (now.hour * 60 + now.minute >= 9 * 60 + 5) or now.hour * 60 + now.minute >= 15 * 60 + 35:
            time.sleep(60)
            continue
        end = now.replace(hour=15, minute=35, second=0, microsecond=0).timestamp()
        try:
            session_once(end)
            backoff = 5
        except Exception as e:                               # noqa: BLE001
            common.log("tape", {"event": "tape_error", "error": str(e)[:300]})
            time.sleep(backoff)
            backoff = min(120, backoff * 2)


def check() -> None:
    """Connect, subscribe, print the first few feed messages, exit. Read-only."""
    from shoonya import Shoonya, load_token
    tok = load_token()
    if not tok:
        print("tape check: no login token today"); return
    inst = Shoonya().instrument("NIFTYBEES")
    ws = WS(WS_URL)
    try:
        ws.send(connect_msg(tok))
        print("ack:", (ws.recv() or "")[:200])
        ws.send(json.dumps({"t": "d", "k": f"NSE|{inst.token}"}))
        ws.sock.settimeout(8)
        for _ in range(3):
            try:
                m = ws.recv()
            except socket.timeout:
                print("no feed within 8 s"); break
            if m:
                print("feed:", m[:300])
    finally:
        ws.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        check()
    else:
        main()
