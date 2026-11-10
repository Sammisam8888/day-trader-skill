#!/usr/bin/env bash
# One tiny Claude call exactly as the trader makes it; prints the reply and
# the token usage. Read-only apart from one log line.
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
timeout 150 python3 - <<'PY'
import brain, common
print("reply:", brain.run_cli('Reply with exactly: {"ok": true}').strip()[:200])
for r in [r for r in common.read_log() if r.get("event") in ("cli_usage", "lean_cli_failed")][-2:]:
    print(r)
PY
