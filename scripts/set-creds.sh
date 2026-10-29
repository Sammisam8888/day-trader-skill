#!/usr/bin/env bash
# Write the broker credentials to /etc/indstocks.env, root-only.
# Run ON the server, with sudo. Values are read from the terminal, never
# from arguments: an argument would land in shell history and in ps output.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

read -r -p  "INDSTOCKS_CLIENT_ID (access-tokens page, after TOTP setup): " CID
read -r -sp "INDSTOCKS_MPIN (hidden): " MPIN; echo
read -r -sp "INDSTOCKS_TOTP_SECRET (hidden, shown once at setup): " TOTP; echo

[ -n "$CID" ] && [ -n "$MPIN" ] && [ -n "$TOTP" ] || { echo "all three required"; exit 1; }

umask 077
printf 'INDSTOCKS_CLIENT_ID=%s\nINDSTOCKS_MPIN=%s\nINDSTOCKS_TOTP_SECRET=%s\n' \
  "$CID" "$MPIN" "$TOTP" > /etc/indstocks.env
chmod 600 /etc/indstocks.env
chown root:root /etc/indstocks.env

echo "wrote /etc/indstocks.env (0600 root:root)"
echo "this file is gitignored and must never be committed or copied off this box"
