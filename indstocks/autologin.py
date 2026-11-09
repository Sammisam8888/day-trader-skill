"""
Daily Shoonya login without a browser.

Reproduces exactly what Shoonya's own OAuth login page does (read from its
JavaScript, api.shoonya.com/OAuthlogin, Nov 2026):

  1. POST NorenWClientAPI/QuickAuth   jData={apkversion, uid, pwd=sha256(password),
       factor2=<authenticator code>, appkey=sha256(uid + "|" + seed), imei,
       addldivinf=<user agent>, app_key=<client id>, source="API", vc="NOREN_API"}
     -> {"stat":"Ok","susertoken":...}
  2. POST NorenWClientAPI/GetAuthCode  (text/plain form)
       jData={"app_key":<client id>}&jKey=<susertoken>
     -> redirect to the registered callback URL with ?code=...
  3. exchange the code for today's API token (shoonya.exchange_code)

`seed` and `apkversion` are constants inside the page's bundle. They are
re-read from the live page on every login, so a Shoonya redeploy does not
silently break this; the values found on 2026-11-09 are the fallback.

Safety: a rejected PASSWORD is never retried automatically. Shoonya locks an
account after a few bad passwords, so after one "invalid password" this
refuses to try again until the stored credentials change. A rejected
authenticator code IS retried once with the next code (clock edge).
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import shoonya as sh                                     # noqa: E402

HOST = "https://api.shoonya.com"
LOGIN_PAGE = HOST + "/OAuthlogin/investor-entry-level/login"
API = HOST + "/NorenWClientAPI/"
FALLBACK_SEED = [83, 50, 97, 114, 110, 46, 27, 93]      # decodes to S3cur3!d
FALLBACK_APKVERSION = "W2_20260827"
GUARD = common.STATE_DIR.parent / "autologin_guard.json"
ATTEMPTS = common.STATE_DIR.parent / "autologin_attempts.json"
MAX_ATTEMPTS_PER_DAY = 3
IMEI_FILE = common.STATE_DIR.parent / "autologin_imei"


class AutoLoginError(RuntimeError):
    pass


class BadPassword(AutoLoginError):
    """Do not retry: repeated bad passwords lock the Shoonya account."""


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": sh.USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def decode_seed(codes) -> str:
    return "".join(chr(c + i) for i, c in enumerate(codes))


def page_constants(fetch: Callable[[str], str] = _get) -> Tuple[str, str]:
    """(seed, apkversion) from the live login bundle; fallbacks on any miss."""
    try:
        html = fetch(LOGIN_PAGE)
        main = re.search(r"/OAuthlogin/assets/index-[A-Za-z0-9_-]+\.js", html).group(0)
        js = fetch(HOST + main)
        login = re.search(r"\./Login-[A-Za-z0-9_-]+\.js", js).group(0)
        ljs = fetch(HOST + "/OAuthlogin/assets/" + login[2:])
        imports = dict((loc, pub) for pub, loc in
                       re.findall(r"([\w$]+) as ([\w$]+)", ljs[:ljs.find("from")]))
        # the Login chunk computes appkey from `X` and sends apkversion `ft`
        m = re.search(r"for\(var \w+=await this\.convertor\.sha256\(this\.ipt_pwd\),\w+=this\.userID\+\"\|\","
                      r"\w+=0;\w+<([\w$]+)\.length", ljs)
        a = re.search(r"apkversion:([\w$]+)\.toString\(\)", ljs)
        seed_pub = imports.get(m.group(1)) if m else None
        apk_pub = imports.get(a.group(1)) if a else None
        tail = js[js.rfind("export{"):]
        exports = dict((pub, loc) for loc, pub in re.findall(r"([\w$]+) as ([\w$]+)", tail))
        seed_loc, apk_loc = exports.get(seed_pub), exports.get(apk_pub)
        sm = re.search(r"[,;\s{(]" + re.escape(seed_loc) + r"=new Uint8Array\(\[([\d,\s]+)\]\)", js)
        am = re.search(r"[,;\s{(]" + re.escape(apk_loc) + r"=\"([^\"]+)\"", js)
        seed = decode_seed([int(x) for x in sm.group(1).split(",")])
        return seed, am.group(1)
    except Exception:                                   # noqa: BLE001
        return decode_seed(FALLBACK_SEED), FALLBACK_APKVERSION


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def imei() -> str:
    """A stable device id, like the browser's localStorage NW_appID."""
    try:
        v = IMEI_FILE.read_text().strip()
        if v:
            return v
    except OSError:
        pass
    v = str(uuid.uuid4())
    try:
        IMEI_FILE.parent.mkdir(parents=True, exist_ok=True)
        IMEI_FILE.write_text(v)
    except OSError:
        pass
    return v


def creds_fingerprint(uid: str, pwd: str, totp_secret: str) -> str:
    return sha(uid + "\0" + pwd + "\0" + totp_secret)[:16]


def guard_blocks(fp: str) -> Optional[str]:
    try:
        g = json.loads(GUARD.read_text())
    except (OSError, ValueError):
        return None
    if g.get("fp") == fp:
        return g.get("reason") or "previous attempt was rejected"
    return None


def set_guard(fp: str, reason: str) -> None:
    try:
        GUARD.write_text(json.dumps({"fp": fp, "reason": reason, "at": time.time()}))
        os.chmod(GUARD, 0o600)
    except OSError:
        pass


def clear_guard() -> None:
    try:
        GUARD.unlink()
    except FileNotFoundError:
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# One cookie jar per login, shared by QuickAuth and GetAuthCode, the way a
# browser carries the session cookie from the first response to the second.
JAR = http.cookiejar.CookieJar()
DEBUG = False


def _dbg(*a) -> None:
    if DEBUG:
        print("[debug]", *a, file=sys.stderr, flush=True)


def _post(path: str, body: str, ctype: str, follow: bool = True):
    req = urllib.request.Request(API + path, data=body.encode("utf-8"), method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": sh.USER_AGENT,
                                          "Origin": HOST, "Referer": LOGIN_PAGE,
                                          "Accept": "application/json, text/plain, */*"})
    handlers = [urllib.request.HTTPCookieProcessor(JAR)]
    if not follow:
        handlers.append(_NoRedirect)
    opener = urllib.request.build_opener(*handlers)
    try:
        r = opener.open(req, timeout=20)
        return r.status, dict(r.headers), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace"), exc.geturl()


def quick_auth(uid: str, pwd: str, code2fa: str, client_id: str, seed: str, apk: str,
               post: Callable = _post) -> dict:
    body = {"apkversion": apk, "uid": uid, "pwd": sha(pwd), "factor2": code2fa,
            "appkey": sha(uid + "|" + seed), "imei": imei(), "addldivinf": sh.USER_AGENT,
            "app_key": client_id, "source": "API", "vc": "NOREN_API"}
    status, headers, text, _ = post("QuickAuth", "jData=" + json.dumps(body, separators=(",", ":")),
                                    "application/x-www-form-urlencoded")
    _dbg("QuickAuth", status, "set-cookie:", [c.split("=")[0] for c in str(headers.get("Set-Cookie", "")).split(",") if "=" in c],
         "cookies in jar:", [c.name for c in JAR])
    try:
        j = json.loads(text)
        _dbg("QuickAuth keys:", sorted(j.keys()), "stat:", j.get("stat"), "emsg:", j.get("emsg"))
        return j
    except ValueError:
        raise AutoLoginError(f"QuickAuth HTTP {status}: {text[:200]}")


def _code_from(headers: dict, final: str, text: str) -> Optional[str]:
    for where in (headers.get("Location") or headers.get("location") or "", final or "", text):
        m = re.search(r"[?&]code=([^&\"'\s<]+)", where)
        if m:
            return m.group(1)
    try:
        j = json.loads(text)
        if isinstance(j, dict) and j.get("code"):
            return str(j["code"])
    except ValueError:
        pass
    return None


def auth_code(client_id: str, session_token: str, post: Callable = _post, uid: str = "") -> str:
    """Trade the QuickAuth session for an OAuth code. The browser posts a
    text/plain form; servers differ on the trailing CRLF and on whether the
    key may travel in a header, so the variants are tried in order and the
    first that yields a code wins. Every variant is harmless to repeat."""
    j = json.dumps({"app_key": client_id}, separators=(",", ":"))
    ju = json.dumps({"app_key": client_id, "uid": uid}, separators=(",", ":"))
    variants = [
        ("form text/plain + CRLF (browser)", "jData=" + j + "&jKey=" + session_token + "\r\n", "text/plain"),
        ("text/plain, no CRLF", "jData=" + j + "&jKey=" + session_token, "text/plain"),
        ("urlencoded", "jData=" + j + "&jKey=" + session_token, "application/x-www-form-urlencoded"),
        ("urlencoded with uid", "jData=" + ju + "&jKey=" + session_token, "application/x-www-form-urlencoded"),
    ]
    errors = []
    for name, body, ctype in variants:
        status, headers, text, final = post("GetAuthCode", body, ctype, follow=False)
        code = _code_from(headers, final, text)
        _dbg("GetAuthCode", name, "->", status, "location:", (headers.get("Location") or "")[:120],
             "body:", text[:160].replace(session_token, "<token>"))
        if code:
            return code
        try:
            errors.append(f"{name}: {json.loads(text).get('emsg')}")
        except (ValueError, AttributeError):
            errors.append(f"{name}: HTTP {status}")
    raise AutoLoginError("GetAuthCode refused every format: " + " | ".join(errors))


def count_attempt(day: str) -> int:
    """Attempts made today, including this one. A cap, so a fault can
    never hammer Shoonya's login (which locks accounts) all morning."""
    try:
        a = json.loads(ATTEMPTS.read_text())
    except (OSError, ValueError):
        a = {}
    n = (a.get("n", 0) if a.get("day") == day else 0) + 1
    try:
        ATTEMPTS.write_text(json.dumps({"day": day, "n": n}))
    except OSError:
        pass
    return n


def run(*, force: bool = False, post: Callable = _post, fetch: Callable = _get,
        now: Callable = time.time, sleep: Callable = time.sleep) -> dict:
    uid = common.setting("SHOONYA_USER_ID")
    pwd = common.setting("SHOONYA_PASSWORD")
    secret = common.setting("SHOONYA_TOTP_SECRET")
    client_id = common.setting("SHOONYA_CLIENT_ID")
    if not all((uid, pwd, secret, client_id)):
        raise AutoLoginError("auto-login needs SHOONYA_USER_ID, SHOONYA_PASSWORD, SHOONYA_TOTP_SECRET "
                             "and SHOONYA_CLIENT_ID; run scripts/set-autologin.sh")
    if not force and sh.load_token():
        return {"ok": True, "skipped": "already logged in today"}
    fp = creds_fingerprint(uid, pwd, secret)
    blocked = guard_blocks(fp)
    if blocked:
        raise BadPassword(f"not retrying: {blocked}. Fix the password with scripts/set-autologin.sh")

    import session as sess
    n = count_attempt(sess.now().strftime("%Y-%m-%d"))
    if n > MAX_ATTEMPTS_PER_DAY and not force:
        raise AutoLoginError(f"{MAX_ATTEMPTS_PER_DAY} auto-login attempts already today; log in from the dashboard")

    seed, apk = page_constants(fetch)
    last = ""
    for attempt in range(2):
        # never use a code in its last 4 seconds: it may expire in flight
        left = 30 - (now() % 30)
        if left < 4:
            sleep(left + 0.5)
        code = sh.totp(secret, at=now())
        resp = quick_auth(uid, pwd, code, client_id, seed, apk, post=post)
        if resp.get("stat") == "Ok" and resp.get("susertoken"):
            clear_guard()
            c = auth_code(client_id, resp["susertoken"], post=post, uid=uid)
            rec = sh.exchange_code(c)
            common.log("login", {"event": "logged_in", "uid": rec["uid"], "how": "auto"})
            return {"ok": True, "uid": rec["uid"]}
        last = str(resp.get("emsg") or resp)
        low = last.lower()
        if "otp" in low or "factor" in low or "2fa" in low or "totp" in low:
            sleep(30 - (now() % 30) + 1)                 # next code, once
            continue
        if "password" in low or "pwd" in low or "blocked" in low or "locked" in low:
            set_guard(fp, last)
            common.log("login", {"event": "login_failed", "reason": last, "action": "not retrying"})
            common.notify("Shoonya auto-login refused", f"{last}. Not retrying until the password is updated.",
                          priority="urgent")
            raise BadPassword(last)
        break
    common.log("login", {"event": "login_failed", "reason": last})
    common.notify("Shoonya auto-login failed", f"{last}. Log in from the dashboard.", priority="high")
    raise AutoLoginError(last)


def configured() -> bool:
    return all(common.setting(k) for k in ("SHOONYA_USER_ID", "SHOONYA_PASSWORD",
                                           "SHOONYA_TOTP_SECRET", "SHOONYA_CLIENT_ID"))


if __name__ == "__main__":
    import session as sess
    force = "--force" in sys.argv
    DEBUG = "--debug" in sys.argv
    if not force and sess.session_for(sess.now()) is None:
        print(json.dumps({"ok": True, "skipped": "not a trading day"}))
        raise SystemExit(0)
    try:
        print(json.dumps(run(force=force)))
    except AutoLoginError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        raise SystemExit(1)
