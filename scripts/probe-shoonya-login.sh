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
python3 - "$D/main.js" <<'PY2'
import re, sys
m = open(sys.argv[1], encoding="utf-8", errors="replace").read()
e = m.rfind("export{")
exp = m[e:e + 2500]
print("== export tail:", exp[:1200])
alias = dict((b, a) for a, b in re.findall(r"(\w+) as (\w+)", exp))
for pub in ("j", "k"):
    local = alias.get(pub, pub)
    print(f"== export {pub} -> local {local}")
    for mm in list(re.finditer(r"[,;\s{(]" + re.escape(local) + r"=", m))[:3]:
        print(f"   def: {m[mm.start():mm.start() + 260]}")
PY2
rm -rf "$D"
