"""
Web Push to the owner's phone (Brave/Chrome on Android, over the private
Tailscale HTTPS address), so trades and live P&L arrive as notifications
even with the dashboard closed.

  keys      a VAPID key pair made on first use, kept in STATE/../push (0600)
  subscribe the dashboard registers a service worker and POSTs its push
            subscription to /api/push/subscribe
  send      RFC 8291 (aes128gcm) encryption + RFC 8292 VAPID JWT, then a
            POST to the browser's push service; gone subscriptions (404/410)
            are dropped

What is sent (see web/public/sw.js for how it is shown):
  trade     an order placed, a fill, an exit, with the one-line reason
  live      ONE card, replaced in place (tag "live"): today's net after
            charges, then a line per open position (net after charges, LTP,
            stop, target), with Sell buttons (Android allows 2: one per
            position for 1-2 positions, else Exit all + Choose)

Sending never blocks the caller (a background thread) and never raises.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import common

CONTACT = "https://github.com/Sammisam8888/day-trader-skill"


def push_dir() -> Path:
    return common.STATE_DIR.parent / "push"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------------ keys

def _private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    p = push_dir() / "vapid.pem"
    try:
        return serialization.load_pem_private_key(p.read_bytes(), password=None)
    except (OSError, ValueError):
        key = ec.generate_private_key(ec.SECP256R1())
        p.parent.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        return key


def _raw_public(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """The applicationServerKey the browser subscribes with."""
    return _b64u(_raw_public(_private_key()))


# ------------------------------------------------------------------ subscriptions

def _subs_path() -> Path:
    return push_dir() / "subscriptions.json"


def subscriptions() -> List[dict]:
    try:
        return json.loads(_subs_path().read_text())
    except (OSError, ValueError):
        return []


def _save_subs(subs: List[dict]) -> None:
    p = _subs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def subscribe(sub: dict) -> bool:
    ep = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return False
    subs = [s for s in subscriptions() if s.get("endpoint") != ep]
    subs.append({"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}})
    _save_subs(subs[-10:])
    return True


# ------------------------------------------------------------------ crypto

def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: Optional[bytes] = None,
            server_key=None) -> bytes:
    """RFC 8291 aes128gcm body for one subscription."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    ua_pub = _unb64u(p256dh)
    auth_secret = _unb64u(auth)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw_public(as_key)
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def vapid_header(endpoint: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = _private_key()
    u = urlparse(endpoint)
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                               "sub": CONTACT}, separators=(",", ":")).encode())
    der = key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={public_key()}"


# ------------------------------------------------------------------ send

def _post(sub: dict, data: bytes, ttl: int, urgency: str, topic: Optional[str]) -> int:
    body = encrypt(data, sub["keys"]["p256dh"], sub["keys"]["auth"])
    headers = {"Authorization": vapid_header(sub["endpoint"]), "Content-Encoding": "aes128gcm",
               "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency}
    if topic:
        headers["Topic"] = topic[:32]                    # the push service keeps only the latest per topic
    req = urllib.request.Request(sub["endpoint"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def send(message: dict, *, ttl: int = 120, urgency: str = "high", wait: bool = False) -> None:
    """Deliver one message to every subscription, in the background."""
    subs = subscriptions()
    if not subs or common.setting("TRADER_PUSH", "true").lower() not in ("1", "true", "yes"):
        return
    data = json.dumps(message, separators=(",", ":"), default=str).encode()
    topic = "".join(ch for ch in str(message.get("tag") or "") if ch.isalnum() or ch in "-_")[:32] or None

    def work():
        gone = []
        for s in subs:
            try:
                code = _post(s, data, ttl, urgency, topic if message.get("type") in ("live",) else None)
                if code in (404, 410):
                    gone.append(s["endpoint"])
            except Exception:                           # noqa: BLE001
                pass
        if gone:
            try:
                _save_subs([s for s in subscriptions() if s["endpoint"] not in gone])
            except OSError:
                pass

    t = threading.Thread(target=work, daemon=False)
    t.start()
    if wait:
        t.join(15)


# ------------------------------------------------------------------ what to send

def rs(x) -> str:
    try:
        v = Decimal(str(x))
    except ArithmeticError:
        return str(x)
    return ("+" if v > 0 else "-" if v < 0 else "") + f"Rs {abs(v):,.0f}"


def trade_placed(rec: dict) -> None:
    """An entry order went to the exchange (from the scan's log)."""
    side = "BUY" if rec.get("side", "B") == "B" else "SELL"
    how = f"trigger {rec['trigger']}" if rec.get("trigger") else f"limit {rec.get('entry')}"
    send({"type": "trade", "tag": f"trade-{rec.get('order_id')}", "renotify": True,
          "title": f"{side} {rec.get('qty')} {rec.get('symbol')} ({how})",
          "body": f"SL {rec.get('stop')} | TP {rec.get('target')} | {str(rec.get('why') or '')[:160]}"})


def position_cards(positions: List[dict], orders: List[dict], ltps: Dict[str, Decimal], now, state: dict) -> None:
    """Live cards from the fill monitor, about once a minute, and at once on
    a fill or an exit."""
    if not subscriptions():
        return
    import metrics
    import profitlock
    held = {str(p["token"]): p for p in positions if p["qty"] != 0}
    prev = state.setdefault("push_held", {})
    placements = common.events("placed")
    adjusted = {}
    for r in common.events("plan_adjusted"):
        if r.get("target"):
            adjusted[str(r.get("parent"))] = r["target"]

    # opened / closed since the last tick
    for tok, p in held.items():
        if tok not in prev:
            pl = next((x for x in reversed(placements) if str(x.get("token")) == tok), {})
            side = "Bought" if p["qty"] > 0 else "Shorted"
            send({"type": "trade", "tag": f"fill-{tok}-{now:%H%M%S}", "renotify": True,
                  "title": f"{side} {abs(p['qty'])} {p['symbol']} @ {p['avg']}",
                  "body": str(pl.get("why") or "position opened")[:180]})
    for tok, was in prev.items():
        if tok not in held:
            closed = next((x for x in positions if str(x["token"]) == tok), None)
            net = None
            if closed:
                try:
                    net = metrics.closed_trades([closed])[0]["net"]
                except (IndexError, KeyError):
                    net = None
            send({"type": "trade", "tag": f"closed-{tok}-{now:%H%M%S}", "renotify": True,
                  "title": f"Closed {was['symbol']}" + (f": {rs(net)} net today on it" if net is not None else ""),
                  "body": "Position closed (stop, target, exit or flatten)."})
    changed = set(held) != set(prev)
    state["push_held"] = {t: {"symbol": p["symbol"], "qty": p["qty"]} for t, p in held.items()}

    last = state.get("push_at", 0.0)
    if not changed and time.monotonic() - last < 60:
        return
    state["push_at"] = time.monotonic()

    lines, actions = [], []
    for tok, p in held.items():
        side = "B" if p["qty"] > 0 else "S"
        ltp = Decimal(str(ltps.get(tok) or p.get("ltp") or p["avg"]))
        qty = abs(int(p["qty"]))
        net = profitlock.net_open(side, Decimal(str(p["avg"])), qty, ltp)
        stop = next((o["trigger"] for o in orders if o["pending"] and str(o["token"]) == tok
                     and o["side"] != side and o.get("type") == "SL-LMT"), None)
        pl = next((x for x in reversed(placements) if str(x.get("token")) == tok), {})
        tgt = adjusted.get(str(pl.get("order_id"))) or pl.get("target")
        lines.append(f"{p['symbol']} {'L' if side == 'B' else 'S'}{qty} {rs(net)} | LTP {ltp} | "
                     f"SL {stop or '-'} | TP {tgt or '-'}")
        actions.append({"action": f"sell:{tok}", "title": f"{'Sell' if side == 'B' else 'Cover'} {p['symbol']}"})
    try:
        day = metrics.pnl(positions, ltps, orders)
    except Exception:                                   # noqa: BLE001
        return
    # Android shows at most 2 buttons on a web notification: one per position
    # when there are 1-2 (plus Exit all for a single one), else Exit all + Choose.
    if len(actions) == 1:
        actions.append({"action": "exitall", "title": "Exit all now"})
    elif len(actions) > 2:
        actions = [{"action": "exitall", "title": "Exit all now"}, {"action": "open", "title": "Choose..."}]
    lines.append(f"Realised {rs(day['realised'])} | Open {rs(day['open'])} | Charges Rs {day['charges']:,.0f}")
    send({"type": "live", "tag": "live", "silent": not changed, "sticky": bool(held),
          "title": f"Today {rs(day['net'])} net after charges" + (f" | {len(held)} open" if held else " | flat"),
          "body": "\n".join(lines), "actions": actions if held else []},
         ttl=90, urgency="normal" if not changed else "high")
