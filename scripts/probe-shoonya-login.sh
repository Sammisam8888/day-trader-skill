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
LOGIN=$(grep -oE '\./Login-[a-z0-9]+\.js' "$D/main.js" | head -1)
curl -sS -A "$UA" "$B/assets/${LOGIN#./}" -o "$D/c.js"
python3 - "$D/c.js" "$D/main.js" <<'PY2'
import re, sys
s = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = open(sys.argv[2], encoding="utf-8", errors="replace").read()
print("== import header:", s[:700])
i = s.find("let r={app_key:")
print("== after QuickAuth Ok:", s[i:i + 1500])
for name in ("X", "ft", "gt", "ut"):
    for mm in list(re.finditer(r"[,;\s(]" + name + r"=", s))[:2]:
        print(f"== def {name}:", s[mm.start():mm.start() + 400])
k = m.find("getAuthCode")
print("== main getAuthCode uses:", [m[x.start() - 300:x.start() + 300] for x in re.finditer("getAuthCode", m)][:3])
j = m.find("async postOAuth")
print("== postOAuth:", m[j:j + 700])
PY2
rm -rf "$D"
