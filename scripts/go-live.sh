#!/usr/bin/env bash
# Turn autonomous LIVE placement on or off.
#
#   sudo bash scripts/go-live.sh on     # real orders, unattended
#   sudo bash scripts/go-live.sh off    # decide and log only
#   sudo bash scripts/go-live.sh status
#
# The halt file is the emergency brake and is checked before every order:
#   sudo bash scripts/go-live.sh halt "reason"
#   sudo bash scripts/go-live.sh resume
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
ENV=/etc/indstocks.env
HALT=/etc/indstocks.halt

set_var() {
  sed -i "/^$1=/d" "$ENV"
  printf '%s=%s\n' "$1" "$2" >> "$ENV"
}

case "${1:-status}" in
  on)
    set_var TRADER_LIVE true
    echo "LIVE placement ON. Unattended runs will send real orders."
    echo "Limits now in force:"
    grep -E '^TRADER_(MAX_LOSS|DAILY_LOSS|MAX_TRADES)=' "$ENV" || \
      echo "  (defaults: max loss 500, daily 1500, 3 trades)"
    echo "Emergency stop: sudo bash $0 halt \"reason\""
    ;;
  off)
    set_var TRADER_LIVE false
    echo "LIVE placement OFF. Runs decide and log, and place nothing."
    ;;
  halt)
    printf '%s\n' "${2:-halted manually}" > "$HALT"
    chmod 644 "$HALT"
    echo "HALTED. No further orders until resumed. Resting orders at the"
    echo "exchange are NOT cancelled by this -- check positions."
    ;;
  resume)
    rm -f "$HALT"
    echo "halt cleared"
    ;;
  status)
    grep -E '^TRADER_' "$ENV" 2>/dev/null | sed 's/^/  /' || echo "  no TRADER_ vars set"
    [ -f "$HALT" ] && echo "  HALTED: $(cat "$HALT")" || echo "  not halted"
    systemctl is-enabled auto.timer >/dev/null 2>&1 \
      && echo "  auto.timer: $(systemctl is-active auto.timer)" \
      || echo "  auto.timer: not installed"
    ;;
  *) echo "usage: $0 {on|off|halt [reason]|resume|status}"; exit 1 ;;
esac
