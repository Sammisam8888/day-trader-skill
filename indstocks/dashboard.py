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
:root{color-scheme:dark;--bg:#0f1115;--card:#171a21;--line:#262b35;--fg:#e6e8ec;--mut:#8b93a1;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{font:15px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:12px;color:var(--fg);background:var(--bg)}
h1{font-size:18px;margin:0 0 2px}h2{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.kv .k{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.kv .v{font-size:17px;font-weight:600;margin-top:2px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--mut)}
.row{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .l{flex:1;min-width:0}.row .r{text-align:right;white-space:nowrap}
.sym{font-weight:700;font-size:15px}
.sub{font-size:12px;color:var(--mut);margin-top:2px;word-break:break-word}
.pill{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;background:#262b35;color:var(--fg)}
.pill.placed,.pill.sold,.pill.ok{background:#1b3a26;color:var(--ok)}
.pill.blocked,.pill.ALERT,.pill.rejected_locally,.pill.halted{background:#3d1d1d;color:var(--bad)}
.pill.would_place,.pill.run_start,.pill.run_end,.pill.reconciled_out{background:#1d2d44;color:var(--acc)}
.pill.unsized,.pill.skip,.pill.no_data{background:#2d2a1a;color:var(--warn)}
.status{font-size:13px;color:var(--mut);margin-bottom:12px}
.status b{color:var(--fg)}
form{margin:0}label{display:block;font-size:12px;color:var(--mut);margin:8px 0 3px}
input[type=number],input[type=text]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:16px}
button{padding:12px 16px;border:0;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;width:100%;margin-top:10px}
.btn-save{background:var(--acc);color:#000}.btn-halt{background:var(--bad);color:#fff}.btn-resume{background:var(--ok);color:#000}
.toggle{display:flex;align-items:center;gap:10px;padding:10px 0}
.toggle input{width:22px;height:22px}
details summary{cursor:pointer;color:var(--acc);font-size:13px;padding:6px 0}
.flash{background:#1b3a26;color:var(--ok);padding:10px;border-radius:8px;margin-bottom:10px;font-size:14px}
.flash.err{background:#3d1d1d;color:var(--bad)}
pre{background:var(--bg);padding:8px;font-size:11px;overflow:auto;border-radius:6px;margin:0}
@media(min-width:720px){body{max-width:900px;margin:0 auto;padding:20px}.grid{grid-template-columns:repeat(4,1fr)}}
"""


EDITABLE = (
    ("TRADER_MAX_LOSS", "max loss per trade (Rs)", "number"),
    ("TRADER_DAILY_LOSS", "daily loss limit (Rs)", "number"),
    ("TRADER_MAX_TRADES", "max trades per day", "number"),
    ("TRADER_MAX_CONCURRENT", "max concurrent positions", "number"),
    ("TRADER_MAX_LEVERAGE", "max leverage (1.0 = cash only)", "number"),
    ("TRADER_STOP_BAND_PCT", "stop band % (room for a gap to fill)", "number"),
)


def render(day: str, flash: str = "", flash_err: bool = False) -> str:
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

    state = (f"<span class=bad>HALTED</span>" if halted else
             (f"<span class=ok>LIVE</span>" if live_flag else
              f"<span class=warn>SHADOW</span>"))
    p = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>trader</title><style>{CSS}</style>",
         f"<h1>autotrader</h1>"
         f"<div class=status>{e(now.strftime('%a %d %b %H:%M'))} &middot; <b>{e(phase)}</b> &middot; {state}"
         f" &middot; <a href='?day={e(day)}' style='color:var(--acc)'>refresh</a></div>"]
    if flash:
        p.append(f"<div class='flash{' err' if flash_err else ''}'>{e(flash)}</div>")

    # ---- account ----
    p.append("<h2>Account</h2><div class='card grid kv'>")
    if "error" in f:
        p.append(f"<div class=bad>{e(f['error'])}</div>")
    else:
        bal = f.get("detailed_avl_balance") or {}
        rp, up = Decimal(str(f.get("realized_pnl") or 0)), Decimal(str(f.get("unrealized_pnl") or 0))
        tot = rp + up
        def kv(k, v, cls=""):
            return f"<div><div class=k>{e(k)}</div><div class='v {cls}'>{v}</div></div>"
        p.append(kv("buying power", money(bal.get("eq_mis"))))
        p.append(kv("combined P&L", money(tot), "ok" if tot >= 0 else "bad"))
        p.append(kv("realised", money(rp), "ok" if rp >= 0 else "bad"))
        p.append(kv("unrealised", money(up), "ok" if up >= 0 else "bad"))
    p.append("</div>")

    # ---- controls ----
    p.append("<h2>Controls</h2><div class=card>")
    if halted:
        reason = ""
        try:
            reason = HALT_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        p.append(f"<div class=bad><b>Halted</b>: {e(reason or 'manually')}. No new orders until resumed. "
                 f"Resting orders at the exchange are not recalled by a halt.</div>"
                 f"<form method=post action=/resume><button class=btn-resume>Resume</button></form>")
    else:
        p.append("<form method=post action=/halt><input type=text name=reason placeholder='reason (optional)'>"
                 "<button class=btn-halt>Halt &mdash; stop placing new orders</button></form>")
    p.append("<details><summary>Risk limits</summary><form method=post action=/limits>")
    for key, label, typ in EDITABLE:
        p.append(f"<label>{e(label)}</label><input type={typ} step=any name={e(key)} "
                 f"value='{e(lim.get(key, ''))}' placeholder='default'>")
    p.append(f"<div class=toggle><input type=checkbox id=live name=TRADER_LIVE value=true"
             f"{' checked' if live_flag else ''}><label for=live style='margin:0'>LIVE &mdash; place real orders "
             f"(unticked = shadow, decides and logs only)</label></div>"
             f"<button class=btn-save>Save limits</button>"
             f"<div class='sub' style='margin-top:8px'>Takes effect on the next scan. Leverage above 1.0 "
             f"means borrowed money.</div></form></details></div>")

    # ---- positions ----
    pos = [r for r in data["positions"]
           if isinstance(r, dict) and ("error" in r or
           int(r.get("net_qty", r.get("quantity", r.get("qty", 0))) or 0))]
    p.append(f"<h2>Open positions ({len(pos)})</h2><div class=card>")
    if not pos:
        p.append("<div class=muted>none</div>")
    for r in pos:
        if "error" in r:
            p.append(f"<div class=bad>{e(r['error'])}</div>"); continue
        pnl = r.get("unrealized_pnl") or r.get("pnl") or r.get("realised_gains") or 0
        try:
            pcls = "ok" if Decimal(str(pnl)) >= 0 else "bad"
        except Exception:                               # noqa: BLE001
            pcls = ""
        p.append(f"<div class=row><div class=l><div class=sym>{e(r.get('name') or r.get('trading_symbol') or r.get('security_id'))}</div>"
                 f"<div class=sub>{e(r.get('net_qty', r.get('quantity', r.get('qty'))))} @ {money(r.get('avg_price') or r.get('buy_avg_price'))} "
                 f"&middot; {e(r.get('product'))}</div></div>"
                 f"<div class='r {pcls}'>{money(pnl)}</div></div>")
    p.append("</div>")

    # ---- orders ----
    live_orders = [r for r in data["orders"] if "error" in r or
                   str(r.get("status", "")).upper() not in ("CANCELLED", "REJECTED", "FAILED", "EXPIRED", "ABORTED")]
    dead_orders = [r for r in data["orders"] if r not in live_orders]
    p.append(f"<h2>Resting orders ({len(live_orders)})</h2><div class=card>")
    if not live_orders:
        p.append("<div class=muted>none</div>")
    for r in live_orders:
        if "error" in r:
            p.append(f"<div class=bad>{e(r['error'])}</div>"); continue
        st = str(r.get("status", "")).upper()
        leg = bool(r.get("parent_order_id"))
        what = (f"{e(r.get('txn_type'))} {e(r.get('requested_qty'))} &middot; "
                + (f"SL {e(r.get('sl_trigger_price'))} / TGT {e(r.get('tgt_trigger_price'))}" if leg
                   else f"trigger {e(r.get('trigger_price') or r.get('requested_price'))}"))
        p.append(f"<div class=row><div class=l><div class=sym>{e((r.get('name') or '')[:22])}"
                 f" <span class=muted style='font-weight:400;font-size:12px'>{'leg' if leg else 'entry'}</span></div>"
                 f"<div class=sub>{what} &middot; filled {e(r.get('traded_qty') or 0)}</div></div>"
                 f"<div class=r><span class='pill {'ok' if st == 'SUCCESS' else ''}'>{e(st)}</span></div></div>")
    if dead_orders:
        p.append(f"<details><summary>{len(dead_orders)} cancelled / rejected today</summary>")
        for r in dead_orders:
            p.append(f"<div class=row><div class=l><div class=sym style='font-weight:500'>{e((r.get('name') or '')[:22])}</div>"
                     f"<div class=sub>{e(r.get('txn_type'))} {e(r.get('requested_qty'))} &middot; {e(r.get('id'))}</div></div>"
                     f"<div class=r><span class='pill blocked'>{e(r.get('status'))}</span></div></div>")
        p.append("</details>")
    p.append("</div>")

    # ---- decisions ----
    p.append(f"<h2>Decisions &middot; {e(day)} ({len(log)})</h2><div class=card>")
    if not log:
        p.append("<div class=muted>no log for this day</div>")
    shown = 0
    for r in reversed(log):
        ev = r.get("event", "")
        if ev in ("ok",) and r.get("job") == "watch":
            continue                                    # quiet watcher ticks
        ts = (r.get("ts") or "")[11:16]
        sym = r.get("symbol") or r.get("name") or ""
        if ev == "blocked":
            detail = " / ".join(str(x) for x in (r.get("reasons") or []))
        elif ev in ("placed", "would_place"):
            detail = (f"{r.get('qty')} @ {r.get('entry')} &rarr; stop {r.get('stop')} / target {r.get('target')} "
                      f"&middot; risk {r.get('risk')} &middot; net if target {r.get('net_if_target')}")
        elif ev == "ALERT":
            detail = f"<b class=bad>{e(r.get('level'))}</b> {e(r.get('action') or '')}"
        else:
            skip = {"ts", "event", "job", "symbol", "name", "live", "payload"}
            detail = e("; ".join(f"{k}={v}" for k, v in r.items() if k not in skip)[:300])
        p.append(f"<div class=row><div class=l><div><span class='pill {e(ev)}'>{e(ev)}</span> "
                 f"<span class=sym>{e(sym)}</span></div><div class=sub>{detail}</div></div>"
                 f"<div class='r muted' style='font-size:12px'>{e(ts)}</div></div>")
        shown += 1
        if shown >= 60:
            p.append(f"<div class='muted sub'>&hellip; {len(log) - shown} earlier events</div>")
            break
    p.append("</div>")

    p.append(f"<h2>Timers</h2><div class=card><pre>{e(timers())}</pre></div>")
    p.append("<div class='sub' style='margin:20px 0'>Reachable only over Tailscale or localhost. "
             "This page can halt, resume and edit limits. It cannot place or cancel orders.</div>")
    return "".join(p)


def write_limits(form: dict) -> str:
    """Rewrite the TRADER_* lines in the env file. Validates before touching it."""
    new = {}
    for key, label, typ in EDITABLE:
        raw = (form.get(key) or [""])[0].strip()
        if not raw:
            continue
        try:
            val = Decimal(raw)
        except Exception:                               # noqa: BLE001
            return f"{label}: not a number"
        if val < 0:
            return f"{label}: must not be negative"
        if key == "TRADER_MAX_LEVERAGE" and val > 5:
            return "leverage above 5x refused"
        if key in ("TRADER_MAX_TRADES", "TRADER_MAX_CONCURRENT"):
            val = int(val)
        new[key] = str(val)
    new["TRADER_LIVE"] = "true" if (form.get("TRADER_LIVE") or [""])[0] == "true" else "false"

    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return f"cannot read env file: {exc}"
    kept = [ln for ln in lines if not any(ln.startswith(k + "=") for k in new)]
    kept += [f"{k}={v}" for k, v in new.items()]
    tmp = ENV_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    os.replace(tmp, ENV_FILE)
    return ""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _day(self) -> str:
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        day = (q.get("day") or [sess.now().strftime("%Y-%m-%d")])[0]
        try:
            datetime.strptime(day, "%Y-%m-%d")
            return day
        except ValueError:
            return sess.now().strftime("%Y-%m-%d")

    def do_GET(self):                                   # noqa: N802
        try:
            self._send(render(self._day()).encode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            self._send(f"<pre>dashboard error: {e(exc)}</pre>".encode(), 500)

    def do_POST(self):                                  # noqa: N802
        """Halt, resume, edit limits. Never an order: there is no route for
        one, and the only POSTs this server makes are none."""
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
        flash, err = "", False
        try:
            if path == "/halt":
                reason = (form.get("reason") or ["halted from dashboard"])[0].strip() or "halted from dashboard"
                HALT_FILE.write_text(reason + "\n", encoding="utf-8")
                flash = "Halted. No new orders will be placed. Resting orders at the exchange remain."
            elif path == "/resume":
                try:
                    HALT_FILE.unlink()
                except FileNotFoundError:
                    pass
                flash = "Resumed. The next scan may place orders."
            elif path == "/limits":
                problem = write_limits(form)
                flash, err = (problem, True) if problem else ("Limits saved. They apply from the next scan.", False)
            else:
                flash, err = "unknown action", True
        except Exception as exc:                        # noqa: BLE001
            flash, err = f"failed: {exc}", True
        try:
            self._send(render(self._day(), flash, err).encode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            self._send(f"<pre>dashboard error: {e(exc)}</pre>".encode(), 500)

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
