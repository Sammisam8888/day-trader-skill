#!/usr/bin/env bash
# Read-only: fetch Shoonya's OAuth login page from this box and list the
# endpoints its JavaScript calls, to automate the daily login without a
# browser. Sends no credentials.
set -uo pipefail
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
CID=$(sudo grep -E '^SHOONYA_CLIENT_ID=' /etc/indstocks.env | cut -d= -f2)
UID_=$(sudo grep -E '^SHOONYA_USER_ID=' /etc/indstocks.env | cut -d= -f2)
D=$(mktemp -d)
for URL in "https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key=$CID&route_to=$UID_" \
           "https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id=$CID"; do
  echo "== GET $URL"
  curl -sS -L -A "$UA" -D "$D/h" -o "$D/p.html" -w "status %{http_code} final %{url_effective}\n" "$URL"
  grep -iE '^(location|set-cookie|content-type):' "$D/h" | sed 's/=[^;]*;/=…;/' | head
  echo "-- forms/inputs"; grep -oiE '<(form|input)[^>]*>' "$D/p.html" | head -20
  echo "-- scripts"; grep -oiE '<script[^>]*src="[^"]+"' "$D/p.html" | head -20
  BASE=$(curl -sS -L -A "$UA" -o /dev/null -w "%{url_effective}" "$URL" | sed -E 's#(https://[^/]+).*#\1#')
  for s in $(grep -oiE '<script[^>]*src="[^"]+"' "$D/p.html" | sed -E 's/.*src="([^"]+)".*/\1/' | head -8); do
    case "$s" in http*) u=$s ;; /*) u=$BASE$s ;; *) u=$(dirname "$(curl -sS -L -A "$UA" -o /dev/null -w "%{url_effective}" "$URL")")/$s ;; esac
    echo "-- js $u"
    curl -sS -A "$UA" "$u" -o "$D/s.js"
    grep -oE '"[^"]{0,80}(login|Login|LOGIN|auth|Auth|otp|OTP|totp|QuickAuth|validate|Validate|code=|redirect)[^"]{0,80}"' "$D/s.js" | sort -u | head -60
  done
done
rm -rf "$D"
