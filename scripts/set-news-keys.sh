#!/usr/bin/env bash
# Store optional news API keys in /etc/indstocks.env (root-only). Each is
# free to sign up for; press Enter to skip one. Keys are read without echo
# and checked with one request before they are saved.
#   Marketaux      https://www.marketaux.com/register     (India news + entity sentiment, 100 req/day)
#   Finnhub        https://finnhub.io/register            (global market news)
#   Alpha Vantage  https://www.alphavantage.co/support/#api-key   (news sentiment, 25 req/day)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
ENV=/etc/indstocks.env
save() { sed -i "/^$1=/d" "$ENV"; echo "$1=$2" >> "$ENV"; chmod 600 "$ENV"; echo "  saved $1"; }
check() { curl -s -m 15 -o /dev/null -w "%{http_code}" "$1"; }

read -r -s -p "Marketaux API token (Enter to skip): " MK; echo
if [ -n "$MK" ]; then
  code=$(check "https://api.marketaux.com/v1/news/all?countries=in&limit=1&api_token=$MK")
  [ "$code" = "200" ] && save TRADER_MARKETAUX_KEY "$MK" || echo "  Marketaux answered HTTP $code: not saved"
fi
read -r -s -p "Finnhub API key (Enter to skip): " FK; echo
if [ -n "$FK" ]; then
  code=$(check "https://finnhub.io/api/v1/news?category=general&token=$FK")
  [ "$code" = "200" ] && save TRADER_FINNHUB_KEY "$FK" || echo "  Finnhub answered HTTP $code: not saved"
fi
read -r -s -p "Alpha Vantage API key (Enter to skip): " AK; echo
if [ -n "$AK" ]; then
  code=$(check "https://www.alphavantage.co/query?function=NEWS_SENTIMENT&limit=1&apikey=$AK")
  [ "$code" = "200" ] && save TRADER_ALPHAVANTAGE_KEY "$AK" || echo "  Alpha Vantage answered HTTP $code: not saved"
fi
systemctl restart newswatch && echo "newswatch restarted with the new keys"
