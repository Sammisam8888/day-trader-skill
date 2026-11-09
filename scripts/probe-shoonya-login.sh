#!/usr/bin/env bash
# Read-only: fetch Shoonya's OAuth login app from this box and print the
# code around its login calls, to automate the daily login without a
# browser. Sends no credentials.
set -uo pipefail
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
B=https://api.shoonya.com/OAuthlogin
D=$(mktemp -d)
curl -sS -A "$UA" "$B/investor-entry-level/login" -o "$D/p.html"
MAIN=$(grep -oE '/OAuthlogin/assets/index-[a-z0-9]+\.js' "$D/p.html" | head -1)
curl -sS -A "$UA" "https://api.shoonya.com$MAIN" -o "$D/main.js"
echo "== config files referenced"; grep -oE '"[^"]*\.json"' "$D/main.js" | sort -u | head
for chunk in $(grep -oE '\./(Login|Auth)-[a-z0-9]+\.js' "$D/main.js" | sort -u); do
  curl -sS -A "$UA" "$B/assets/${chunk#./}" -o "$D/c.js"
  echo; echo "==== $chunk ($(wc -c < "$D/c.js") bytes)"
  python3 - "$D/c.js" <<'PY'
import re, sys
s = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for kw in ("GetAuthCode", "QuickAuth", "pwd", "factor2", "sha256", "SHA256", "encrypt", "otp", "apkversion",
           "source", "imei", "vc", "appkey", "redirect", "code", "post(", "fetch("):
    for m in list(re.finditer(re.escape(kw), s))[:3]:
        a, b = max(0, m.start() - 220), min(len(s), m.end() + 260)
        print(f"--- {kw} @{m.start()}: ...{s[a:b]}...")
PY
done
echo; echo "==== main.js around wcApi / GetAuthCode / post helper"
python3 - "$D/main.js" <<'PY'
import re, sys
s = open(sys.argv[1], encoding="utf-8", errors="replace").read()
for kw in ("wcApi", "GetAuthCode", "jData", "jKey", "NorenWClient", "QuickAuth", "sha256", "createHash", "encryptotp"):
    for m in list(re.finditer(re.escape(kw), s))[:3]:
        a, b = max(0, m.start() - 200), min(len(s), m.end() + 240)
        print(f"--- {kw} @{m.start()}: ...{s[a:b]}...")
PY
rm -rf "$D"
