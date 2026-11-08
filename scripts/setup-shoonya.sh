#!/usr/bin/env bash
# One-time Shoonya setup. Asks for each value, writes /etc/indstocks.env,
# removes the old INDstocks settings and services, installs the Shoonya
# timers and services, and tells you exactly what to register on Shoonya.
#
#   sudo bash /opt/indstocks/day-trader-skill/scripts/setup-shoonya.sh
#
# Safe to re-run: press Enter to keep any value already saved.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

ENV=/etc/indstocks.env
REPO=/opt/indstocks/day-trader-skill
STATIC_IP=13.127.164.72
touch "$ENV"; chmod 600 "$ENV"

get()  { grep -E "^$1=" "$ENV" 2>/dev/null | tail -1 | cut -d= -f2- || true; }
put()  { sed -i "/^$1=/d" "$ENV"; printf '%s=%s\n' "$1" "$2" >> "$ENV"; }
ask() {  # ask KEY "prompt" default [secret]
  local key=$1 prompt=$2 def=$3 secret=${4:-} cur val shown
  cur=$(get "$key"); [ -n "$cur" ] && def=$cur
  if [ -n "$secret" ] && [ -n "$def" ]; then shown="(saved, Enter to keep)"; else shown=${def:+[$def]}; fi
  if [ -n "$secret" ]; then read -r -s -p "$prompt $shown: " val; echo; else read -r -p "$prompt $shown: " val; fi
  val=${val:-$def}
  printf -v "$key" '%s' "$val"
  [ -n "$val" ] && put "$key" "$val"
  return 0
}

TS_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
REDIRECT="http://${TS_IP:-100.127.19.72}:8080/shoonya/callback"

cat <<TXT

==================== Shoonya setup ====================
First, on trade.shoonya.com -> profile menu -> API Key, set:
  Primary IP address ..... $STATIC_IP
  Secondary IP address ... $STATIC_IP
  "more than 10 orders per second" ... leave UNTICKED
  Redirect URL (if the page has one) ... $REDIRECT
Then copy the Client ID and Secret Code shown there.
========================================================

TXT

ask SHOONYA_USER_ID     "1/7 Shoonya user ID (login ID)"                   "FN245398"
ask SHOONYA_CLIENT_ID   "2/7 Client ID from the API Key page"              ""
ask SHOONYA_SECRET_CODE "3/7 Secret Code from the API Key page (hidden)"   ""  secret
ask SHOONYA_ALGO_ID     "4/7 SEBI Algo ID from Shoonya (Enter if none yet)" ""
ask TRADER_SYMBOLS      "5/7 Symbols to trade, comma separated"            "NIFTYBEES"
if [ -z "$(get TRADER_NTFY_TOPIC)" ]; then
  suggestion="trader-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
else suggestion=""; fi
ask TRADER_NTFY_TOPIC   "6/7 ntfy topic for phone alerts (Enter to accept)" "$suggestion"
read -r -p "7/7 Place REAL orders as soon as you are logged in? (yes/no) [no]: " golive
case "${golive,,}" in yes|y) put TRADER_LIVE true ;; *) put TRADER_LIVE false ;; esac

for k in SHOONYA_USER_ID SHOONYA_CLIENT_ID SHOONYA_SECRET_CODE; do
  [ -n "$(get $k)" ] || { echo "ERROR: $k is required"; exit 1; }
done

# Defaults for anything never set. Existing values are kept.
for kv in TRADER_LIVE=false TRADER_MAX_LOSS=300 TRADER_DAILY_LOSS=1000 TRADER_MAX_TRADES=4 \
          TRADER_MAX_CONCURRENT=2 TRADER_MAX_LEVERAGE=1.0 TRADER_MAX_RESTING=4 \
          TRADER_MIN_ORDER_VALUE=5000 TRADER_MIN_REWARD_TO_COST=2 TRADER_RESTING_TTL_MIN=45 \
          TRADER_STOP_BAND_PCT=0.5 TRADER_MARKETABLE_PCT=0.3 TRADER_SCAN_MINUTES=5; do
  k=${kv%%=*}; [ -n "$(get "$k")" ] || put "$k" "${kv#*=}"
done

echo; echo "==> removing INDstocks settings and services"
sed -i '/^INDSTOCKS_/d' "$ENV"
for u in indstocks-mcp.service shadow.timer shadow.service; do
  systemctl disable --now "$u" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$u"
done
rm -f /var/lib/indstocks/token.json
rm -rf /etc/systemd/system/auto.timer.d

echo "==> installing Shoonya timers and services"
for u in auto.service auto.timer flatten.service flatten.timer watch.service watch.timer \
         fillmonitor.service dashboard.service; do
  cp "$REPO/scripts/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now auto.timer flatten.timer watch.timer >/dev/null
systemctl enable fillmonitor dashboard >/dev/null
systemctl restart fillmonitor dashboard

echo "==> checking"
cd "$REPO" && python3 -m unittest discover -s tests 2>&1 | tail -1
systemctl is-active fillmonitor dashboard auto.timer | paste -sd' ' | sed 's/^/services: /'

cat <<TXT

Done. LIVE is $(get TRADER_LIVE) (false = decides and logs, places nothing).
Limits in force:
$(grep -E '^TRADER_(SYMBOLS|MAX_LOSS|DAILY_LOSS|MAX_TRADES|MAX_CONCURRENT|MAX_LEVERAGE|MAX_RESTING)=' "$ENV" | sed 's/^/  /')

NEXT, once per trading day:
  1. Open the dashboard:  http://${TS_IP:-100.127.19.72}:8080
  2. Tap "Log in to Shoonya" and sign in (user ID, password, TOTP).
     If the page you land on does not load, copy its address and paste
     it into the box under the Log in button.
  3. Check the account works (reads only, places nothing):
       sudo bash $REPO/scripts/shoonya-check.sh

Phone alerts: install the ntfy app and subscribe to "$(get TRADER_NTFY_TOPIC)".
When the check is clean and you want real orders:
       sudo bash $REPO/scripts/go-live.sh on
TXT
