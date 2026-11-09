#!/usr/bin/env bash
# Store the Shoonya password and authenticator (TOTP) secret on this box so
# the daily login happens by itself, then log in once to prove it works.
#   sudo bash /opt/indstocks/day-trader-skill/scripts/set-autologin.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
ENV=/etc/indstocks.env
REPO=/opt/indstocks/day-trader-skill
put() { sed -i "/^$1=/d" "$ENV"; printf '%s=%s\n' "$1" "$2" >> "$ENV"; }

cat <<TXT
Shoonya auto-login. Two values, both stay on this server only
($ENV, readable by root alone). Nothing you type is shown.

  Password ......... your Shoonya login password
  TOTP secret ...... the setup key behind your authenticator entry
                     (letters A-Z and digits 2-7, often 16 or 32 long;
                     NOT the 6-digit code)

TXT
read -r -s -p "Shoonya password: " PW; echo
read -r -s -p "TOTP secret key:  " TS; echo
TS=$(printf '%s' "$TS" | tr -d ' ' | tr '[:lower:]' '[:upper:]')
[ -n "$PW" ] && [ -n "$TS" ] || { echo "both are required"; exit 1; }
printf '%s' "$TS" | grep -Eq '^[A-Z2-7]+=*$' || { echo "that is not a base32 TOTP secret (A-Z, 2-7)"; exit 1; }

python3 - "$TS" <<'PY'
import sys
sys.path.insert(0, "/opt/indstocks/day-trader-skill/indstocks")
import shoonya
print("Your authenticator app should show this code right now:", shoonya.totp(sys.argv[1]))
PY
read -r -p "Does it match your authenticator app? (yes/no): " ok
[ "${ok,,}" = "yes" ] || [ "${ok,,}" = "y" ] || { echo "Not saved. Check the secret key and run again."; exit 1; }

put SHOONYA_PASSWORD "$PW"
put SHOONYA_TOTP_SECRET "$TS"
chmod 600 "$ENV"
rm -f /var/lib/indstocks/autologin_guard.json /var/lib/indstocks/autologin_attempts.json
cp "$REPO/scripts/login.service" "$REPO/scripts/login.timer" /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now login.timer >/dev/null

echo; echo "==> logging in now"
set -a; . "$ENV"; set +a
cd "$REPO/indstocks"
python3 autologin.py --force || { echo "Login failed (see message above). Nothing will retry a bad password."; exit 1; }
echo; echo "==> reading the account"
bash "$REPO/scripts/shoonya-check.sh"
echo
echo "Done. Each trading day at 08:40 (retry 08:55) the box logs in by itself."
