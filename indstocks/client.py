"""
INDstocks HTTP client with a per-category rate-limit governor.

The published limits differ sharply by category, and the one that bites a
day-trading loop is Data at 5/sec with a 100,000/day cap -- not the Order
limit everyone looks at first. A bar-polling watcher spends the data budget,
so the governor is what keeps an entry watcher from starving the rest of the
session.

  Order APIs        10/sec
  Data APIs          5/sec, 100,000/day   (instruments, historical, option chain)
  Quote APIs         5/sec, 100,000/day
  Non-Trading APIs  15/sec, 100,000/day   (profile, funds, order book)

Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# Siblings are loaded by path (the repo's tests/_load.py convention) and also
# imported directly by the server, so put this folder on the path either way
# rather than relying on the caller's cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import BASE_URL, AuthError, TokenManager  # type: ignore  # noqa: E402


class RateLimited(RuntimeError):
    pass


class DailyBudgetExhausted(RuntimeError):
    pass


# category -> (per second, per day or None)
# "data" is deliberately BELOW the documented 5/sec. At 5 the historical
# endpoint starts answering success:true with empty candle arrays instead of
# 429, which is indistinguishable from a stock that did not trade. Slower and
# correct beats fast and silently blind.
_DATA_RPS = int(os.environ.get("INDSTOCKS_DATA_RPS", "2"))

LIMITS = {
    "order": (10, None),
    "data": (_DATA_RPS, 100_000),
    "quote": (5, 100_000),
    "non_trading": (15, 100_000),
}

# Which category each path belongs to. Longest prefix wins.
ROUTES = (
    ("/smart/order", "order"),
    ("/order", "order"),
    ("/market/instruments", "data"),
    ("/market/historical", "data"),
    ("/market/option-chain", "data"),
    ("/market/quotes", "quote"),
    ("/portfolio", "non_trading"),
    ("/order-book", "non_trading"),
    ("/trade-book", "non_trading"),
    ("/trades", "non_trading"),
    ("/user", "non_trading"),
    ("/funds", "non_trading"),
    ("/margin", "non_trading"),
)


def category_for(path: str) -> str:
    best = ("", "non_trading")
    for prefix, cat in ROUTES:
        if path.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, cat)
    return best[1]


class Governor:
    """Token bucket per category, plus a daily counter.

    Blocks rather than raising on the per-second limit: a short wait is
    always better than a 429, which on the order path could mean a missed
    entry. The daily cap raises, because sleeping it off is never right.
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep, day_clock=time.time):
        self._clock = clock
        self._sleep = sleep
        self._day_clock = day_clock
        self._lock = threading.Lock()
        self._allowance = {c: float(r) for c, (r, _) in LIMITS.items()}
        self._last = {c: clock() for c in LIMITS}
        self._used_today = {c: 0 for c in LIMITS}
        self._day = self._today()

    def _today(self) -> int:
        return int(self._day_clock() // 86400)

    def acquire(self, category: str) -> None:
        rate, daily = LIMITS[category]
        while True:
            with self._lock:
                if self._today() != self._day:
                    self._day = self._today()
                    self._used_today = {c: 0 for c in LIMITS}

                if daily is not None and self._used_today[category] >= daily:
                    raise DailyBudgetExhausted(
                        f"{category} APIs: {daily}/day budget is spent"
                    )

                now = self._clock()
                elapsed = now - self._last[category]
                self._last[category] = now
                self._allowance[category] = min(
                    float(rate), self._allowance[category] + elapsed * rate
                )
                if self._allowance[category] >= 1.0:
                    self._allowance[category] -= 1.0
                    self._used_today[category] += 1
                    return
                wait = (1.0 - self._allowance[category]) / rate
            self._sleep(wait)

    def used_today(self, category: str) -> int:
        return self._used_today[category]


class Client:
    def __init__(self, tokens: Optional[TokenManager] = None,
                 base_url: str = BASE_URL, governor: Optional[Governor] = None,
                 opener=None, max_retries: int = 2):
        self.tokens = tokens or TokenManager()
        self.base_url = base_url.rstrip("/")
        self.governor = governor or Governor()
        self._opener = opener or urllib.request.urlopen
        self.max_retries = max_retries

    def request(self, method: str, path: str, *, params: Optional[dict] = None,
                body: Optional[dict] = None, raw: bool = False):
        category = category_for(path)
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        attempt = 0
        while True:
            self.governor.acquire(category)
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    **self.tokens.auth_header(),
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                method=method,
            )
            try:
                with self._opener(req, timeout=30) as resp:
                    payload = resp.read()
                return payload if raw else json.loads(payload.decode())
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:400]

                if "1010" in detail or "cloudflare" in detail.lower():
                    raise AuthError(
                        "blocked at the edge (Cloudflare 1010), not by INDstocks. "
                        "Check INDSTOCKS_USER_AGENT."
                    ) from exc

                # A replaced or revoked token: mint one and retry once.
                if exc.code in (401, 403) and "TokenException" in detail \
                        and attempt < self.max_retries:
                    self.tokens.generate()
                    attempt += 1
                    continue

                if exc.code == 429 and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(2 ** attempt)
                    continue

                if exc.code == 429:
                    raise RateLimited(f"429 after {attempt} retries: {detail}") from exc

                raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail}") from exc

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)
