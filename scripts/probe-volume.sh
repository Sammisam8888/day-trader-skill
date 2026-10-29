#!/usr/bin/env bash
# Does a FRESH process, asking for one code and nothing else, get candles for
# a symbol the dry run reports as empty?
#
#   works here, empty in the dry run  -> the limit is request VOLUME per
#                                        session/window, not the symbol
#   empty here too                    -> something about the symbol or window
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

SYM="${1:-TCS}"

echo "=== fresh process, ONE code, narrow window (what the dry run asks for) ==="
python3 - "$SYM" <<'PY'
import sys
from datetime import date, timedelta
import session as sess, instruments as inst_mod
from client import Client
sym = sys.argv[1]
day = sess.previous_trading_days(date.today(), 1)[0]
s = sess.Session(day)
c = Client()
i = inst_mod.Instruments(client=c, source="equity").load().resolve(sym)
code = f"{i.exchange}_{i.security_id}"
for label, end in (("09:15-10:15", s.open + timedelta(hours=1)),
                   ("09:15-15:30", s.close)):
    r = c.get("/market/historical/5minute", params={
        "scrip-codes": code, "start_time": sess.epoch_ms(s.open),
        "end_time": sess.epoch_ms(end)})
    n = len((r.get("data", {}).get(code) or {}).get("candles") or [])
    print(f"  {code}  {label}  -> {n} candles")
PY

echo
echo "=== same process, after burning 40 data calls first ==="
python3 - "$SYM" <<'PY'
import sys
from datetime import date, timedelta
import session as sess, instruments as inst_mod
from client import Client
sym = sys.argv[1]
day = sess.previous_trading_days(date.today(), 1)[0]
s = sess.Session(day)
c = Client()
m = inst_mod.Instruments(client=c, source="equity").load()
i = m.resolve(sym)
code = f"{i.exchange}_{i.security_id}"

burn = [f"{r.exchange}_{r.security_id}" for r in m._rows[:40]]
for b in burn:
    try:
        c.get("/market/historical/5minute", params={
            "scrip-codes": b, "start_time": sess.epoch_ms(s.open),
            "end_time": sess.epoch_ms(s.close)})
    except Exception:
        pass

r = c.get("/market/historical/5minute", params={
    "scrip-codes": code, "start_time": sess.epoch_ms(s.open),
    "end_time": sess.epoch_ms(s.close)})
n = len((r.get("data", {}).get(code) or {}).get("candles") or [])
print(f"  {code}  after 40 calls  -> {n} candles")
PY
