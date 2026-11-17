"""Phone notifications: RFC 8291 encryption the browser can decrypt, a valid
VAPID token, the live position/day cards, and the Sell-now action."""

import json
import os
import shutil
import sys
import unittest
from datetime import datetime
from decimal import Decimal as D

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import NIFTY, FakeBroker, order, position, reset  # noqa: E402

import common  # noqa: E402
import push  # noqa: E402
import session as sess  # noqa: E402

AT = datetime(2026, 11, 17, 11, 0, tzinfo=sess.IST)


def receiver():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, push._b64u(pub), push._b64u(os.urandom(16))


def decrypt(body: bytes, key, auth_b64: str) -> bytes:
    """What the browser does (RFC 8291)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    salt, idlen = body[:16], body[20]
    as_pub = body[21:21 + idlen]
    ct = body[21 + idlen:]
    ua_pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = HKDF(hashes.SHA256(), 32, push._unb64u(auth_b64), b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    pt = AESGCM(cek).decrypt(nonce, ct, None)
    assert pt.endswith(b"\x02")
    return pt[:-1]


class Crypto(unittest.TestCase):
    def setUp(self):
        reset()
        shutil.rmtree(push.push_dir(), ignore_errors=True)

    def test_the_browser_can_decrypt_what_we_send(self):
        key, p256dh, auth = receiver()
        msg = json.dumps({"title": "AXISBANK +Rs 46 net"}).encode()
        self.assertEqual(decrypt(push.encrypt(msg, p256dh, auth), key, auth), msg)

    def test_vapid_token_is_signed_by_our_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        h = push.vapid_header("https://fcm.googleapis.com/fcm/send/abc")
        jwt = h.split("t=", 1)[1].split(",", 1)[0]
        head, claims, sig = jwt.split(".")
        self.assertEqual(json.loads(push._unb64u(claims))["aud"], "https://fcm.googleapis.com")
        raw = push._unb64u(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push._unb64u(push.public_key()))
        pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))   # raises if wrong
        self.assertEqual(oct(os.stat(push.push_dir() / "vapid.pem").st_mode & 0o777), "0o600")

    def test_only_https_subscriptions_with_keys_are_kept(self):
        _, p, a = receiver()
        self.assertFalse(push.subscribe({"endpoint": "http://x", "keys": {"p256dh": p, "auth": a}}))
        self.assertTrue(push.subscribe({"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": p, "auth": a}}))
        self.assertTrue(push.subscribe({"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": p, "auth": a}}))
        self.assertEqual(len(push.subscriptions()), 1, "re-subscribing replaces, never duplicates")


class Cards(unittest.TestCase):
    def setUp(self):
        reset()
        shutil.rmtree(push.push_dir(), ignore_errors=True)
        _, p, a = receiver()
        push.subscribe({"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": p, "auth": a}})
        self.sent = []
        self.orig = push.send
        push.send = lambda m, **k: self.sent.append(m)
        sess.now = lambda clock=None: AT

    def tearDown(self):
        push.send = self.orig

    def test_one_live_card_with_every_position_and_its_sell_button(self):
        from _fake import SENSEX
        common.log("auto", {"event": "placed", "order_id": "P1", "symbol": "NIFTYBEES", "token": NIFTY.token,
                            "side": "B", "qty": 40, "stop": "280.30", "target": "284.00", "why": "ORB break, market UP"})
        self.sent.clear()                               # the order alert itself is tested below
        orders = [order("P1", NIFTY, status="COMPLETE", filled=40, avg="281.00", typ="LMT"),
                  order("S1", NIFTY, side="S", status="TRIGGER_PENDING", trigger="280.30", price="278.90")]
        state = {}
        push.position_cards([position(NIFTY, 40, "281.00"), position(SENSEX, 10, "899.00")], orders,
                            {NIFTY.token: D("282.00"), SENSEX.token: D("901")}, AT, state)
        self.assertEqual([m["type"] for m in self.sent], ["trade", "trade", "live"])
        self.assertIn("ORB break", self.sent[0]["body"], "the fill alert carries the reason")
        card = self.sent[-1]
        self.assertIn("net after charges | 2 open", card["title"])
        first, second, day = card["body"].split("\n")
        for part in ("NIFTYBEES L40", "LTP 282", "SL 280.3", "TP 284"):
            self.assertIn(part, first)
        self.assertTrue(second.startswith("SENSEXIETF L10"))
        self.assertIn("Charges", day)
        self.assertEqual([a["action"] for a in card["actions"]], [f"sell:{NIFTY.token}", f"sell:{SENSEX.token}"])

    def test_one_position_gets_sell_and_exit_all_three_get_exit_all_and_choose(self):
        from _fake import SENSEX
        push.position_cards([position(NIFTY, 40, "281.00")], [], {NIFTY.token: D("282")}, AT, {})
        self.assertEqual([a["action"] for a in self.sent[-1]["actions"]], [f"sell:{NIFTY.token}", "exitall"])
        third = dict(position(SENSEX, 5, "900"), token="999", symbol="THIRD", tsym="THIRD-EQ")
        push.position_cards([position(NIFTY, 40, "281.00"), position(SENSEX, 5, "899"), third], [],
                            {NIFTY.token: D("282")}, AT, {})
        self.assertEqual([a["action"] for a in self.sent[-1]["actions"]], ["exitall", "open"])

    def test_cards_refresh_at_most_once_a_minute_but_a_close_is_at_once(self):
        state = {}
        push.position_cards([position(NIFTY, 40, "281.00")], [], {NIFTY.token: D("282")}, AT, state)
        n = len(self.sent)
        push.position_cards([position(NIFTY, 40, "281.00")], [], {NIFTY.token: D("282.5")}, AT, state)
        self.assertEqual(len(self.sent), n, "no spam every tick")
        closed = position(NIFTY, 0, "281.00", realised="40", buy_qty=40, sell_qty=40, sell_avg="282")
        push.position_cards([closed], [], {}, AT, state)
        self.assertTrue(any(m.get("title", "").startswith("Closed NIFTYBEES") for m in self.sent[n:]))

    def test_a_placed_order_alerts_with_its_reason(self):
        common.log("auto", {"event": "placed", "order_id": "P9", "symbol": "NIFTYBEES", "side": "S", "qty": 10,
                            "trigger": "280.00", "stop": "281", "target": "276", "why": "breakdown below VWAP"})
        self.assertEqual(self.sent[-1]["type"], "trade")
        self.assertIn("SELL 10 NIFTYBEES", self.sent[-1]["title"])
        self.assertIn("breakdown below VWAP", self.sent[-1]["body"])


class SellNow(unittest.TestCase):
    def test_sell_now_closes_at_an_urgent_price(self):
        import dashboard
        reset()
        b = FakeBroker(positions=[position(NIFTY, 40, "281.02")], quotes={NIFTY.token: {"ltp": "282", "bid": "281.95"}})
        orig = dashboard.sh.Shoonya
        dashboard.sh.Shoonya = lambda *a, **k: b
        try:
            out = dashboard.api_action({"action": "exit_now", "token": NIFTY.token})
        finally:
            dashboard.sh.Shoonya = orig
        self.assertTrue(out["ok"], out)
        p = b.placed[0]
        self.assertEqual((p["side"], p["qty"], p["remarks"]), ("S", 40, "now/NIFTYBEES"))
        self.assertLessEqual(p["price"], D("279.14"), "1% through the bid: fills at once")


if __name__ == "__main__":
    unittest.main()
