"""
Read-only dashboard. Standard library only. Binds to localhost.

Reach it over an SSH tunnel, never a public port -- this box holds broker
credentials and can place orders, and "nobody knows the URL" is not a
security model:

    ssh -L 8080:127.0.0.1:8080 -i ~/.ssh/lightsail.pem ubuntu@13.127.164.72
    # then open http://localhost:8080 on your laptop

Shows: funds and P&L, open positions, resting orders (each entry paired
with its OCO stop/target leg and the reason it was placed), today's
decisions, the limits in force, and the timers.

Writes: halt / resume / limits, CANCEL (an entry and its linked leg
together), EXIT (market-sell an open intraday position), and CHAT, which
routes through chat.py: stop, continue, scan interval, limits, cancel,
sell, and a capped market BUY (at most half of cash). Every write goes
through the same code the timers use.
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
            if (line.startswith("TRADER_") or line.startswith("ANTHROPIC_API_KEY")) and "=" in line:
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
                         if isinstance(r, dict)]
    except Exception as exc:                            # noqa: BLE001
        out["orders"] = [{"error": str(exc)[:120]}]
    return out


def ltps(client, security_ids) -> dict:
    out = {}
    codes = [f"NSE_{s}" for s in dict.fromkeys(str(s) for s in security_ids if s)]
    for i in range(0, len(codes), 5):
        try:
            data = (client.get("/market/quotes/ltp",
                               params={"scrip-codes": ",".join(codes[i:i + 5])})
                    or {}).get("data") or {}
        except Exception:                               # noqa: BLE001
            continue
        for code, blk in data.items():
            try:
                out[code.split("_", 1)[1]] = Decimal(str((blk or {}).get("live_price")))
            except Exception:                           # noqa: BLE001
                pass
    return out


def placed_records(log: list) -> dict:
    """parent_order_id -> the autotrader's placement record (the why)."""
    return {str(r.get("parent_order_id")): r for r in log
            if r.get("event") == "placed" and r.get("parent_order_id")}


def pair_orders(rows: list) -> list:
    """[(entry_row, leg_row_or_None)] for every parent in the book."""
    by_parent = {}
    for r in rows:
        pid = r.get("parent_order_id")
        if pid:
            by_parent.setdefault(str(pid), r)
    out = []
    for r in rows:
        if r.get("parent_order_id"):
            continue
        out.append((r, by_parent.get(str(r.get("id") or r.get("order_id")))))
    return out


DEAD = ("CANCELLED", "REJECTED", "FAILED", "EXPIRED", "ABORTED")


def status_of(entry: dict, leg) -> tuple:
    """(css class, label) the way the broker app words it."""
    st = str(entry.get("status", "")).upper()
    traded = int(entry.get("traded_qty") or 0)
    if st in DEAD:
        return "dead", st.title()
    if traded >= int(entry.get("requested_qty") or 0) and traded > 0:
        return "fill", "Filled - SL / Target live" if leg else "Filled"
    if traded > 0:
        return "fill", f"Partly filled {traded}"
    if str(entry.get("order_type", "")).upper() in ("TRIGGER", "GTT", "GTT_LIMIT", "SL", "SL-M") \
            or entry.get("trigger_price"):
        return "wait", "Waiting for Trigger Price"
    return "wait", st.title() or "Pending"


def term_history(n: int) -> list:
    try:
        lines = (STATE_DIR / "term.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def run_terminal(cmd: str) -> dict:
    """Run a shell command on the box. The page is reachable only over
    Tailscale from the owner's devices: the same trust as their SSH key."""
    try:
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=60, cwd="/opt/indstocks/day-trader-skill")
        out, rc = (r.stdout + r.stderr), r.returncode
    except subprocess.TimeoutExpired as exc:
        out, rc = (exc.stdout or "") + (exc.stderr or "") + "\n[timed out after 60 s]", 124
    rec = {"ts": sess.now().isoformat(timespec="seconds"), "cmd": cmd,
           "out": out[-20000:], "rc": rc}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / "term.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


def dash_log(record: dict) -> None:
    rec = {"ts": sess.now().isoformat(timespec="seconds"), "job": "dash", **record}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def cancel_pair(client, order_id: str) -> str:
    """Cancel an entry and its linked leg. Only ids present in today's book."""
    from flatten import cancel_order
    rows = (client.get("/order-book") or {}).get("data") or []
    rows = [r for r in rows if isinstance(r, dict)]
    ids = {str(r.get("id") or r.get("order_id")): r for r in rows}
    if order_id not in ids:
        return f"order {order_id} is not in today's order book"
    targets = [order_id] + [str(r.get("id") or r.get("order_id")) for r in rows
                            if str(r.get("parent_order_id") or "") == order_id]
    done, failed = [], []
    for oid in targets:
        if str(ids.get(oid, {}).get("status", "")).upper() in DEAD:
            continue
        try:
            cancel_order(client, oid)
            done.append(oid)
        except Exception as exc:                        # noqa: BLE001
            failed.append(f"{oid}: {str(exc)[:80]}")
    dash_log({"event": "cancelled_from_dashboard", "name": ids[order_id].get("name"),
              "order_ids": done, "failed": failed})
    if failed:
        return "cancelled " + ", ".join(done) + "; FAILED " + "; ".join(failed)
    return f"Cancelled {ids[order_id].get('name')}: entry and its SL/Target leg ({len(done)} orders)."


def exit_position(client, security_id: str) -> str:
    from flatten import cancel_resting_legs, open_intraday, sell
    pos = [p for p in open_intraday(client) if p["security_id"] == security_id]
    if not pos:
        return f"no open intraday position with security_id {security_id}"
    p = pos[0]
    legs = cancel_resting_legs(client, security_id)
    if legs:
        import time
        time.sleep(1)
    resp = sell(client, p)
    oid = ((resp or {}).get("data") or {}).get("order_id")
    dash_log({"event": "sold", "name": p["name"], "qty": p["qty"], "order_id": oid,
              "reason": "exit from dashboard", "cancelled_legs": legs})
    return f"Exit sent for {p['name']} x{p['qty']} at market (order {oid})."


# ------------------------------------------------------------------ page

CSS = """
:root{color-scheme:dark;--bg:#0b0d12;--card:#151921;--line:#232833;--fg:#f2f3f5;--mut:#8b93a1;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--acc:#58a6ff;--orange:#e8833a}
*{box-sizing:border-box}
body{font:15px/1.4 -apple-system,system-ui,Inter,sans-serif;margin:0;padding:0 0 70px;color:var(--fg);background:var(--bg)}
.wrap{padding:12px}
h1{font-size:20px;margin:0}
h2{font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);margin:22px 0 8px}
.top{position:sticky;top:0;background:var(--bg);z-index:5;padding:12px 12px 0;border-bottom:1px solid var(--line)}
.tabs{display:flex;gap:18px;overflow-x:auto;font-size:17px;font-weight:600;color:var(--mut);padding:10px 0 0}
.tabs a{color:inherit;text-decoration:none;padding-bottom:8px;white-space:nowrap}
.tabs a.on{color:var(--fg);border-bottom:2px solid var(--fg)}
.ticker{font-size:13px;color:var(--mut);padding:8px 0}
.ticker b{color:var(--fg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.pnl{font-size:15px;color:var(--mut)}.pnl .v{font-size:26px;font-weight:700;margin-top:4px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.muted{color:var(--mut)}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.logo{width:40px;height:40px;border-radius:50%;background:#fff;color:#111;font-weight:800;display:flex;align-items:center;justify-content:center;font-size:14px;flex:none}
.nm{font-weight:700;font-size:17px}.tk{font-size:13px;color:var(--mut)}
.badge{display:inline-block;border:1px solid;border-radius:5px;font-size:12px;font-weight:700;padding:0 6px;line-height:20px}
.B{color:var(--ok);border-color:var(--ok)}.S{color:#60a5fa;border-color:#60a5fa}
.st{display:inline-block;border-radius:8px;padding:4px 10px;font-size:13px;font-weight:600;white-space:nowrap}
.st.wait{background:#3a2414;color:var(--orange)}.st.arm{background:#3a2414;color:var(--orange)}
.st.fill{background:#14301f;color:var(--ok)}.st.dead{background:#3a1a1a;color:var(--bad)}
.meta{font-size:14px;color:var(--mut);margin-top:4px}.meta b{color:var(--fg);font-weight:500}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:14px}
.g3 .k{font-size:13px;color:var(--mut)}.g3 .v{font-size:18px;font-weight:700;margin-top:2px}
.g3 div:nth-child(2){text-align:center}.g3 div:nth-child(3){text-align:right}
.why{margin-top:12px;padding:10px 12px;background:var(--bg);border-radius:10px;font-size:13px;color:var(--mut);line-height:1.5}
.why b{color:var(--fg)}
.why .row2{display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px dashed var(--line)}
.why .row2:last-child{border-bottom:0}
.btns{display:flex;gap:10px;margin-top:14px}
.btns>*{flex:1;margin:0}
button,.btn{white-space:nowrap;padding:12px 10px;border:1px solid var(--line);border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;width:100%;background:var(--card);color:var(--fg);text-align:center;text-decoration:none;display:block}
.btn-x{color:var(--bad);border-color:#4a2a2a}.btn-save{background:var(--acc);color:#000;border:0}.btn-halt{background:var(--bad);color:#fff;border:0}.btn-resume{background:var(--ok);color:#000;border:0}
.leg{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.pill{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;background:#262b35;color:var(--fg)}
.pill.placed,.pill.sold,.pill.ok,.pill.cancelled_on_fill{background:#1b3a26;color:var(--ok)}
.pill.blocked,.pill.ALERT,.pill.rejected_locally,.pill.halted,.pill.sell_failed,.pill.cancel_failed,.pill.error{background:#3d1d1d;color:var(--bad)}
.pill.would_place,.pill.run_start,.pill.run_end,.pill.reconciled_out,.pill.state,.pill.sizing{background:#1d2d44;color:var(--acc)}
.pill.unsized,.pill.skip,.pill.no_data,.pill.stop{background:#2d2a1a;color:var(--warn)}
.row{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}.row .l{flex:1;min-width:0}.row .r{text-align:right;white-space:nowrap}
.sym{font-weight:700}.sub{font-size:12px;color:var(--mut);margin-top:2px;word-break:break-word}
form{margin:0}label{display:block;font-size:12px;color:var(--mut);margin:8px 0 3px}
input[type=number],input[type=text]{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font-size:16px}
.toggle{display:flex;align-items:center;gap:10px;padding:10px 0}.toggle input{width:22px;height:22px}
.copy{background:#262b35;color:var(--fg);border:0;border-radius:6px}
a[data-cmd]{color:var(--acc);text-decoration:none}
details summary{cursor:pointer;color:var(--acc);font-size:13px;padding:6px 0}
.flash{background:#1b3a26;color:var(--ok);padding:10px;border-radius:8px;margin-bottom:10px;font-size:14px}
.flash.err{background:#3d1d1d;color:var(--bad)}
pre{background:var(--bg);padding:8px;font-size:11px;overflow:auto;border-radius:6px;margin:0}
.chatbox{display:flex;flex-direction:column;gap:8px;padding:4px 0 12px;min-height:40vh}
.bub{max-width:88%;padding:10px 12px;border-radius:14px;font-size:14px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.bub.me{align-self:flex-end;background:#1d3a5c;border-bottom-right-radius:4px}
.bub.bot{align-self:flex-start;background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px}
.bub .t{font-size:10px;color:var(--mut);margin-top:4px;text-align:right}
.chatform{display:flex;gap:8px;position:sticky;bottom:70px}
.chatform input[type=text]{flex:1}
.nav{position:fixed;bottom:0;left:0;right:0;background:#0f1218;border-top:1px solid var(--line);display:flex;justify-content:space-around;padding:10px 0 14px;font-size:12px;color:var(--mut)}
.nav a{color:inherit;text-decoration:none;text-align:center;padding:0 10px}.nav a.on{color:var(--acc)}.nav a span{display:block;font-size:18px}
@media(min-width:720px){.wrap,.top{max-width:720px;margin:0 auto}}
"""


TABS_JS = """
(function(){
  var q=new URLSearchParams(location.search), tab=q.get('tab');
  try{ tab=tab||localStorage.getItem('tab'); }catch(e){}
  if(!tab) tab='pos';
  function show(t){
    document.querySelectorAll('[data-pane]').forEach(function(s){ s.style.display = s.dataset.pane===t ? '' : 'none'; });
    document.querySelectorAll('[data-tab]').forEach(function(a){ a.classList.toggle('on', a.dataset.tab===t); });
    try{ localStorage.setItem('tab', t); }catch(e){}
    var u=new URL(location); u.searchParams.set('tab', t); history.replaceState(null,'',u);
    window.scrollTo(0,0);
  }
  document.querySelectorAll('[data-tab]').forEach(function(a){
    a.addEventListener('click', function(ev){ ev.preventDefault(); show(a.dataset.tab); });
  });
  document.querySelectorAll('[data-copy]').forEach(function(b){
    b.addEventListener('click', function(){
      var txt=b.dataset.copy;
      (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(function(){ b.textContent='Copied'; },
        function(){ var ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); b.textContent='Copied'; });
    });
  });
  document.querySelectorAll('[data-cmd]').forEach(function(a){
    a.addEventListener('click', function(ev){ ev.preventDefault(); var f=document.querySelector('form[action=\"/term\"] input[name=cmd]'); f.value=a.dataset.cmd; f.form.submit(); });
  });
  show(tab);
  var cb=document.getElementById('chatbox'); if(cb && tab==='chat'){ window.scrollTo(0, document.body.scrollHeight); }
  setTimeout(function(){ if(tab!=='term' && tab!=='chat') location.reload(); }, 60000);
})();
"""

EDITABLE = (
    ("TRADER_MAX_LOSS", "max loss per trade (Rs)", "number"),
    ("TRADER_DAILY_LOSS", "daily loss limit (Rs)", "number"),
    ("TRADER_MAX_TRADES", "max trades per day", "number"),
    ("TRADER_MAX_CONCURRENT", "max concurrent positions", "number"),
    ("TRADER_MAX_LEVERAGE", "max leverage (1.0 = cash only)", "number"),
    ("TRADER_STOP_BAND_PCT", "stop band % (room for a gap to fill)", "number"),
    ("TRADER_MAX_RESTING", "max resting orders at once", "number"),
    ("TRADER_RESTING_TTL_MIN", "cancel a resting order after N minutes (0 = never)", "number"),
    ("TRADER_MIN_ORDER_VALUE", "smallest order value (Rs)", "number"),
    ("TRADER_MIN_REWARD_TO_COST", "gain at target must be N x charges (min 1)", "number"),
    ("TRADER_MANUAL_TARGET_PCT", "manual position: sell when up %", "number"),
    ("TRADER_MANUAL_STOP_PCT", "manual position: sell when down %", "number"),
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
    whys = placed_records(log)

    pos = [r for r in data["positions"] if isinstance(r, dict) and "error" not in r
           and int(r.get("net_qty", r.get("quantity", r.get("qty", 0))) or 0) != 0]
    pairs = pair_orders([r for r in data["orders"] if "error" not in r])
    live_pairs = [(a, b) for a, b in pairs if str(a.get("status", "")).upper() not in DEAD]
    dead_pairs = [(a, b) for a, b in pairs if str(a.get("status", "")).upper() in DEAD]
    px = ltps(client, [r.get("security_id") for r in pos] +
              [a.get("security_id") for a, _ in live_pairs])

    try:
        cash = Decimal(str((f.get("detailed_avl_balance") or {}).get("eq_mis") or 0))
    except Exception:                                   # noqa: BLE001
        cash = Decimal(0)

    state = ("<span class=bad>HALTED</span>" if halted else
             ("<span class=ok>LIVE</span>" if live_flag else "<span class=warn>SHADOW</span>"))
    p = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>trader</title><style>{CSS}</style>",
         f"<div class=top><div class=head><h1>autotrader</h1><div class=muted style='font-size:13px'>"
         f"{e(now.strftime('%a %d %b %H:%M'))}</div></div>"
         f"<div class=tabs><a data-tab=chat href='?tab=chat&day={e(day)}'>Chat</a>"
         f"<a data-tab=pos href='?tab=pos&day={e(day)}'>Positions</a>"
         f"<a data-tab=orders href='?tab=orders&day={e(day)}'>Orders</a>"
         f"<a data-tab=decisions href='?tab=decisions&day={e(day)}'>Decisions</a>"
         f"<a data-tab=controls href='?tab=controls&day={e(day)}'>Controls</a>"
         f"<a data-tab=term href='?tab=term&day={e(day)}'>Terminal</a></div>"
         f"<div class=ticker><b>{e(phase)}</b> &middot; {state} &middot; cash <b>{money(cash)}</b>"
         f" &middot; max {e(lim.get('TRADER_MAX_CONCURRENT', '2'))} positions &middot; "
         f"leverage cap {e(lim.get('TRADER_MAX_LEVERAGE', '1.0'))}x</div></div><div class=wrap>"]
    if flash:
        p.append(f"<div class='flash{' err' if flash_err else ''}'>{e(flash)}</div>")

    # ---- chat ----
    import chat as chat_mod
    has_key = bool(lim.get("ANTHROPIC_API_KEY"))
    p.append("<section data-pane=chat><div class=chatbox id=chatbox>")
    msgs = chat_mod.history(60)
    if not msgs:
        p.append("<div class='bub bot'>Hi. Tell me <b>stop</b>, <b>continue</b>, <b>scan every 5 minutes</b>, "
                 "<b>buy 10 RELIANCE</b>, <b>sell all</b>, <b>cancel all</b>, <b>status</b> or <b>why HDFCBANK</b>."
                 + ("" if (has_key or chat_mod.find_claude()) else " No Claude login found on the server, so only commands work.")
                 + "</div>")
    for m in msgs:
        role = m.get("role")
        if role == "system":
            continue
        cls = "me" if role == "user" else "bot"
        p.append(f"<div class='bub {cls}'>{e(m.get('text', '')).replace(chr(10), '<br>')}"
                 f"<div class=t>{e((m.get('ts') or '')[11:16])}</div></div>")
    p.append("</div><form method=post action=/chat class=chatform autocomplete=off>"
             f"<input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=chat>"
             "<input type=text name=text placeholder='stop / continue / buy 10 RELIANCE / why ITC / status' required>"
             "<button class=btn-save style='width:auto;margin:0'>Send</button></form>"
             f"<div class=meta style='margin-top:8px'>{'AI replies via API key' if has_key else ('AI replies via the server Claude login' if chat_mod.find_claude() else 'Command mode only')}"
             f" &middot; scan every {e(lim.get('TRADER_SCAN_MINUTES', '10'))} min</div></section>")
    p.append("<section data-pane=pos>")
    # ---- P&L card ----
    rp = Decimal(str(f.get("realized_pnl") or 0)) if "error" not in f else Decimal(0)
    up = Decimal(str(f.get("unrealized_pnl") or 0)) if "error" not in f else Decimal(0)
    tot = rp + up
    p.append(f"<div class=card><div class=pnl>Today's Intraday P&amp;L"
             f"<div class='v {'ok' if tot >= 0 else 'bad'}'>{'' if tot < 0 else '+'}&#8377;{money(tot)}</div>"
             f"<div class=meta>realised <b class='{'ok' if rp >= 0 else 'bad'}'>{money(rp)}</b> &middot; "
             f"unrealised <b class='{'ok' if up >= 0 else 'bad'}'>{money(up)}</b></div></div></div>")
    if "error" in f:
        p.append(f"<div class='card bad'>{e(f['error'])}</div>")

    # ---- positions ----
    held_value = Decimal(0)
    p.append(f"<h2>Live positions ({len(pos)})</h2>")
    if not pos:
        p.append("<div class='card muted'>No open intraday positions.</div>")
    for r in pos:
        sid = str(r.get("security_id"))
        qty = int(r.get("net_qty", r.get("quantity", r.get("qty", 0))) or 0)
        avg = Decimal(str(r.get("avg_price") or r.get("buy_avg_price") or 0))
        ltp = px.get(sid, Decimal(0))
        inv, cur = avg * qty, ltp * qty
        held_value += cur
        pnl = cur - inv if ltp else Decimal(str(r.get("unrealized_pnl") or r.get("pnl") or 0))
        pct = (pnl / inv * 100) if inv else Decimal(0)
        cls = "ok" if pnl >= 0 else "bad"
        name = r.get("name") or r.get("trading_symbol") or sid
        ours = any(a.get("security_id") == sid and int(a.get("traded_qty") or 0) > 0
                   and str(a.get("remarks", "")).startswith("auto/") for a, _ in pairs)
        p.append(f"<div class=card><div class=head><div style='display:flex;gap:10px'>"
                 f"<div class=logo>{e(str(name)[:2].upper())}</div><div><div class=nm>{e(name)}</div>"
                 f"<div class=meta>&#8377;{money(ltp)} &middot; {e(r.get('product'))} &middot; "
                 f"{'bot' if ours else '<span class=warn>manual</span>'}</div></div></div>"
                 f"<div style='text-align:right'><div class=meta>Avg. <b>&#8377;{money(avg)}</b></div>"
                 f"<div class=meta>Qty <b>{'+' if qty > 0 else ''}{qty}</b> <span class='badge {'B' if qty > 0 else 'S'}'>{'B' if qty > 0 else 'S'}</span></div></div></div>"
                 f"<div class=g3><div><div class=k>Invested Value</div><div class=v>&#8377;{money(inv)}</div></div>"
                 f"<div><div class=k>Current Value</div><div class=v>&#8377;{money(cur)}</div></div>"
                 f"<div><div class=k>P&amp;L (<span class={cls}>{pct:+.2f}%</span>)</div><div class='v {cls}'>{'+' if pnl >= 0 else ''}&#8377;{money(pnl)}</div></div></div>")
        if not ours:
            p.append(f"<div class=why>Manual position. The fill monitor sells it at market when it is up "
                     f"<b>{e(lim.get('TRADER_MANUAL_TARGET_PCT', '1.0'))}%</b> or down "
                     f"<b>{e(lim.get('TRADER_MANUAL_STOP_PCT', '0.5'))}%</b> from {money(avg)}, "
                     f"and the 15:05 flatten closes it regardless.</div>")
        p.append(f"<div class=btns><form method=post action=/exit onsubmit=\"return confirm('Sell {e(name)} x{qty} at market now?')\">"
                 f"<input type=hidden name=security_id value='{e(sid)}'><input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=pos>"
                 f"<button class=btn-x>&#10162; Insta Exit</button></form>"
                 f"<a class=btn data-tab=orders href='?tab=orders&day={e(day)}'>Manage</a></div></div>")
    if pos and cash:
        lev = held_value / cash
        p.append(f"<div class=meta style='margin:-4px 0 8px'>Held &#8377;{money(held_value)} against cash "
                 f"&#8377;{money(cash)} = <b class='{'bad' if lev > Decimal(lim.get('TRADER_MAX_LEVERAGE', '1.0') or 1) else 'ok'}'>{lev:.2f}x</b> leverage</div>")

    p.append("</section><section data-pane=orders>")
    # ---- orders ----
    p.append(f"<h2>Open orders ({len(live_pairs)})</h2>")
    if not live_pairs:
        p.append("<div class='card muted'>No resting orders. The scanner runs every 10 minutes 09:30-14:30.</div>")
    for entry, leg in live_pairs:
        sid = str(entry.get("security_id"))
        oid = str(entry.get("id") or entry.get("order_id"))
        name = entry.get("name") or sid
        ltp = px.get(sid)
        cls, label = status_of(entry, leg)
        side = str(entry.get("txn_type", "B"))[:1].upper()
        why = whys.get(oid, {})
        req = int(entry.get("requested_qty") or why.get("qty") or 0)
        traded = int(entry.get("traded_qty") or 0)
        trig = entry.get("trigger_price") or entry.get("requested_price") or why.get("entry")
        ours = str(entry.get("remarks", "")).startswith("auto/")
        when = (entry.get("created_at") or entry.get("order_time") or entry.get("exch_time")
                or (why.get("ts") or "")[11:16])
        p.append(f"<div class=card><div class=head><div style='display:flex;gap:10px'>"
                 f"<div class=logo>{e(str(name)[:2].upper())}</div><div><div class=tk>{e(why.get('symbol') or sid)}</div>"
                 f"<div class=nm>{e(name)}</div><div class=meta><span class='badge {side}'>{side}</span> &nbsp;"
                 f"{e(entry.get('order_type') or 'GTT')} &middot; LTP: <b>&#8377;{money(ltp) if ltp is not None else '?'}</b></div></div></div>"
                 f"<span class='st {cls}'>{e(label)}</span></div>"
                 f"<div class=g3><div><div class=k>Placed</div><div class=v style='font-size:15px'>{e(str(when)[:16]) or '-'}</div></div>"
                 f"<div><div class=k>Trigger</div><div class=v>&#8377;{money(trig)}</div></div>"
                 f"<div><div class=k>Qty Filled</div><div class=v>{traded}/{req}</div></div></div>")
        if leg:
            lcls = "arm" if traded == 0 else "fill"
            p.append(f"<div class=leg><div class=head><div class=meta><span class='badge S'>S</span> &nbsp;OCO &middot; "
                     f"linked sell for the same {req}</div>"
                     f"<span class='st {lcls}'>{'Activating SL / Target' if traded == 0 else 'SL / Target live'}</span></div>"
                     f"<div class=g3><div><div class=k>Stop loss</div><div class='v bad'>&#8377;{money(leg.get('sl_trigger_price') or why.get('stop'))}</div></div>"
                     f"<div><div class=k>Target</div><div class='v ok'>&#8377;{money(leg.get('tgt_trigger_price') or why.get('target'))}</div></div>"
                     f"<div><div class=k>Status</div><div class=v style='font-size:14px'>{e(str(leg.get('status', '')).title())}</div></div></div></div>")
        if why:
            p.append(f"<div class=why><b>Why:</b> {e(why.get('why', ''))}"
                     f"<div style='margin-top:8px'>"
                     f"<div class=row2><span>Position value</span><b>&#8377;{money(why.get('position_value'))}</b></div>"
                     f"<div class=row2><span>Leverage used (this order / cash)</span><b>{e(why.get('leverage'))}x</b></div>"
                     f"<div class=row2><span>Max loss at stop</span><b class=bad>-&#8377;{money(why.get('risk'))}</b></div>"
                     f"<div class=row2><span>Profit if target hits (gross)</span><b class=ok>+&#8377;{money(why.get('gross_if_target'))}</b></div>"
                     f"<div class=row2><span>Charges, round trip</span><b>-&#8377;{money(why.get('costs'))}</b></div>"
                     f"<div class=row2><span>Net profit if target hits</span><b class='{'ok' if Decimal(str(why.get('net_if_target') or 0)) >= 0 else 'bad'}'>&#8377;{money(why.get('net_if_target'))}</b></div>"
                     f"<div class=row2><span>LTP when placed</span><b>&#8377;{money(why.get('ltp_at_placement'))}</b></div>"
                     f"</div></div>")
        elif not ours:
            p.append("<div class=why>Placed outside the autotrader (no decision record).</div>")
        p.append(f"<div class=btns><form method=post action=/cancel onsubmit=\"return confirm('Cancel {e(name)} entry AND its SL/Target leg?')\">"
                 f"<input type=hidden name=order_id value='{e(oid)}'><input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=orders>"
                 f"<button class=btn-x>&#10006; Cancel both</button></form></div></div>")
    if dead_pairs:
        p.append(f"<details><summary>{len(dead_pairs)} cancelled / rejected / done today</summary><div class=card>")
        for entry, leg in dead_pairs:
            p.append(f"<div class=row><div class=l><div class=sym>{e((entry.get('name') or '')[:22])}</div>"
                     f"<div class=sub>{e(entry.get('txn_type'))} {e(entry.get('requested_qty'))} &middot; filled {e(entry.get('traded_qty') or 0)} &middot; {e(entry.get('id'))}</div></div>"
                     f"<div class=r><span class='pill blocked'>{e(entry.get('status'))}</span></div></div>")
        p.append("</div></details>")

    p.append("</section><section data-pane=decisions>")
    # ---- decisions ----
    p.append(f"<h2>Decisions &middot; {e(day)} ({len(log)})</h2><div class=card>")
    if not log:
        p.append("<div class=muted>no log for this day</div>")
    shown = 0
    for r in reversed(log):
        ev = r.get("event", "")
        if (ev == "ok" and r.get("job") == "watch") or ev == "state":
            continue
        ts = (r.get("ts") or "")[11:16]
        sym = r.get("symbol") or r.get("name") or ""
        if ev == "blocked":
            detail = e(" / ".join(str(x) for x in (r.get("reasons") or [])))
        elif ev in ("placed", "would_place"):
            detail = e(f"{r.get('qty')} @ {r.get('entry')} -> stop {r.get('stop')} / target {r.get('target')} "
                       f"| risk {r.get('risk')} | net if target {r.get('net_if_target')} | {r.get('leverage')}x")
        elif ev == "ALERT":
            detail = f"<b class=bad>{e(r.get('level'))}</b> {e(r.get('action') or '')}"
        else:
            skip = {"ts", "event", "job", "symbol", "name", "live", "payload"}
            detail = e("; ".join(f"{k}={v}" for k, v in r.items() if k not in skip)[:300])
        p.append(f"<div class=row><div class=l><div><span class='pill {e(ev)}'>{e(ev)}</span> "
                 f"<span class=sym>{e(sym)}</span> <span class=muted style='font-size:11px'>{e(r.get('job', 'auto'))}</span></div>"
                 f"<div class=sub>{detail}</div></div>"
                 f"<div class='r muted' style='font-size:12px'>{e(ts)}</div></div>")
        shown += 1
        if shown >= 80:
            p.append(f"<div class='muted sub'>&hellip; {len(log) - shown} earlier events</div>")
            break
    p.append("</div>")

    p.append("</section><section data-pane=controls>")
    # ---- controls ----
    p.append("<h2>Controls</h2><div class=card>")
    if halted:
        reason = ""
        try:
            reason = HALT_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        p.append(f"<div class=bad><b>Halted</b>: {e(reason or 'manually')}. No new orders until resumed. "
                 f"Resting orders stay at the exchange; cancel them above if you want them gone.</div>"
                 f"<form method=post action=/resume><input type=hidden name=tab value=controls><button class=btn-resume>Resume</button></form>")
    else:
        p.append("<form method=post action=/halt><input type=hidden name=tab value=controls><input type=text name=reason placeholder='reason (optional)'>"
                 "<button class=btn-halt style='margin-top:10px'>Halt &mdash; stop placing new orders</button></form>")
    p.append("<details><summary>Risk limits</summary><form method=post action=/limits><input type=hidden name=tab value=controls>")
    for key, label, typ in EDITABLE:
        p.append(f"<label>{e(label)}</label><input type={typ} step=any name={e(key)} "
                 f"value='{e(lim.get(key, ''))}' placeholder='default'>")
    p.append(f"<label>Anthropic API key (for chat; leave blank to keep{' the saved one' if lim.get('ANTHROPIC_API_KEY') else ''})</label>"
             f"<input type=password name=ANTHROPIC_API_KEY placeholder='{'saved' if lim.get('ANTHROPIC_API_KEY') else 'sk-ant-...'}'>")
    p.append(f"<div class=toggle><input type=checkbox id=live name=TRADER_LIVE value=true"
             f"{' checked' if live_flag else ''}><label for=live style='margin:0'>LIVE &mdash; place real orders "
             f"(unticked = shadow, decides and logs only)</label></div>"
             f"<button class=btn-save style='margin-top:10px'>Save limits</button>"
             f"<div class='sub' style='margin-top:8px'>Takes effect on the next scan. Leverage above 1.0 "
             f"means borrowed money.</div></form></details></div>")

    p.append(f"<h2>Timers</h2><div class=card><pre>{e(timers())}</pre></div>")
    p.append("</section><section data-pane=term>")
    # ---- terminal ----
    p.append("<form method=post action=/term class=chatform autocomplete=off>"
             f"<input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=term>"
             "<input type=text name=cmd placeholder='sudo tail -n 30 /var/lib/indstocks/auto/$(date +%F).jsonl' required>"
             "<button class=btn-save style='width:auto;margin:0'>Run</button></form>"
             "<div class=meta style='margin:6px 0 10px'>Runs on the server as root, 60 s limit. Shortcuts: "
             "<a href='#' data-cmd='sudo tail -n 40 /var/lib/indstocks/auto/$(TZ=Asia/Kolkata date +%F).jsonl'>today's log</a> &middot; "
             "<a href='#' data-cmd='systemctl status auto.timer fillmonitor dashboard --no-pager | head -40'>services</a> &middot; "
             "<a href='#' data-cmd='sudo journalctl -u auto.service -n 60 --no-pager'>last scan</a> &middot; "
             "<a href='#' data-cmd='sudo journalctl -u fillmonitor -n 40 --no-pager'>fill monitor</a> &middot; "
             "<a href='#' data-cmd='cd /opt/indstocks/day-trader-skill && git log --oneline -8'>deployed version</a></div>")
    for run in reversed(term_history(12)):
        p.append(f"<div class=card><div class=head><code class=muted style='word-break:break-all'>$ {e(run.get('cmd'))}</code>"
                 f"<button class=copy data-copy='{e(run.get('out', ''))}' style='width:auto;padding:6px 10px;margin:0;font-size:12px'>Copy</button></div>"
                 f"<pre style='margin-top:8px;max-height:60vh'>{e(run.get('out', ''))}</pre>"
                 f"<div class=meta>exit {e(run.get('rc'))} &middot; {e((run.get('ts') or '')[11:19])}</div></div>")
    if not term_history(1):
        p.append("<div class='card muted'>No commands run yet.</div>")
    p.append("<div class='sub' style='margin:20px 0'>Reachable only over Tailscale or localhost. "
             "Cancel and Exit reduce exposure; the only way to open a position from here is a chat 'buy', capped at half of cash.</div>"
             "</section></div>"
             f"<div class=nav><a data-tab=chat href='?tab=chat&day={e(day)}'><span>&#128172;</span>Chat</a>"
             f"<a data-tab=pos href='?tab=pos&day={e(day)}'><span>&#8377;</span>Positions</a>"
             f"<a data-tab=orders href='?tab=orders&day={e(day)}'><span>&#9776;</span>Orders</a>"
             f"<a data-tab=decisions href='?tab=decisions&day={e(day)}'><span>&#9881;</span>Decisions</a>"
             f"<a data-tab=controls href='?tab=controls&day={e(day)}'><span>&#9211;</span>Controls</a>"
             f"<a data-tab=term href='?tab=term&day={e(day)}'><span>&gt;_</span>Terminal</a></div>"
             f"<script>{TABS_JS}</script>")
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
        if key == "TRADER_MIN_REWARD_TO_COST" and val < 1:
            return "reward-to-cost below 1 means taking trades that lose when they win; refused"
        if key in ("TRADER_MAX_TRADES", "TRADER_MAX_CONCURRENT", "TRADER_MAX_RESTING", "TRADER_RESTING_TTL_MIN"):
            val = int(val)
        new[key] = str(val)
    new["TRADER_LIVE"] = "true" if (form.get("TRADER_LIVE") or [""])[0] == "true" else "false"
    key = (form.get("ANTHROPIC_API_KEY") or [""])[0].strip()
    if key:
        if not key.startswith("sk-ant-") or len(key) < 40:
            return "that does not look like an Anthropic API key"
        new["ANTHROPIC_API_KEY"] = key

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

    def _query(self) -> dict:
        from urllib.parse import parse_qs, urlparse
        return parse_qs(urlparse(self.path).query)

    def _day(self, form: dict | None = None) -> str:
        q = form or self._query()
        day = (q.get("day") or [sess.now().strftime("%Y-%m-%d")])[0]
        try:
            datetime.strptime(day, "%Y-%m-%d")
            return day
        except ValueError:
            return sess.now().strftime("%Y-%m-%d")

    def do_GET(self):                                   # noqa: N802
        q = self._query()
        flash = (q.get("flash") or [""])[0]
        err = (q.get("err") or ["0"])[0] == "1"
        try:
            self._send(render(self._day(), flash, err).encode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            self._send(f"<pre>dashboard error: {e(exc)}</pre>".encode(), 500)

    def do_POST(self):                                  # noqa: N802
        """Halt, resume, edit limits, cancel an order pair, exit a position.
        Every order route reduces exposure; none opens any."""
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
            elif path == "/chat":
                import chat as chat_mod
                text = (form.get("text") or [""])[0].strip()[:500]
                if text:
                    chat_mod.handle(text, Client())
                flash = ""
            elif path == "/term":
                cmd = (form.get("cmd") or [""])[0].strip()[:2000]
                if cmd:
                    run_terminal(cmd)
                flash = ""
            elif path == "/cancel":
                oid = (form.get("order_id") or [""])[0].strip()
                if not oid:
                    flash, err = "no order id", True
                else:
                    flash = cancel_pair(Client(), oid)
                    err = "FAILED" in flash or "not in" in flash
            elif path == "/exit":
                sid = (form.get("security_id") or [""])[0].strip()
                if not sid:
                    flash, err = "no security id", True
                else:
                    flash = exit_position(Client(), sid)
                    err = flash.startswith("no open")
            else:
                flash, err = "unknown action", True
        except Exception as exc:                        # noqa: BLE001
            flash, err = f"failed: {exc}", True
        from urllib.parse import urlencode
        tab = (form.get("tab") or ["pos"])[0]
        if tab not in ("chat", "pos", "orders", "decisions", "controls", "term"):
            tab = "pos"
        q = urlencode({"tab": tab, "day": self._day(form), "flash": flash[:300], "err": "1" if err else "0"})
        self.send_response(303)
        self.send_header("Location", "/?" + q)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
