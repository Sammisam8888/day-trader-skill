#!/usr/bin/env bash
# What would the system have done, and how did those setups actually do?
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"

python3 - <<'PY'
import json, os
from decimal import Decimal
from pathlib import Path
import session as sess

LOG = Path(os.environ.get("TRADER_SHADOW_DIR", "/var/lib/indstocks/shadow"))
files = sorted(LOG.glob("*.jsonl"))
if not files:
    raise SystemExit(f"no shadow logs yet in {LOG}")

placed = []
counts = {}
for f in files:
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        ev = r.get("event", "?")
        counts[ev] = counts.get(ev, 0) + 1
        if ev == "would_place":
            placed.append(r)

print(f"{len(files)} session(s) logged\n")
for ev, n in sorted(counts.items(), key=lambda kv: -kv[1]):
    print(f"  {ev:18} {n}")

if not placed:
    print("\nNothing would have been placed yet. That is a result, not a failure:")
    print("it says the gate found no setup worth its costs on these days.")
    raise SystemExit(0)

print(f"\n{len(placed)} would-be trades:\n")
head = ("date".ljust(12) + "symbol".ljust(13) + "qty".rjust(5)
        + "entry".rjust(10) + "stop".rjust(10) + "target".rjust(10)
        + "risk".rjust(9) + "net@tgt".rjust(10))
print(head); print("-" * len(head))
risk_total = gain_total = Decimal(0)
for r in placed:
    risk_total += Decimal(r["loss_at_stop"])
    gain_total += Decimal(r["net_if_target"])
    print(r["ts"][:10].ljust(12) + r["symbol"].ljust(13)
          + str(r["qty"]).rjust(5) + r["entry"].rjust(10)
          + r["stop"].rjust(10) + r["target"].rjust(10)
          + r["loss_at_stop"].rjust(9) + r["net_if_target"].rjust(10))

print("-" * len(head))
print(f"total risked if every stop hit: {risk_total}")
print(f"total net if every target hit : {gain_total}")
print()
print("Neither figure is the outcome. Scoring needs what the stock actually")
print("did after entry -- fetch those days' candles and check which level came")
print("first. Until then this says what the system DECIDED, not how it did.")
PY
