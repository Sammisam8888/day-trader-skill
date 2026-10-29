#!/usr/bin/env bash
# Prove the three credentials work, end to end, without placing anything.
# Run ON the server, with sudo (it reads the root-only env file).
#
#   sudo bash /opt/indstocks/day-trader-skill/scripts/verify-auth.sh
#
# Generates a token via TOTP and calls two read-only endpoints. Read-only
# endpoints do not need the whitelisted IP, so this works even before the
# static IP is approved -- which makes it a clean way to separate a
# credential problem from a whitelist problem.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ -f /etc/indstocks.env ] || { echo "no /etc/indstocks.env -- run set-creds.sh first"; exit 1; }

set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/.."

echo "clock check (TOTP fails on drift over ~1 minute):"
timedatectl | sed -n 's/^ *\(System clock synchronized.*\)/  \1/p;s/^ *\(NTP service.*\)/  \1/p'

python3 - <<'PY'
import json, sys, urllib.request, urllib.error
sys.path.insert(0, "indstocks")
import auth

def mask(s):
    s = str(s)
    return s[:4] + "..." + s[-4:] if len(s) > 10 else "***"

try:
    m = auth.TokenManager()
    print("  TOTP code generates OK:", auth.totp(m.totp_secret)[:2] + "****")
    tok = m.token()
    print("  token acquired:", mask(tok))
except auth.LockedOut as e:
    print("LOCKED OUT:", e); sys.exit(2)
except auth.AuthError as e:
    print("AUTH FAILED:", e); sys.exit(1)

for path in ("/user/profile", "/funds"):
    req = urllib.request.Request(auth.BASE_URL + path, headers={"Authorization": tok})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        print("  GET %-16s %s" % (path, body.get("status", "ok")))
    except urllib.error.HTTPError as e:
        print("  GET %-16s HTTP %s %s" % (path, e.code, e.read().decode()[:160]))
        sys.exit(1)

print()
print("Credentials work. Read-only access confirmed.")
print("This does NOT prove order placement: that additionally needs the")
print("static IP whitelisted on the access-tokens page.")
PY
