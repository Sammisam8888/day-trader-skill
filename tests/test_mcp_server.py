import json
import subprocess
import sys
import unittest
from pathlib import Path

from _load import ROOT, load

_m = load("indstocks", "mcp_server")
SERVER = Path(ROOT) / "indstocks" / "mcp_server.py"


def rpc(*requests):
    """Drive the real server over stdio, as Claude Desktop does."""
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    p = subprocess.run([sys.executable, str(SERVER)], input=payload,
                       capture_output=True, text=True, timeout=60)
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


class TestProtocol(unittest.TestCase):
    def test_initialize_and_list_tools(self):
        out = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "indstocks")
        names = [t["name"] for t in out[1]["result"]["tools"]]
        for expected in ("account", "positions", "scan", "propose_order",
                         "place_order", "check_protection", "flatten"):
            self.assertIn(expected, names)

    def test_every_tool_declares_a_schema(self):
        out = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        for t in out[0]["result"]["tools"]:
            self.assertIn("inputSchema", t, t["name"])
            self.assertIn("properties", t["inputSchema"], t["name"])

    def test_notifications_get_no_reply(self):
        out = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(out, [])

    def test_unknown_tool_is_an_error_not_a_crash(self):
        out = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", out[0])

    def test_malformed_line_does_not_kill_the_server(self):
        p = subprocess.run(
            [sys.executable, str(SERVER)],
            input="not json\n" + json.dumps(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n",
            capture_output=True, text=True, timeout=60)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(out[0]["id"], 7)


class TestWriteToolsAreGated(unittest.TestCase):
    """The security property: no path to the broker without a ticket."""

    def test_there_is_no_tool_that_takes_a_raw_payload(self):
        for name, _desc, _fn, schema in _m.TOOLS:
            props = set(schema.get("properties", {}))
            self.assertNotIn("payload", props, name)
            self.assertNotIn("body", props, name)
            self.assertNotIn("order", props, name)

    def test_place_order_requires_a_ticket_and_a_reply(self):
        schema = dict((n, s) for n, _d, _f, s in _m.TOOLS)["place_order"]
        self.assertEqual(set(schema["required"]), {"ticket_id", "reply"})

    def test_place_order_rejects_an_unknown_ticket(self):
        out = _m.t_place_order({"ticket_id": "deadbeef", "reply": "yes"})
        self.assertIn("unknown ticket", out["error"])

    def test_write_tools_are_described_as_placing_real_orders(self):
        for name, desc, _fn, _s in _m.TOOLS:
            if name in ("place_order", "flatten"):
                self.assertIn("PLACES A REAL ORDER", desc, name)

    def test_flatten_refuses_without_an_explicit_yes(self):
        for reply in ("sure", "ok", "", "yes to all"):
            out = _m.t_flatten({"symbol": "RELIANCE", "qty": 1, "reply": reply})
            self.assertIn("error", out, reply)

    def test_place_order_does_not_place_when_the_reply_is_not_yes(self):
        import ticket as tk_mod
        from decimal import Decimal as D
        import risk as risk_mod
        import smartorder as so

        class Inst:
            security_id, tick_size, exchange = "2885", D("0.10"), "NSE"

        order = so.build_for_instrument(Inst(), qty=10, entry=D("1502.40"),
                                        stop=D("1488.10"), target=D("1530.60"))
        tk = tk_mod.build_ticket("RELIANCE", order,
                                 risk_verdict=risk_mod.Verdict(True, []),
                                 max_loss=D("143"))
        _m._state["tickets"][tk.id] = tk
        out = _m.t_place_order({"ticket_id": tk.id, "reply": "sure go ahead"})
        self.assertFalse(out["placed"])
        self.assertIsNone(tk.parent_order_id, "nothing may have been sent")


if __name__ == "__main__":
    unittest.main()
