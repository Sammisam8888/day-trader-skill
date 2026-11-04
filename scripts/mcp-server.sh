#!/usr/bin/env bash
# Launcher for the MCP server.
#
# The server reads its credentials and limits from the environment. Claude
# Code starts it as a child of the user's shell, which does not have them:
# /etc/indstocks.env is root-only and is sourced explicitly by every other
# script here. Registering `python3 mcp_server.py` directly therefore starts
# a server with no credentials, which fails on the first tool call.
#
# Register THIS file instead:
#   claude mcp add --transport stdio --scope user indstocks -- \
#     sudo /opt/indstocks/day-trader-skill/scripts/mcp-server.sh
#
# stdout belongs to the JSON-RPC protocol, so nothing here may print to it.
set -euo pipefail

[ -f /etc/indstocks.env ] || {
  echo "missing /etc/indstocks.env -- run scripts/set-creds.sh" >&2
  exit 1
}
set -a
. /etc/indstocks.env

# Risk limits, overridable in /etc/indstocks.env. Deliberately small: the
# first trades are for watching the mechanism work, not for making money.
: "${TRADER_MAX_LOSS:=100}"
: "${TRADER_DAILY_LOSS:=300}"
: "${TRADER_MAX_TRADES:=2}"
set +a

exec /usr/bin/python3 /opt/indstocks/day-trader-skill/indstocks/mcp_server.py "$@"
