import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

from _load import load

_m = load("indstocks", "auth")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestTOTP(unittest.TestCase):
    # RFC 6238 test vectors, SHA1, seed "12345678901234567890"
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def test_rfc6238_vectors(self):
        for at, expected in ((59, "287082"), (1111111109, "081804"),
                             (1111111111, "050471"), (1234567890, "005924")):
            self.assertEqual(_m.totp(self.SECRET, at=at), expected, at)

    def test_unpadded_secret_accepted(self):
        # Authenticator apps usually show the key without '=' padding.
        self.assertEqual(_m.totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59), "287082")

    def test_lowercase_and_spaces_accepted(self):
        spaced = "gezd gnbv gy3t qojq gezd gnbv gy3t qojq"
        self.assertEqual(_m.totp(spaced, at=59), "287082")

    def test_bad_secret_is_a_clear_error(self):
        with self.assertRaises(_m.AuthError):
            _m.totp("not!base32!")

    def test_code_rolls_on_the_30s_boundary(self):
        self.assertEqual(_m.totp(self.SECRET, at=29), _m.totp(self.SECRET, at=0))
        self.assertNotEqual(_m.totp(self.SECRET, at=30), _m.totp(self.SECRET, at=0))

    def test_seconds_until_next_code(self):
        self.assertAlmostEqual(_m.seconds_until_next_code(at=10), 20, places=5)


class TestTokenManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = _m.CACHE_PATH
        _m.CACHE_PATH = Path(self.tmp.name) / "token.json"

    def tearDown(self):
        _m.CACHE_PATH = self._old
        self.tmp.cleanup()

    def mgr(self, opener=None):
        return _m.TokenManager(client_id="CID", mpin="1234",
                               totp_secret=TestTOTP.SECRET, opener=opener)

    def test_missing_credentials_names_them(self):
        m = _m.TokenManager(client_id="", mpin="", totp_secret="")
        with self.assertRaises(_m.AuthError) as cm:
            m.generate()
        for name in ("INDSTOCKS_CLIENT_ID", "INDSTOCKS_MPIN", "INDSTOCKS_TOTP_SECRET"):
            self.assertIn(name, str(cm.exception))

    def test_reads_token_field_not_access_token(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            return FakeResp(json.dumps({"status": "success",
                                        "data": {"token": "TKN-1"}}).encode())

        self.assertEqual(self.mgr(opener).generate(), "TKN-1")
        self.assertEqual(calls[0].get_header("X-api-key"), "CID")
        self.assertIsNone(calls[0].headers.get("Authorization"))

    def test_flat_payload_also_accepted(self):
        def opener(req, timeout=None):
            return FakeResp(json.dumps({"token": "TKN-FLAT"}).encode())

        self.assertEqual(self.mgr(opener).generate(), "TKN-FLAT")

    def test_cached_token_is_reused_not_regenerated(self):
        hits = []

        def opener(req, timeout=None):
            hits.append(1)
            return FakeResp(json.dumps({"token": "TKN-%d" % len(hits)}).encode())

        m = self.mgr(opener)
        self.assertEqual(m.token(), "TKN-1")
        self.assertEqual(m.token(), "TKN-1")
        self.assertEqual(m.token(), "TKN-1")
        self.assertEqual(len(hits), 1, "a second call would invalidate the first token")

    def test_near_expiry_triggers_refresh(self):
        def opener(req, timeout=None):
            return FakeResp(json.dumps({"token": "FRESH"}).encode())

        _m._write_cache({"token": "OLD", "issued_at": time.time() - (23.5 * 3600)})
        self.assertEqual(self.mgr(opener).token(), "FRESH")

    def test_throttle_window_returns_cache_instead_of_calling(self):
        def opener(req, timeout=None):
            raise AssertionError("must not call /generate/token inside the 60s gap")

        # Past refresh margin but only 10s old: server would reject, cache is fine.
        _m._write_cache({"token": "RECENT", "issued_at": time.time() - 10})
        m = self.mgr(opener)
        m.token()  # within TTL, returns cache
        self.assertEqual(m.token(), "RECENT")

    def test_lockout_is_its_own_error(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(
                "u", 429, "Too Many Requests", {},
                io.BytesIO(b'{"message":"account locked for 15 minutes"}'))

        with self.assertRaises(_m.LockedOut) as cm:
            self.mgr(opener).generate()
        self.assertIn("clock", str(cm.exception).lower())

    def test_sends_a_non_urllib_user_agent(self):
        seen = {}

        def opener(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return FakeResp(json.dumps({"token": "T"}).encode())

        self.mgr(opener).generate()
        self.assertNotIn("urllib", (seen["ua"] or "").lower(),
                         "the edge rejects Python-urllib with Cloudflare 1010")

    def test_cloudflare_block_is_not_reported_as_a_credential_problem(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {},
                                         io.BytesIO(b"error code: 1010"))

        with self.assertRaises(_m.AuthError) as cm:
            self.mgr(opener).generate()
        msg = str(cm.exception).lower()
        self.assertIn("not a credential problem", msg)
        self.assertIn("user_agent", msg.replace("-", "_"))

    def test_auth_header_carries_the_user_agent_too(self):
        def opener(req, timeout=None):
            return FakeResp(json.dumps({"token": "T"}).encode())

        h = self.mgr(opener).auth_header()
        self.assertEqual(h["Authorization"], "T")
        self.assertIn("User-Agent", h)

    def test_missing_token_field_is_an_error(self):
        def opener(req, timeout=None):
            return FakeResp(json.dumps({"status": "success", "data": {}}).encode())

        with self.assertRaises(_m.AuthError):
            self.mgr(opener).generate()

    def test_cache_file_is_private(self):
        def opener(req, timeout=None):
            return FakeResp(json.dumps({"token": "TKN"}).encode())

        self.mgr(opener).generate()
        self.assertEqual(os.stat(_m.CACHE_PATH).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
