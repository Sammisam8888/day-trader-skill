#!/usr/bin/env bash
# Read-only audit of one day's decision log: event counts, and every refusal,
# block, error and failure grouped by reason. Usage: sudo bash audit-day.sh [YYYY-MM-DD]
set -uo pipefail
D="${1:-$(TZ=Asia/Kolkata date +%F)}"
F=/var/lib/indstocks/auto/$D.jsonl
[ -f "$F" ] || { echo "no log for $D"; exit 0; }
python3 - "$F" <<'PY'
import collections, json, re, sys
rows = []
for ln in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        rows.append(json.loads(ln))
    except ValueError:
        pass
print("events:", len(rows))
c = collections.Counter(r.get("event") for r in rows)
print("counts:", ", ".join(f"{k}={v}" for k, v in c.most_common()))
def norm(s):
    return re.sub(r"[0-9]+(\.[0-9]+)?", "#", str(s))[:150]
for ev in ("refused", "blocked", "error", "order_failed", "order_rejected", "cancel_failed", "modify_failed",
           "exit_failed", "sell_failed", "stop_failed", "brain_unavailable", "lean_cli_failed", "score_failed",
           "sentiment_failed", "autologin_failed", "flatten_incomplete", "login_required", "abort", "halted"):
    rs = [r for r in rows if r.get("event") == ev]
    if not rs:
        continue
    g = collections.Counter(norm(r.get("reason") or "; ".join(r.get("reasons") or []) or r.get("action") or "") for r in rs)
    print(f"\n[{ev}] {len(rs)}")
    for k, v in g.most_common(12):
        print(f"  {v:4d}  {k}")
ent = [r for r in rows if r.get("event") == "entry_cancelled"]
if ent:
    g = collections.Counter(norm(r.get("reason")) for r in ent)
    print(f"\n[entry_cancelled] {len(ent)}")
    for k, v in g.most_common(8):
        print(f"  {v:4d}  {k}")
PY
