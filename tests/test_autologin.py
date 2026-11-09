"""Auto-login: constants read from the live bundle, the exact request
shapes, and the guards that keep a bad password from locking the account."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _fake  # noqa: E402
from _fake import reset  # noqa: E402

import autologin as al  # noqa: E402
import shoonya as sh  # noqa: E402

MAIN = ('var a=1,$n=new Uint8Array([83,50,97,114,110,46,27,93]),Li=["C"];const q=0,mn="W2_20260827",Me="WEB";'
        'x("./Login-60d2e1a6.js");export{m0 as $,$n as j,mn as k};')
LOGIN = ('import{E as q,j as X,k as ft}from"./index-82ea259a.js";async login(t){for(var e=await this.convertor.sha256(this.ipt_pwd),'
         'd=this.userID+"|",p=0;p<X.length;p++)d+=String.fromCharCode(X[p]+p);let n={apkversion:ft.toString(),uid:1}}')


def fetch(url):
    if url.endswith("/investor-entry-level/login"):
        return '<script type="module" crossorigin src="/OAuthlogin/assets/index-82ea259a.js"></script>'
    if url.endswith("index-82ea259a.js"):
        return MAIN
    if url.endswith("Login-60d2e1a6.js"):
        return LOGIN
    raise AssertionError(url)


class Post:
    def __init__(self, *quick):
        self.quick = list(quick)
        self.calls = []

    def __call__(self, path, body, ctype, follow=True):
        self.calls.append((path, body, ctype, follow))
        if path == "QuickAuth":
            return 200, {}, json.dumps(self.quick.pop(0)), ""
        if path == "GetAuthCode":
            return 302, {"Location": "http://100.127.19.72:8080/shoonya/callback?code=CODE9"}, "", ""
        raise AssertionError(path)


def env(**kw):
    base = dict(SHOONYA_USER_ID="FN245398", SHOONYA_PASSWORD="pw", SHOONYA_TOTP_SECRET="JBSWY3DPEHPK3PXP",
                SHOONYA_CLIENT_ID="FN245398_U", SHOONYA_SECRET_CODE="SEC")
    base.update(kw)
    reset(**base)
    for f in (al.GUARD, al.ATTEMPTS):
        f.unlink(missing_ok=True)
    sh.clear_token()


class Constants(unittest.TestCase):
    def test_seed_decodes(self):
        self.assertEqual(al.decode_seed(al.FALLBACK_SEED), "S3cur3!d")

    def test_constants_are_read_from_the_live_bundle(self):
        self.assertEqual(al.page_constants(fetch), ("S3cur3!d", "W2_20260827"))

    def test_a_changed_bundle_is_followed(self):
        changed = MAIN.replace("[83,50,97,114,110,46,27,93]", "[65,65]").replace("W2_20260827", "W3_X")
        f = lambda u: changed if u.endswith("index-82ea259a.js") else fetch(u)
        self.assertEqual(al.page_constants(f), ("AB", "W3_X"))

    def test_fallback_when_the_page_is_unreadable(self):
        def broken(url):
            raise OSError("down")
        self.assertEqual(al.page_constants(broken), ("S3cur3!d", "W2_20260827"))


class Flow(unittest.TestCase):
    def setUp(self):
        env()
        self.exchanged = []
        self.orig = sh.exchange_code
        sh.exchange_code = lambda code: (self.exchanged.append(code) or sh.save_token("TOK", "FN245398", "FN245398"))

    def tearDown(self):
        sh.exchange_code = self.orig

    def run_al(self, post, **kw):
        return al.run(force=True, post=post, fetch=fetch, now=lambda: 1_700_000_005.0, sleep=lambda s: None, **kw)

    def test_quickauth_body_matches_the_browser(self):
        post = Post({"stat": "Ok", "susertoken": "SESS"})
        self.assertTrue(self.run_al(post)["ok"])
        path, body, ctype, _ = post.calls[0]
        self.assertEqual((path, ctype), ("QuickAuth", "application/x-www-form-urlencoded"))
        j = json.loads(body[len("jData="):])
        self.assertEqual(j["pwd"], al.sha("pw"))
        self.assertEqual(j["appkey"], al.sha("FN245398|S3cur3!d"))
        self.assertEqual((j["app_key"], j["source"], j["vc"], j["apkversion"]),
                         ("FN245398_U", "API", "NOREN_API", "W2_20260827"))
        self.assertEqual(j["factor2"], sh.totp("JBSWY3DPEHPK3PXP", at=1_700_000_005.0))

    def test_auth_code_is_taken_from_the_redirect_and_exchanged(self):
        post = Post({"stat": "Ok", "susertoken": "SESS"})
        self.run_al(post)
        path, body, ctype, follow = post.calls[1]
        self.assertEqual((path, ctype, follow), ("GetAuthCode", "text/plain", False))
        self.assertTrue(body.startswith('jData={"app_key":"FN245398_U"}&jKey=SESS'))
        self.assertEqual(self.exchanged, ["CODE9"])

    def test_a_bad_otp_is_retried_once(self):
        post = Post({"stat": "Not_Ok", "emsg": "Invalid OTP"}, {"stat": "Ok", "susertoken": "S"})
        self.assertTrue(self.run_al(post)["ok"])
        self.assertEqual([c[0] for c in post.calls], ["QuickAuth", "QuickAuth", "GetAuthCode"])

    def test_a_bad_password_is_never_retried(self):
        post = Post({"stat": "Not_Ok", "emsg": "Invalid Password"})
        with self.assertRaises(al.BadPassword):
            self.run_al(post)
        with self.assertRaises(al.BadPassword):
            self.run_al(Post())
        self.assertEqual(len(post.calls), 1, "the second run never reached Shoonya")

    def test_new_credentials_lift_the_guard(self):
        with self.assertRaises(al.BadPassword):
            self.run_al(Post({"stat": "Not_Ok", "emsg": "Invalid Password"}))
        env(SHOONYA_PASSWORD="fixed")
        self.assertTrue(self.run_al(Post({"stat": "Ok", "susertoken": "S"}))["ok"])

    def test_daily_attempt_cap(self):
        for _ in range(3):
            with self.assertRaises(al.AutoLoginError):
                al.run(post=Post({"stat": "Not_Ok", "emsg": "Server busy"}), fetch=fetch,
                       now=lambda: 1_700_000_005.0, sleep=lambda s: None)
        with self.assertRaisesRegex(al.AutoLoginError, "attempts already today"):
            al.run(post=Post(), fetch=fetch, now=lambda: 1_700_000_005.0, sleep=lambda s: None)

    def test_already_logged_in_is_a_no_op(self):
        sh.save_token("T", "FN245398", "FN245398")
        post = Post()
        self.assertEqual(al.run(post=post, fetch=fetch)["skipped"], "already logged in today")
        self.assertEqual(post.calls, [])

    def test_missing_credentials(self):
        env(SHOONYA_PASSWORD="")
        with self.assertRaisesRegex(al.AutoLoginError, "set-autologin"):
            al.run(force=True, post=Post(), fetch=fetch)


if __name__ == "__main__":
    unittest.main()
