#!/usr/bin/env python3
"""Start the MCP server, list its tools, exit. Proves the server runs with
the deployed credentials without touching a write endpoint."""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "indstocks", "mcp_server.py")
req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
p = subprocess.run([sys.executable, SERVER], input=req, capture_output=True,
                   text=True, timeout=60)
lines = [l for l in p.stdout.splitlines() if l.strip()]
if not lines:
    sys.stderr.write(p.stderr)
    sys.exit("MCP server produced no response")
tools = json.loads(lines[0])["result"]["tools"]
print("MCP server OK, %d tools: %s" % (len(tools), ", ".join(t["name"] for t in tools)))
