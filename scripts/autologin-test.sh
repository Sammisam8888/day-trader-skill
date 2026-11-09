#!/usr/bin/env bash
# Try the auto-login now with the saved credentials, printing what Shoonya
# answers at each step (tokens masked). Places no orders.
#   sudo bash /opt/indstocks/day-trader-skill/scripts/autologin-test.sh
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
set -a; . /etc/indstocks.env; set +a
cd "$(dirname "$0")/../indstocks"
python3 autologin.py --force --debug && bash ../scripts/shoonya-check.sh
