"""
Read-only dashboard. Standard library only. Binds to localhost.

Reach it over an SSH tunnel, never a public port -- this box holds broker
credentials and can place orders, and "nobody knows the URL" is not a
security model:

    ssh -L 8080:127.0.0.1:8080 -i ~/.ssh/lightsail.pem ubuntu@13.127.164.72
    # then open http://localhost:8080 on your laptop

Shows: funds and P&L, open positions, resting orders, today's decisions
with the reason for each, the limits in force, and the timers. It cannot
place, cancel or modify anything: there is no POST handler.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session as sess
from client import Client

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
ENV_FILE = Path("/etc/indstocks.env")
HALT_FILE = Path(os.environ.get("TRADER_KILL_SWITCH", "/etc/indstocks.halt"))
PORT = int(os.environ.get("TRADER_DASHBOARD_PORT", "8080"))

# Where to listen. 127.0.0.1 always. The Tailscale address too, when one
# exists: it is private to the account owner's devices and never routed
# from the public internet, so it is the same trust boundary as localhost.
# 0.0.0.0 is refused outright -- there is no setting for it.
BIND = os.environ.get("TRADER_DASHBOARD_BIND", "auto")


def tailscale_ip() -> str | None:
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                           text=True, timeout=5)
        ip = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        return ip if ip.startswith("100.") else None
    except Exception:                                   # noqa: BLE001
        return None


def bind_addresses() -> list:
    if BIND == "auto":
        out = ["127.0.0.1"]
        ts = tailscale_ip()
        if ts:
            out.append(ts)
        return out
    if BIND in ("0.0.0.0", "", "*", "::"):
        raise SystemExit("refusing to bind the dashboard to all interfaces; "
                         "this box holds broker credentials")
    return [BIND]


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def money(x) -> str:
    try:
        return f"{Decimal(str(x)):,.2f}"
    except Exception:                                   # noqa: BLE001
        return e(x)


# ------------------------------------------------------------------ data

def read_log(day: str) -> list:
    path = STATE_DIR / f"{day}.jsonl"
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def limits() -> dict:
    out = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("TRADER_") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
    except OSError:
        pass
    return out


def timers() -> str:
    try:
        r = subprocess.run(["systemctl", "list-timers", "--no-pager", "--no-legend",
                            "auto.timer", "flatten.timer", "watch.timer"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "(no timers)"
    except Exception as exc:                            # noqa: BLE001
        return f"(unavailable: {exc})"


def live(client) -> dict:
    out = {"funds": {}, "positions": [], "orders": []}
    try:
        out["funds"] = (client.get("/funds") or {}).get("data") or {}
    except Exception as exc:                            # noqa: BLE001
        out["funds"] = {"error": str(exc)[:120]}
    try:
        d = (client.get("/portfolio/positions") or {}).get("data") or {}
        out["positions"] = d if isinstance(d, list) else (
            d.get("positions") or d.get("net_positions") or [])
    except Exception as exc:                            # noqa: BLE001
        out["positions"] = [{"error": str(exc)[:120]}]
    try:
        d = (client.get("/order-book") or {}).get("data") or []
        out["orders"] = [r for r in (d if isinstance(d, list) else [])
                         if "auto/" in str(r.get("remarks", ""))
                         or "flat/" in str(r.get("remarks", ""))]
    except Exception as exc:                            # noqa: BLE001
        out["orders"] = [{"error": str(exc)[:120]}]
    return out


# ------------------------------------------------------------------ page

CSS = """
body{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a1a;background:#fafafa}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}
th{background:#f3f3f3;font-weight:600}
.k{display:inline-block;min-width:160px;color:#555}
.ok{color:#187a3a}.bad{color:#b3261e}.warn{color:#9a6700}.muted{color:#777}
pre{background:#f3f3f3;padding:8px;font-size:12px;overflow:auto}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:#eee}
.placed{background:#dff3e4}.blocked{background:#fde8e6}.ALERT{background:#fde8e6;font-weight:600}
.halted{background:#fff3cd}.sold{background:#dff3e4}.would_place{background:#e6f0fd}
"""


def render(day: str) -> str:
    log = read_log(day)
    lim = limits()
    client = Client()
    data = live(client)
    f = data["funds"]
    now = sess.now()
    session = sess.session_for(now)
    phase = session.phase(now) if session else "not a trading day"
    halted = HALT_FILE.exists()
    live_flag = lim.get("TRADER_LIVE", "false").lower() in ("1", "true", "yes")

    p = [f"<!doctype html><meta charset=utf-8><title>trader {e(day)}</title>"
         f"<style>{CSS}</style>",
         f"<h1>INDstocks autotrader</h1>"
         f"<div class=muted>{e(now.strftime('%a %d %b %Y %H:%M:%S IST'))} &middot; "
         f"phase <b>{e(phase)}</b> &middot; "
         + (f"<span class=bad>HALTED</span>" if halted else
            (f"<span class=ok>LIVE</span>" if live_flag else
             f"<span class=warn>SHADOW (places nothing)</span>"))
         + f" &middot; <a href='?day={e(day)}'>refresh</a></div>"]

    # funds
    p.append("<h2>Account</h2><table>")
    if "error" in f:
        p.append(f"<tr><td class=bad>{e(f['error'])}</td></tr>")
    else:
        bal = f.get("detailed_avl_balance") or {}
        rp, up = f.get("realized_pnl", 0), f.get("unrealized_pnl", 0)
        tot = Decimal(str(rp or 0)) + Decimal(str(up or 0))
        cls = "ok" if tot >= 0 else "bad"
        p.append(f"<tr><td><span class=k>buying power (eq_mis)</span>{money(bal.get('eq_mis'))}</td>"
                 f"<td><span class=k>realised today</span><span class={'ok' if (rp or 0) >= 0 else 'bad'}>{money(rp)}</span></td></tr>"
                 f"<tr><td><span class=k>withdrawable</span>{money(f.get('withdrawal_balance'))}</td>"
                 f"<td><span class=k>unrealised</span><span class={'ok' if (up or 0) >= 0 else 'bad'}>{money(up)}</span></td></tr>"
                 f"<tr><td><span class=k>brokerage today</span>{money(f.get('brokerage'))}</td>"
                 f"<td><span class=k>combined P&amp;L</span><b class={cls}>{money(tot)}</b></td></tr>")
    p.append("</table>")

    # limits
    p.append("<h2>Limits in force</h2><table><tr>")
    for k in ("TRADER_LIVE", "TRADER_MAX_LOSS", "TRADER_DAILY_LOSS", "TRADER_MAX_TRADES",
              "TRADER_MAX_LEVERAGE", "TRADER_MAX_CONCURRENT", "TRADER_STOP_BAND_PCT"):
        p.append(f"<td><span class=k>{e(k.replace('TRADER_', '').lower())}</span>"
                 f"{e(lim.get(k, '<default>'))}</td>")
    p.append("</tr></table>")

    # positions
    p.append("<h2>Open positions</h2>")
    pos = [r for r in data["positions"]
           if isinstance(r, dict) and int(r.get("net_qty", r.get("quantity", r.get("qty", 0))) or 0)]
    if not pos:
        p.append("<div class=muted>none</div>")
    else:
        p.append("<table><tr><th>name</th><th>qty</th><th>avg</th><th>product</th><th>P&amp;L</th></tr>")
        for r in pos:
            if "error" in r:
                p.append(f"<tr><td class=bad colspan=5>{e(r['error'])}</td></tr>"); continue
            p.append(f"<tr><td>{e(r.get('name'))}</td>"
                     f"<td>{e(r.get('net_qty', r.get('quantity', r.get('qty'))))}</td>"
                     f"<td>{money(r.get('avg_price') or r.get('buy_avg_price'))}</td>"
                     f"<td>{e(r.get('product'))}</td>"
                     f"<td>{money(r.get('unrealized_pnl') or r.get('pnl') or r.get('realised_gains'))}</td></tr>")
        p.append("</table>")

    # orders
    p.append("<h2>Our orders at the broker</h2>")
    if not data["orders"]:
        p.append("<div class=muted>none</div>")
    else:
        p.append("<table><tr><th>id</th><th>name</th><th>side</th><th>type</th>"
                 "<th>status</th><th>qty</th><th>filled</th><th>trigger/limit</th>"
                 "<th>SL / target</th><th>parent</th></tr>")
        for r in data["orders"]:
            if "error" in r:
                p.append(f"<tr><td class=bad colspan=10>{e(r['error'])}</td></tr>"); continue
            st = str(r.get("status", "")).upper()
            cls = "ok" if st == "SUCCESS" else ("bad" if st in ("CANCELLED", "REJECTED", "FAILED") else "warn")
            p.append(f"<tr><td class=muted>{e(r.get('id'))}</td><td>{e((r.get('name') or '')[:22])}</td>"
                     f"<td>{e(r.get('txn_type'))}</td><td>{e(r.get('order_type'))}</td>"
                     f"<td class={cls}>{e(st)}</td><td>{e(r.get('requested_qty'))}</td>"
                     f"<td>{e(r.get('traded_qty'))}</td>"
                     f"<td>{e(r.get('trigger_price') or r.get('requested_price') or '')}</td>"
                     f"<td>{e(r.get('sl_trigger_price') or '')} / {e(r.get('tgt_trigger_price') or '')}</td>"
                     f"<td class=muted>{e(r.get('parent_order_id') or '')}</td></tr>")
        p.append("</table>")

    # decisions
    p.append(f"<h2>Decisions on {e(day)} <span class=muted>({len(log)} events)</span></h2>")
    if not log:
        p.append("<div class=muted>no log for this day</div>")
    else:
        p.append("<table><tr><th>time</th><th>job</th><th>event</th><th>symbol</th><th>detail</th></tr>")
        for r in reversed(log):
            ts = (r.get("ts") or "")[11:19]
            ev = r.get("event", "")
            job = r.get("job", "auto")
            sym = r.get("symbol") or r.get("name") or ""
            skip = {"ts", "event", "job", "symbol", "name", "live", "payload"}
            detail = "; ".join(f"{k}={v}" for k, v in r.items() if k not in skip)
            p.append(f"<tr class='{e(ev)}'><td class=muted>{e(ts)}</td><td class=muted>{e(job)}</td>"
                     f"<td><span class=pill>{e(ev)}</span></td><td><b>{e(sym)}</b></td>"
                     f"<td style='font-size:12px'>{e(detail[:400])}</td></tr>")
        p.append("</table>")

    p.append(f"<h2>Timers</h2><pre>{e(timers())}</pre>")
    p.append("<div class=muted style='margin:24px 0'>Read-only. This page has no POST handler and cannot place, "
             "cancel or modify anything. To act, use Claude Code on the box.</div>")
    return "".join(p)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):                                   # noqa: N802
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        day = (q.get("day") or [sess.now().strftime("%Y-%m-%d")])[0]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            day = sess.now().strftime("%Y-%m-%d")
        try:
            body = render(day).encode("utf-8")
            code = 200
        except Exception as exc:                        # noqa: BLE001
            body = f"<pre>dashboard error: {e(exc)}</pre>".encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                          # quiet
        pass


def main() -> int:
    import threading
    servers = []
    for addr in bind_addresses():
        srv = HTTPServer((addr, PORT), Handler)
        servers.append(srv)
        print(f"dashboard on http://{addr}:{PORT}", flush=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not servers:
        return 1
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
