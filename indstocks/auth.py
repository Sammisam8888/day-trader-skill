"""
INDstocks token manager.

The access token is a SHARED SINGLETON. Every successful call to
/generate/token invalidates the token the previous call returned, so two
processes each minting their own will keep killing each other's and you see
it as random TokenException 403s mid-session. One writer, one cache on disk,
everything else reads it.

Rules this encodes, from indstocks/vendor/api-docs.md:
  - token lives 24h; generate once per session, reuse
  - at most 1 generation per 60 seconds (throttle, rejected otherwise)
  - 5 wrong TOTP codes in a rolling 15 min -> 15 min lockout
  - 3 lockouts in an hour -> 1 hour lockout
  - a lockout does NOT revoke a live token, deliberately
  - x-api-key carries the Client ID for /generate/token ONLY
  - the response field is `token`, not `access_token`

Credentials are read from the environment, never from arguments: argv is
visible in `ps` to every user on the box.

Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

BASE_URL = "https://api.indstocks.com"
CACHE_PATH = Path(os.environ.get("INDSTOCKS_TOKEN_CACHE", "/var/lib/indstocks/token.json"))

TOKEN_TTL = 24 * 60 * 60
REFRESH_MARGIN = 60 * 60       # refresh when under an hour remains
MIN_GENERATE_GAP = 60          # server throttle: 1 token per 60s


class AuthError(RuntimeError):
    pass


class LockedOut(AuthError):
    """Too many bad TOTP codes. Back off; a live token keeps working."""


# ---------------------------------------------------------------- TOTP

def totp(secret_b32: str, *, at: Optional[float] = None, step: int = 30,
         digits: int = 6) -> str:
    """RFC 6238 TOTP, HMAC-SHA1. Matches what an authenticator app shows."""
    s = secret_b32.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)  # base32 needs padding; apps usually strip it
    try:
        key = base64.b32decode(s, casefold=True)
    except Exception as exc:
        raise AuthError("TOTP secret is not valid base32") from exc

    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def seconds_until_next_code(*, at: Optional[float] = None, step: int = 30) -> float:
    now = time.time() if at is None else at
    return step - (now % step)


# ---------------------------------------------------------------- cache

def _read_cache() -> Optional[dict]:
    try:
        with CACHE_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_cache(data: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    # 0600 before any content is written: the token is a bearer credential.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, CACHE_PATH)  # atomic: a reader never sees a half file


# ---------------------------------------------------------------- client

class TokenManager:
    def __init__(self, client_id: Optional[str] = None, mpin: Optional[str] = None,
                 totp_secret: Optional[str] = None, base_url: str = BASE_URL,
                 opener=None):
        self.client_id = client_id or os.environ.get("INDSTOCKS_CLIENT_ID", "")
        self.mpin = mpin or os.environ.get("INDSTOCKS_MPIN", "")
        self.totp_secret = totp_secret or os.environ.get("INDSTOCKS_TOTP_SECRET", "")
        self.base_url = base_url.rstrip("/")
        self._opener = opener or urllib.request.urlopen

    def _require_creds(self) -> None:
        missing = [n for n, v in (
            ("INDSTOCKS_CLIENT_ID", self.client_id),
            ("INDSTOCKS_MPIN", self.mpin),
            ("INDSTOCKS_TOTP_SECRET", self.totp_secret),
        ) if not v]
        if missing:
            raise AuthError(
                "missing credentials: " + ", ".join(missing)
                + " -- run scripts/set-creds.sh on the server"
            )

    def token(self, *, force: bool = False) -> str:
        """A live token. Reuses the cached one until it is close to expiry."""
        cached = _read_cache()
        if cached and not force:
            age = time.time() - cached.get("issued_at", 0)
            if age < TOKEN_TTL - REFRESH_MARGIN:
                return cached["token"]

        if cached:
            since = time.time() - cached.get("issued_at", 0)
            if since < MIN_GENERATE_GAP:
                # Server would reject this anyway; the cached token is still
                # valid, so hand it back rather than burning the throttle.
                return cached["token"]

        return self.generate()

    def generate(self) -> str:
        """Mint a new token. INVALIDATES any previously issued one."""
        self._require_creds()

        # Don't send a code about to roll over: it may expire in flight and
        # each failure counts toward the 5-attempt lockout.
        if seconds_until_next_code() < 2:
            time.sleep(2)

        body = json.dumps({"mpin": self.mpin, "totp": totp(self.totp_secret)}).encode()
        req = urllib.request.Request(
            self.base_url + "/generate/token",
            data=body,
            headers={
                # Client ID goes here, NOT in Authorization, for this endpoint only.
                "x-api-key": self.client_id,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with self._opener(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code == 429 or "lock" in detail.lower():
                raise LockedOut(
                    f"token generation refused ({exc.code}): {detail}\n"
                    "Back off for the full window and check the server clock "
                    "(timedatectl) -- drift over about a minute fails every code."
                ) from exc
            raise AuthError(f"token generation failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"could not reach {self.base_url}: {exc.reason}") from exc

        # The docs are explicit: the field is `token`, not `access_token`.
        token = (payload.get("data") or payload).get("token") if isinstance(payload, dict) else None
        if not token:
            raise AuthError(f"no `token` field in response: {str(payload)[:200]}")

        _write_cache({"token": token, "issued_at": time.time()})
        return token

    def auth_header(self) -> dict:
        return {"Authorization": self.token()}
