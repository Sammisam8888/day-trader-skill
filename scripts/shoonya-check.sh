#!/usr/bin/env bash
# Read-only Shoonya check: session, funds, positions, orders, each symbol's
# token / tick / quote / today's candles. Places nothing.
#   sudo bash scripts/shoonya-check.sh [SYMBOL ...]   (or a search: -s SENSEX)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
python3 - "$@" <<'PY'
import sys
from datetime import timedelta
import session as sess
from shoonya import Shoonya, LoginRequired, ShoonyaError, load_token
import common

args = sys.argv[1:]
b = Shoonya()
tok = load_token()
if not tok:
    print("NOT LOGGED IN: open the dashboard and tap 'Log in to Shoonya'"); sys.exit(1)
print(f"session  {tok['uid']} since {tok['at']}")
try:
    if args[:1] == ["-s"]:
        for r in b.search(" ".join(args[1:]))[:25]:
            q = b.quote(r["token"])
            print(f"  {r['tsym']:22} token {r['token']:>7}  ltp {q['ltp']:>10}  volume {q['volume']:>12}")
        sys.exit(0)
    lim = b.limits()
    print(f"cash     {b.cash()}   (cash {lim.get('cash')} payin {lim.get('payin')} marginused {lim.get('marginused')})")
    pos = b.positions(intraday_only=False)
    print(f"positions {len(pos)}:", ", ".join(f"{p['symbol']} {p['qty']} {p['product']}" for p in pos) or "none")
    od = b.orders()
    print(f"orders   {len(od)} today:", ", ".join(f"{o['symbol']} {o['side']} {o['status']}" for o in od[:8]) or "none")
    now = sess.now()
    for sym in (args or common.symbols()):
        try:
            i = b.instrument(sym)
        except KeyError as exc:
            print(f"  {sym}: NOT FOUND ({exc}); try: sudo bash scripts/shoonya-check.sh -s {sym[:6]}"); continue
        q = b.quote(i.token)
        cs = b.candles(i.token, now - timedelta(days=4), now)
        last = cs[-1] if cs else None
        print(f"  {sym:12} {i.tsym:16} token {i.token:>7} tick {i.tick} | ltp {q['ltp']} "
              f"band {q['lc']}-{q['uc']} | {len(cs)} 5m candles"
              + (f", last {last.opens_at:%d %b %H:%M} close {last.close}" if last else ""))
    print("OK: reads work. Nothing was placed.")
except LoginRequired as exc:
    print(f"LOGIN NEEDED: {exc}"); sys.exit(1)
except ShoonyaError as exc:
    print(f"BROKER ERROR: {exc}"); sys.exit(1)
PY
