"""
Dashboard for the Shoonya autotrader. Standard library only. Binds to
localhost and the Tailscale address; never to all interfaces.

Tabs: Chat, Positions (net P&L after charges, live and closed positions),
Orders (each entry with its exchange stop and the reason it was placed),
Decisions (every logged decision), Controls (halt/resume/limits/symbols),
Terminal (run a command on the box).

Login: Shoonya is OAuth-only. "Log in" sends the browser to Shoonya's
authorize page; the redirect comes back to /shoonya/callback (or the
redirected URL is pasted into the login card) and the code is exchanged
for today's session token on THIS box, from its whitelisted static IP.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import metrics
import session as sess
import shoonya as sh

WEB_DIST = Path(os.path.dirname(os.path.abspath(__file__))) / "web" / "dist"

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
ENV_FILE = Path(os.environ.get("TRADER_ENV_FILE", "/etc/indstocks.env"))
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
            if (line.startswith(("TRADER_", "ANTHROPIC_API_KEY", "SHOONYA_CLIENT_ID", "SHOONYA_USER_ID", "SHOONYA_ALGO_ID"))) and "=" in line:
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


def live(broker) -> dict:
    """Everything the page shows from the broker, each part failing alone."""
    out = {"cash": None, "positions": [], "orders": [], "error": "", "login": False}
    try:
        out["cash"] = broker.cash()
    except sh.LoginRequired as exc:
        out["login"], out["error"] = True, str(exc)
        return out
    except Exception as exc:                            # noqa: BLE001
        out["error"] = f"funds: {exc}"[:200]
    for key, fn in (("positions", broker.positions), ("orders", broker.orders)):
        try:
            out[key] = fn()
        except sh.LoginRequired as exc:
            out["login"], out["error"] = True, str(exc)
            return out
        except Exception as exc:                        # noqa: BLE001
            out["error"] = (out["error"] + f" {key}: {exc}")[:300]
    return out


def placed_records(log: list) -> dict:
    """entry order id -> the autotrader's placement record (the why)."""
    return {str(r.get("order_id")): r for r in log
            if r.get("event") == "placed" and r.get("order_id")}


def stop_for(log: list) -> dict:
    """entry order id -> latest stop order id."""
    out = {}
    for r in log:
        if r.get("event") == "stop_placed" and r.get("parent"):
            out[str(r["parent"])] = str(r.get("order_id"))
    return out


def status_label(o: dict) -> tuple:
    """(css class, label) in the broker app's words."""
    if o["dead"]:
        return "dead", o["status"].title()
    if o["status"] == "COMPLETE":
        return "fill", "Executed"
    if o["filled"] > 0:
        return "fill", f"Partly filled {o['filled']}"
    if o["status"] == "TRIGGER_PENDING":
        return "wait", "Waiting for Trigger Price"
    return "wait", "Open"


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


def cancel_pair(broker, order_id: str) -> str:
    """Cancel an entry and, if it is the bot's, its stop. Ids must be in
    today's book."""
    book = {o["id"]: o for o in broker.orders()}
    if order_id not in book:
        return f"order {order_id} is not in today's order book"
    targets = [order_id]
    sid = stop_for(common.read_log()).get(order_id)
    if sid:
        targets.append(sid)
    done, failed = [], []
    for oid in targets:
        o = book.get(oid)
        if not o or not o["pending"]:
            continue
        try:
            broker.cancel(oid)
            done.append(oid)
        except Exception as exc:                        # noqa: BLE001
            failed.append(f"{oid}: {str(exc)[:80]}")
    common.log("dash", {"event": "cancelled_from_dashboard", "symbol": book[order_id]["symbol"],
                        "order_ids": done, "failed": failed})
    if failed:
        return "cancelled " + ", ".join(done) + "; FAILED " + "; ".join(failed)
    return f"Cancelled {book[order_id]['symbol']} ({len(done)} order{'s' if len(done) != 1 else ''})."


def exit_position(broker, token: str) -> str:
    import chat as chat_mod
    pos = [p for p in broker.positions() if p["token"] == token and p["qty"] > 0]
    if not pos:
        return f"no open intraday position with token {token}"
    return chat_mod.act_sell(broker, pos[0]["symbol"])


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
.pill.placed,.pill.sold,.pill.ok,.pill.cancelled_on_fill,.pill.stop_moved{background:#1b3a26;color:var(--ok)}
.pill.blocked,.pill.ALERT,.pill.rejected_locally,.pill.halted,.pill.sell_failed,.pill.cancel_failed,.pill.error{background:#3d1d1d;color:var(--bad)}
.pill.would_place,.pill.run_start,.pill.run_end,.pill.reconciled_out,.pill.state,.pill.sizing{background:#1d2d44;color:var(--acc)}
.pill.unsized,.pill.skip,.pill.no_data,.pill.stop,.pill.invalidated,.pill.expired{background:#2d2a1a;color:var(--warn)}
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
    ("TRADER_SYMBOLS", "symbols to trade (comma separated)", "text"),
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
    ("TRADER_BREAKEVEN_R", "move stop to entry after +N R", "number"),
    ("TRADER_TRAIL_R", "then trail the stop N R below price", "number"),
    ("TRADER_MANUAL_TARGET_PCT", "manual position: sell when up %", "number"),
    ("TRADER_MANUAL_STOP_PCT", "manual position: sell when down %", "number"),
)


def render(day: str, flash: str = "", flash_err: bool = False) -> str:
    import charges as charges_mod
    log = read_log(day)
    lim = limits()
    broker = sh.Shoonya()
    data = live(broker)
    now = sess.now()
    session = sess.session_for(now)
    phase = session.phase(now) if session else "not a trading day"
    halted = HALT_FILE.exists()
    live_flag = lim.get("TRADER_LIVE", "false").lower() in ("1", "true", "yes")
    whys = placed_records(log)
    stops = stop_for(log)
    tok = sh.load_token(now)
    cash = data["cash"] or Decimal(0)

    allpos = data["positions"]
    pos = [r for r in allpos if r["qty"] != 0]
    closed = [r for r in allpos if r["qty"] == 0 and (r["buy_qty"] or r["sell_qty"])]
    orders = data["orders"]
    pending = [o for o in orders if o["pending"]]
    done_orders = [o for o in orders if not o["pending"]]
    px = {}
    if tok and not data["login"]:
        px = broker.ltps([r["token"] for r in pos] + [o["token"] for o in pending])

    state = ("<span class=bad>HALTED</span>" if halted else
             ("<span class=ok>LIVE</span>" if live_flag else "<span class=warn>SHADOW</span>"))
    p = [f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>trader</title><style>{CSS}</style>",
         f"<div class=top><div class=head><h1>autotrader <span class=muted style='font-size:12px;font-weight:500'>Shoonya</span></h1>"
         f"<div class=muted style='font-size:13px'>{e(now.strftime('%a %d %b %H:%M'))}</div></div>"
         f"<div class=tabs><a data-tab=chat href='/classic?tab=chat&day={e(day)}'>Chat</a>"
         f"<a data-tab=pos href='/classic?tab=pos&day={e(day)}'>Positions</a>"
         f"<a data-tab=orders href='/classic?tab=orders&day={e(day)}'>Orders</a>"
         f"<a data-tab=decisions href='/classic?tab=decisions&day={e(day)}'>Decisions</a>"
         f"<a data-tab=controls href='/classic?tab=controls&day={e(day)}'>Controls</a>"
         f"<a data-tab=term href='/classic?tab=term&day={e(day)}'>Terminal</a></div>"
         f"<div class=ticker><b>{e(phase)}</b> &middot; {state} &middot; cash <b>{money(cash)}</b>"
         f" &middot; {e(lim.get('TRADER_SYMBOLS', common.DEFAULT_SYMBOLS))} &middot; "
         f"max {e(lim.get('TRADER_MAX_CONCURRENT', '2'))} pos &middot; "
         f"{e(lim.get('TRADER_MAX_LEVERAGE', '1.0'))}x</div></div><div class=wrap>"]
    if flash:
        p.append(f"<div class='flash{' err' if flash_err else ''}'>{e(flash)}</div>")

    # ---- login card: always first when there is no session ----
    if not tok or data["login"]:
        configured = bool(lim.get("SHOONYA_CLIENT_ID"))
        p.append("<div class=card style='border-color:var(--warn)'><div class=nm>Not logged in to Shoonya</div>"
                 "<div class=meta style='margin:6px 0 10px'>Nothing can trade or be protected until you log in. "
                 "Shoonya sessions end every night, so this is once per trading day.</div>")
        if configured:
            p.append("<a class='btn btn-save' href='/shoonya/login'>Log in to Shoonya</a>"
                     "<form method=post action=/shoonya/code style='margin-top:10px'>"
                     "<input type=hidden name=tab value=pos>"
                     "<label>If the redirect page does not load, paste its full address (or just the code) here</label>"
                     "<input type=text name=code placeholder='https://...?code=...'>"
                     "<button>Use this code</button></form>")
        else:
            p.append("<div class=bad>Shoonya credentials are not set. In Terminal run: "
                     "<code>sudo bash scripts/setup-shoonya.sh</code></div>")
        p.append("</div>")
    else:
        p.append(f"<div class=meta style='margin-bottom:8px'>Shoonya session since {e(tok['at'][11:16])} "
                 f"&middot; {e(tok.get('uid'))}</div>")
    if data["error"] and not data["login"]:
        p.append(f"<div class='card bad'>{e(data['error'])}</div>")

    # ---- chat ----
    import chat as chat_mod
    has_key = bool(lim.get("ANTHROPIC_API_KEY"))
    p.append("<section data-pane=chat><div class=chatbox id=chatbox>")
    msgs = chat_mod.history(60)
    if not msgs:
        p.append("<div class='bub bot'>Hi. Tell me <b>stop</b>, <b>continue</b>, <b>scan every 5 minutes</b>, "
                 "<b>buy 50 NIFTYBEES</b>, <b>sell all</b>, <b>cancel all</b>, <b>status</b> or <b>why NIFTYBEES</b>."
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
             "<input type=text name=text placeholder='stop / continue / buy 50 NIFTYBEES / why / status' required>"
             "<button class=btn-save style='width:auto;margin:0'>Send</button></form>"
             f"<div class=meta style='margin-top:8px'>{'AI replies via API key' if has_key else ('AI replies via the server Claude login' if chat_mod.find_claude() else 'Command mode only')}"
             f" &middot; scan every {e(lim.get('TRADER_SCAN_MINUTES', '5'))} min</div></section>")

    # ---- positions ----
    p.append("<section data-pane=pos>")
    rp = sum((r["realised"] for r in allpos), Decimal(0))
    up = Decimal(0)
    fees = Decimal(0)
    for r in allpos:
        ltp = px.get(r["token"]) or r["ltp"] or r["avg"]
        if r["qty"]:
            up += (ltp - r["avg"]) * r["qty"]
            fees += charges_mod.round_trip(r["avg"] * abs(r["qty"]), ltp * abs(r["qty"])).total
        q = min(r["buy_qty"], r["sell_qty"])
        if q:
            fees += charges_mod.round_trip(r["buy_avg"] * q, r["sell_avg"] * q).total
    net = rp + up - fees

    def c(v): return "ok" if v >= 0 else "bad"
    p.append(f"<div class=card><div class=pnl>Today's net, after charges"
             f"<div class='v {c(net)}'>{'+' if net >= 0 else ''}&#8377;{money(net)}</div></div>"
             f"<div class=g3 style='margin-top:10px'><div><div class=k>Realised (closed)</div><div class='v {c(rp)}'>{'+' if rp >= 0 else ''}&#8377;{money(rp)}</div></div>"
             f"<div><div class=k>Open (at LTP)</div><div class='v {c(up)}'>{'+' if up >= 0 else ''}&#8377;{money(up)}</div></div>"
             f"<div><div class=k>Charges (est.)</div><div class='v bad'>-&#8377;{money(fees)}</div></div></div></div>")

    bot_tokens = {w["token"] for oid, w in whys.items()
                  if any(o["id"] == oid and o["filled"] > 0 for o in orders)}
    held_value = Decimal(0)
    p.append(f"<h2>Live positions ({len(pos)})</h2>")
    if not pos:
        p.append("<div class='card muted'>No open intraday positions.</div>")
    for r in pos:
        qty, avg = r["qty"], r["avg"]
        ltp = px.get(r["token"]) or r["ltp"]
        inv, cur = avg * qty, ltp * qty
        held_value += abs(cur)
        pnl = cur - inv
        pct = (pnl / inv * 100) if inv else Decimal(0)
        cls = c(pnl)
        ours = r["token"] in bot_tokens
        stop_o = next((o for o in pending if o["token"] == r["token"] and o["side"] == "S"), None)
        plan = next((w for w in whys.values() if w.get("token") == r["token"]), {})
        p.append(f"<div class=card><div class=head><div style='display:flex;gap:10px'>"
                 f"<div class=logo>{e(r['symbol'][:2])}</div><div><div class=nm>{e(r['symbol'])}</div>"
                 f"<div class=meta>&#8377;{money(ltp)} &middot; MIS &middot; "
                 f"{'bot' if ours else '<span class=warn>manual</span>'}</div></div></div>"
                 f"<div style='text-align:right'><div class=meta>Avg. <b>&#8377;{money(avg)}</b></div>"
                 f"<div class=meta>Qty <b>{'+' if qty > 0 else ''}{qty}</b> <span class='badge {'B' if qty > 0 else 'S'}'>{'B' if qty > 0 else 'S'}</span></div></div></div>"
                 f"<div class=g3><div><div class=k>Invested</div><div class=v>&#8377;{money(inv)}</div></div>"
                 f"<div><div class=k>Current</div><div class=v>&#8377;{money(cur)}</div></div>"
                 f"<div><div class=k>P&amp;L (<span class={cls}>{pct:+.2f}%</span>)</div><div class='v {cls}'>{'+' if pnl >= 0 else ''}&#8377;{money(pnl)}</div></div></div>")
        if stop_o:
            p.append(f"<div class=why>Stop at the exchange: <b class=bad>&#8377;{money(stop_o['trigger'])}</b>"
                     + (f" &middot; target <b class=ok>&#8377;{money(plan.get('target'))}</b> (taken by the fill monitor)" if plan else "")
                     + "</div>")
        elif ours:
            p.append("<div class='why bad'>No stop at the exchange yet. The fill monitor places one within seconds of a fill; "
                     "if this stays, check the Decisions tab for stop_failed.</div>")
        else:
            p.append(f"<div class=why>Manual position. The fill monitor sells it when it is up "
                     f"<b>{e(lim.get('TRADER_MANUAL_TARGET_PCT', '1.0'))}%</b> or down "
                     f"<b>{e(lim.get('TRADER_MANUAL_STOP_PCT', '0.5'))}%</b> from {money(avg)}; "
                     f"the flatten closes it regardless.</div>")
        p.append(f"<div class=btns><form method=post action=/exit onsubmit=\"return confirm('Sell {e(r['symbol'])} x{qty} now?')\">"
                 f"<input type=hidden name=token value='{e(r['token'])}'><input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=pos>"
                 f"<button class=btn-x>&#10162; Insta Exit</button></form>"
                 f"<a class=btn data-tab=orders href='/classic?tab=orders&day={e(day)}'>Orders</a></div></div>")
    if pos and cash:
        lev = held_value / cash
        p.append(f"<div class=meta style='margin:-4px 0 8px'>Held &#8377;{money(held_value)} against cash "
                 f"&#8377;{money(cash)} = <b class='{'bad' if lev > Decimal(lim.get('TRADER_MAX_LEVERAGE', '1.0') or 1) else 'ok'}'>{lev:.2f}x</b></div>")
    if closed:
        p.append(f"<h2>Closed today ({len(closed)})</h2>")
        for r in closed:
            q = min(r["buy_qty"], r["sell_qty"])
            fee = charges_mod.round_trip(r["buy_avg"] * q, r["sell_avg"] * q).total if q else Decimal(0)
            rpnl = r["realised"]
            plan = next((w for w in whys.values() if w.get("token") == r["token"]), {})
            how = ""
            if plan:
                stop, tgt = Decimal(plan["stop"]), Decimal(plan["target"])
                how = ("stop-loss hit" if r["sell_avg"] <= stop * Decimal("1.002") else
                       "target hit" if r["sell_avg"] >= tgt * Decimal("0.998") else "closed before stop/target")
            p.append(f"<div class=card><div class=head><div style='display:flex;gap:10px'>"
                     f"<div class=logo>{e(r['symbol'][:2])}</div><div><div class=nm>{e(r['symbol'])}</div>"
                     f"<div class=meta>Qty {q} &middot; <span class='st {'dead' if rpnl < 0 else 'fill'}'>{e(how) or 'traded'}</span></div></div></div>"
                     f"<div style='text-align:right'><div class='v {c(rpnl)}' style='font-size:18px;font-weight:700'>{'+' if rpnl >= 0 else ''}&#8377;{money(rpnl)}</div>"
                     f"<div class=meta>net {'+' if rpnl - fee >= 0 else ''}&#8377;{money(rpnl - fee)}</div></div></div>"
                     f"<div class=g3><div><div class=k>Avg. Buy</div><div class=v>&#8377;{money(r['buy_avg'])}</div></div>"
                     f"<div><div class=k>Avg. Sell</div><div class=v>&#8377;{money(r['sell_avg'])}</div></div>"
                     f"<div><div class=k>Charges</div><div class=v>&#8377;{money(fee)}</div></div></div>"
                     + (f"<div class=why><b>Plan was:</b> {e(plan.get('why', ''))}</div>" if plan else "")
                     + "</div>")

    # ---- orders ----
    p.append("</section><section data-pane=orders>")
    entries = [o for o in pending if o["side"] == "B"]
    sells = [o for o in pending if o["side"] == "S"]
    p.append(f"<h2>Open orders ({len(pending)})</h2>")
    if not pending:
        p.append("<div class='card muted'>No open orders.</div>")
    for o in entries + sells:
        ltp = px.get(o["token"])
        cls, label = status_label(o)
        why = whys.get(o["id"], {})
        is_stop = o["side"] == "S" and o["type"] == "SL-LMT"
        kind = ("Stop-loss" if is_stop else "Sell") if o["side"] == "S" else ("Breakout entry" if why else "Buy")
        p.append(f"<div class=card><div class=head><div style='display:flex;gap:10px'>"
                 f"<div class=logo>{e(o['symbol'][:2])}</div><div><div class=tk>{e(kind)}</div>"
                 f"<div class=nm>{e(o['symbol'])}</div><div class=meta><span class='badge {o['side']}'>{o['side']}</span> &nbsp;"
                 f"{e(o['type'])} &middot; LTP <b>&#8377;{money(ltp) if ltp is not None else '?'}</b></div></div></div>"
                 f"<span class='st {cls}'>{e(label)}</span></div>"
                 f"<div class=g3><div><div class=k>Trigger</div><div class=v>{'&#8377;' + money(o['trigger']) if o['trigger'] else '-'}</div></div>"
                 f"<div><div class=k>Limit</div><div class=v>&#8377;{money(o['price'])}</div></div>"
                 f"<div><div class=k>Filled</div><div class=v>{o['filled']}/{o['qty']}</div></div></div>")
        if why:
            p.append(f"<div class=why><b>Why:</b> {e(why.get('why', ''))}"
                     f"<div style='margin-top:8px'>"
                     f"<div class=row2><span>Stop / target</span><b><span class=bad>&#8377;{money(why.get('stop'))}</span> / <span class=ok>&#8377;{money(why.get('target'))}</span></b></div>"
                     f"<div class=row2><span>Position value</span><b>&#8377;{money(why.get('position_value'))}</b></div>"
                     f"<div class=row2><span>Leverage (this order / cash)</span><b>{e(why.get('leverage'))}x</b></div>"
                     f"<div class=row2><span>Max loss at stop</span><b class=bad>-&#8377;{money(why.get('risk'))}</b></div>"
                     f"<div class=row2><span>Profit if target hits (gross)</span><b class=ok>+&#8377;{money(why.get('gross_if_target'))}</b></div>"
                     f"<div class=row2><span>Charges, round trip</span><b>-&#8377;{money(why.get('costs'))}</b></div>"
                     f"<div class=row2><span>Net if target hits</span><b class='{c(Decimal(str(why.get('net_if_target') or 0)))}'>&#8377;{money(why.get('net_if_target'))}</b></div>"
                     f"</div></div>")
        confirm = ("Cancel this STOP? The position will have no protection." if is_stop
                   else f"Cancel {o['symbol']} {kind.lower()}?")
        p.append(f"<div class=btns><form method=post action=/cancel onsubmit=\"return confirm('{e(confirm)}')\">"
                 f"<input type=hidden name=order_id value='{e(o['id'])}'><input type=hidden name=day value='{e(day)}'><input type=hidden name=tab value=orders>"
                 f"<button class=btn-x>&#10006; Cancel</button></form></div></div>")
    if done_orders:
        p.append(f"<details><summary>{len(done_orders)} executed / cancelled / rejected today</summary><div class=card>")
        for o in done_orders:
            p.append(f"<div class=row><div class=l><div class=sym>{e(o['symbol'])} <span class='badge {o['side']}'>{o['side']}</span></div>"
                     f"<div class=sub>{e(o['type'])} {o['qty']} &middot; filled {o['filled']} @ {money(o['avg'])} &middot; {e(o['remarks'])}"
                     f"{(' &middot; ' + e(o['reason'])) if o['reason'] else ''}</div></div>"
                     f"<div class=r><span class='pill {'ok' if o['status'] == 'COMPLETE' else 'blocked'}'>{e(o['status'])}</span></div></div>")
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
    p.append("<details><summary>Risk limits &amp; credentials</summary><form method=post action=/limits><input type=hidden name=tab value=controls>")
    for key, label, typ in EDITABLE:
        p.append(f"<label>{e(label)}</label><input type={typ} {'step=any ' if typ == 'number' else ''}name={e(key)} "
                 f"value='{e(lim.get(key, ''))}' placeholder='default'>")
    p.append("<h2 style='margin-top:16px'>Shoonya credentials</h2>"
             "<div class=sub>From trade.shoonya.com &rarr; profile &rarr; API Key. Blank keeps what is saved. "
             "Register IP 13.127.164.72 there as primary and secondary.</div>")
    for key, label, kind in (("SHOONYA_USER_ID", "User ID", "text"),
                             ("SHOONYA_CLIENT_ID", "Client ID", "text"),
                             ("SHOONYA_SECRET_CODE", "Secret Code", "password"),
                             ("SHOONYA_ALGO_ID", "SEBI Algo ID (if issued)", "text")):
        saved = bool(cred_saved(key))
        shown = "" if kind == "password" else e(lim.get(key, ""))
        p.append(f"<label>{label}{' (saved)' if saved and kind == 'password' else ''}</label>"
                 f"<input type={kind} name={key} value='{shown}' autocomplete=off>")
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
             f"<div class=nav><a data-tab=chat href='/classic?tab=chat&day={e(day)}'><span>&#128172;</span>Chat</a>"
             f"<a data-tab=pos href='/classic?tab=pos&day={e(day)}'><span>&#8377;</span>Positions</a>"
             f"<a data-tab=orders href='/classic?tab=orders&day={e(day)}'><span>&#9776;</span>Orders</a>"
             f"<a data-tab=decisions href='/classic?tab=decisions&day={e(day)}'><span>&#9881;</span>Decisions</a>"
             f"<a data-tab=controls href='/classic?tab=controls&day={e(day)}'><span>&#9211;</span>Controls</a>"
             f"<a data-tab=term href='/classic?tab=term&day={e(day)}'><span>&gt;_</span>Terminal</a></div>"
             f"<script>{TABS_JS}</script>")
    return "".join(p)


def cred_saved(key: str) -> str:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
    except OSError:
        pass
    return ""


CRED_KEYS = ("SHOONYA_USER_ID", "SHOONYA_CLIENT_ID", "SHOONYA_SECRET_CODE", "SHOONYA_ALGO_ID")


def write_limits(form: dict) -> str:
    """Rewrite the TRADER_* lines in the env file. Validates before touching it."""
    new = {}
    for key, label, typ in EDITABLE:
        raw = (form.get(key) or [""])[0].strip()
        if not raw:
            continue
        if typ == "text":
            import re as _re
            syms = [s.upper() for s in _re.split(r"[ ,]+", raw) if s]
            if not syms or not all(_re.fullmatch(r"[A-Z0-9&\-]{1,20}", s) for s in syms):
                return f"{label}: letters, digits, & and - only"
            new[key] = ",".join(syms)
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
    import re as _re2
    for ck in CRED_KEYS:
        raw = (form.get(ck) or [""])[0].strip()
        if not raw:
            continue
        if not _re2.fullmatch(r"[A-Za-z0-9_\-]{2,128}", raw):
            return f"{ck}: unexpected characters"
        new[ck] = raw
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



# ------------------------------------------------------------------ JSON API
# The React app (indstocks/web) renders from these. State comes from the
# fill monitor's snapshot whenever it is fresh, so opening the page costs
# the broker nothing; otherwise one direct read, cached for 15 seconds.

import threading
import time as _time

_cache: dict = {}
_lock = threading.Lock()


def _j(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, dict):
        return {k: _j(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_j(v) for v in x]
    return x


def _broker_view() -> dict:
    snap = metrics.read_snapshot()
    if snap:
        return {"source": "monitor", "age": round(_time.time() - snap["ts"], 1), **snap, "login": False, "error": ""}
    with _lock:
        hit = _cache.get("view")
        if hit and _time.time() - hit["at"] < 15:
            return hit["data"]
        b = sh.Shoonya()
        data = live(b)
        ltps = {}
        if not data["login"]:
            toks = [p["token"] for p in data["positions"] if p["qty"]] + \
                   [o["token"] for o in data["orders"] if o["pending"]]
            for sym in common.symbols():
                try:
                    toks.append(b.instrument(sym).token)
                except Exception:                       # noqa: BLE001
                    pass
            try:
                ltps = b.ltps(list(dict.fromkeys(toks)))
            except Exception:                           # noqa: BLE001
                ltps = {}
        view = {"source": "direct", "age": 0, "positions": data["positions"], "orders": data["orders"],
                "ltps": ltps, "cash": data["cash"], "login": data["login"], "error": data["error"]}
        _cache["view"] = {"at": _time.time(), "data": view}
        return view


def _hm(dt) -> str:
    return dt.strftime("%H:%M")


AI_PREFIXES = ("auto/", "stop/", "tgt/", "man/", "flat/", "chat/", "cover/")


def api_state() -> dict:
    now = sess.now()
    day = now.strftime("%Y-%m-%d")
    session = sess.session_for(now)
    lim = limits()
    log = read_log(day)
    tok = sh.load_token(now)
    halted = HALT_FILE.exists()
    halt_reason = ""
    if halted:
        try:
            halt_reason = HALT_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    v = _broker_view() if tok else {"positions": [], "orders": [], "ltps": {}, "cash": None,
                                    "login": True, "error": "", "source": "none", "age": None}
    positions, orders, ltps = v["positions"], v["orders"], v["ltps"]
    whys = placed_records(log)
    ours = common.our_order_ids(day)
    pending_sell = {o["token"]: o for o in orders if o["pending"] and o["side"] == "S"}
    bot_tokens = {w.get("token") for oid, w in whys.items()
                  if any(o["id"] == oid and o["filled"] > 0 for o in orders)}

    pos_out = []
    for p in positions:
        if not p["qty"]:
            continue
        ltp = ltps.get(p["token"]) or p["ltp"] or p["avg"]
        plan = next((w for w in whys.values() if w.get("token") == p["token"]), {})
        so = pending_sell.get(p["token"])
        inv = p["avg"] * p["qty"]
        pnl_ = (ltp - p["avg"]) * p["qty"]
        pos_out.append({"symbol": p["symbol"], "token": p["token"], "qty": p["qty"], "avg": p["avg"],
                        "ltp": ltp, "invested": inv, "current": ltp * p["qty"], "pnl": pnl_,
                        "pct": (pnl_ / inv * 100) if inv else 0,
                        "source": "ai" if p["token"] in bot_tokens else "manual",
                        "stop": so["trigger"] if so else (plan.get("stop") and Decimal(plan["stop"])),
                        "stop_live": bool(so), "target": plan.get("target") and Decimal(plan["target"]),
                        "entry": plan.get("entry") and Decimal(plan["entry"]), "why": plan.get("why", "")})

    ord_out = []
    for o in orders:
        ai = o["id"] in ours or o["remarks"].startswith(AI_PREFIXES)
        why = whys.get(o["id"], {})
        r = o["remarks"]
        kind = ("entry" if r.startswith("auto/") or why else "stop" if r.startswith("stop/") or (o["side"] == "S" and o["type"] == "SL-LMT")
                else "target" if r.startswith("tgt/") else "flatten" if r.startswith("flat/")
                else "chat" if r.startswith("chat/") else "exit" if o["side"] == "S" else "buy")
        cls, label = status_label(o)
        ord_out.append({**{k: o[k] for k in ("id", "symbol", "token", "side", "type", "qty", "filled", "avg",
                                             "price", "trigger", "status", "pending", "time", "reason")},
                        "label": label, "tone": cls, "kind": kind, "source": "ai" if ai else "manual",
                        "ltp": ltps.get(o["token"]),
                        "plan": {k: why.get(k) for k in ("why", "stop", "target", "risk", "gross_if_target",
                                                         "costs", "net_if_target", "position_value", "leverage")}
                        if why else None})
    ord_out.sort(key=lambda x: (not x["pending"], x["time"] or ""), reverse=False)

    summ = metrics.pnl(positions, ltps)
    trades = metrics.closed_trades(positions)
    wins = sum(1 for x in trades if x["net"] > 0)
    hist = [h for h in metrics.history(30) if h["day"] != day]
    hist.append({"day": day, "net": float(summ["net"]), "trades": len(trades), "wins": wins})
    all_trades = sum(int(h.get("trades") or 0) for h in hist)
    all_wins = sum(int(h.get("wins") or 0) for h in hist)

    series = [{"t": r["ts"][11:16], "net": float(Decimal(r["net"])), "realised": float(Decimal(r["realised"])),
               "open": float(Decimal(r["open"]))} for r in log if r.get("event") == "pnl"]

    hide = {"pnl", "state", "fillmonitor_start"}
    decisions = [r for r in log if r.get("event") not in hide
                 and not (r.get("job") == "watch" and r.get("event") == "ok")][-120:]

    import chat as chat_mod
    status = ("halted" if halted else "login" if (not tok or v["login"]) else
              "live" if lim.get("TRADER_LIVE", "").lower() == "true" else "shadow")
    tickers = []
    for sym in common.symbols():
        c = sh._load_cache().get(f"NSE:{sym}")
        tickers.append({"symbol": sym, "token": c and c.get("token"),
                        "ltp": ltps.get(c.get("token")) if c else None})

    return _j({
        "now": now.isoformat(timespec="seconds"), "day": day,
        "session": ({"phase": session.phase(now), "open": _hm(session.open), "or_end": _hm(session.opening_range_end),
                     "last_entry": _hm(session.last_entry), "flat_by": _hm(session.flat_by),
                     "close": _hm(session.close), "special": bool(session.special)} if session else None),
        "status": status, "halt_reason": halt_reason,
        "login": {"ok": bool(tok) and not v["login"], "since": tok and tok["at"][11:16],
                  "uid": tok and tok.get("uid"), "configured": bool(lim.get("SHOONYA_CLIENT_ID"))},
        "data": {"source": v.get("source"), "age": v.get("age"), "error": v.get("error")},
        "cash": v.get("cash"), "symbols": tickers,
        "limits": {k.replace("TRADER_", "").lower(): val for k, val in lim.items() if k.startswith("TRADER_")},
        "pnl": summ, "series": series,
        "stats": {"today_trades": len(trades), "today_wins": wins,
                  "win_rate": (all_wins / all_trades * 100) if all_trades else None,
                  "trades": all_trades, "wins": all_wins, "history": hist},
        "positions": pos_out, "closed": trades, "orders": ord_out,
        "decisions": decisions[::-1], "chat": chat_mod.history(60),
        "chat_mode": "api" if lim.get("ANTHROPIC_API_KEY") else ("claude" if chat_mod.find_claude() else "commands"),
    })


def api_candles(symbol: str) -> dict:
    """5-minute candles for today (or the last session) with the plan's
    levels, for the price chart. Cached 60 s per symbol."""
    symbol = symbol.upper()
    key = f"candles:{symbol}"
    with _lock:
        hit = _cache.get(key)
        if hit and _time.time() - hit["at"] < 60:
            return hit["data"]
    now = sess.now()
    if not sh.load_token(now):
        return {"symbol": symbol, "candles": [], "levels": {}, "error": "not logged in"}
    from datetime import timedelta
    b = sh.Shoonya()
    inst = b.instrument(symbol)
    cs = b.candles(inst.token, now - timedelta(days=5), now)
    if cs:
        last_day = cs[-1].opens_at.date()
        cs = [c for c in cs if c.opens_at.date() == last_day]
    offset = 19800                                       # show IST on a UTC axis
    levels = {}
    plan = next((r for r in reversed(read_log(now.strftime("%Y-%m-%d")))
                 if r.get("event") in ("placed", "would_place") and r.get("symbol") == symbol), None)
    if plan:
        levels = {k: float(Decimal(plan[k])) for k in ("range_high", "range_low", "trigger", "stop", "target")
                  if plan.get(k)}
    elif cs:
        s = sess.session_for(cs[0].opens_at)
        from market import opening_range
        rng = opening_range(cs, s) if s else None
        if rng:
            levels = {"range_high": float(rng[0]), "range_low": float(rng[1])}
    data = {"symbol": symbol, "tick": float(inst.tick),
            "candles": [{"time": int(c.opens_at.timestamp()) + offset, "open": float(c.open), "high": float(c.high),
                         "low": float(c.low), "close": float(c.close), "volume": c.volume} for c in cs],
            "levels": levels, "error": ""}
    with _lock:
        _cache[key] = {"at": _time.time(), "data": data}
    return data


def api_action(body: dict) -> dict:
    a = str(body.get("action") or "")
    if a == "halt":
        HALT_FILE.write_text((str(body.get("reason") or "halted from dashboard"))[:200] + "\n", encoding="utf-8")
        return {"ok": True, "message": "Halted. No new entries. Stops, targets and the flatten keep running."}
    if a == "resume":
        try:
            HALT_FILE.unlink()
        except FileNotFoundError:
            pass
        return {"ok": True, "message": "Resumed. The next scan may place orders."}
    if a == "login_code":
        rec = sh.exchange_code(str(body.get("code") or ""))
        common.log("dash", {"event": "logged_in", "uid": rec["uid"]})
        return {"ok": True, "message": f"Logged in to Shoonya as {rec['uid']}."}
    if a == "chat":
        import chat as chat_mod
        text = str(body.get("text") or "").strip()[:500]
        if not text:
            return {"ok": False, "message": "empty message"}
        return {"ok": True, "message": chat_mod.handle(text, sh.Shoonya())}
    if a == "cancel":
        msg = cancel_pair(sh.Shoonya(), str(body.get("order_id") or ""))
        return {"ok": not ("FAILED" in msg or "not in" in msg), "message": msg}
    if a == "exit":
        msg = exit_position(sh.Shoonya(), str(body.get("token") or ""))
        return {"ok": not msg.lower().startswith("no open"), "message": msg}
    return {"ok": False, "message": f"unknown action {a!r}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> bool:
        if not WEB_DIST.is_dir():
            return False
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (WEB_DIST / rel).resolve()
        if not str(f).startswith(str(WEB_DIST.resolve())) or not f.is_file():
            if rel.startswith("assets/"):
                return False
            f = WEB_DIST / "index.html"
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                 ".woff2": "font/woff2", ".json": "application/json"}.get(f.suffix, "application/octet-stream")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if f.suffix == ".html" else "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)
        return True

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

    def _redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):                                   # noqa: N802
        from urllib.parse import urlencode, urlparse
        path = urlparse(self.path).path
        q = self._query()
        if path == "/shoonya/login":
            try:
                return self._redirect(sh.authorize_url())
            except sh.ShoonyaError as exc:
                return self._redirect("/?" + urlencode({"flash": str(exc), "err": "1"}))
        if path == "/shoonya/callback":
            code = (q.get("code") or [""])[0]
            try:
                rec = sh.exchange_code(code)
                common.log("dash", {"event": "logged_in", "uid": rec["uid"]})
                msg, err = f"Logged in to Shoonya as {rec['uid']}.", "0"
            except Exception as exc:                    # noqa: BLE001
                msg, err = f"login failed: {exc}", "1"
            return self._redirect("/?" + urlencode({"flash": msg[:300], "err": err}))
        if path == "/api/state":
            try:
                return self._json(api_state())
            except Exception as exc:                    # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if path == "/api/candles":
            try:
                return self._json(api_candles((q.get("symbol") or ["NIFTYBEES"])[0]))
            except Exception as exc:                    # noqa: BLE001
                return self._json({"candles": [], "levels": {}, "error": str(exc)[:200]})
        if path != "/classic" and self._static(path):
            return
        flash = (q.get("flash") or [""])[0]
        err = (q.get("err") or ["0"])[0] == "1"
        try:
            self._send(render(self._day(), flash, err).encode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            self._send(f"<pre>dashboard error: {e(exc)}</pre>".encode(), 500)

    def do_POST(self):                                  # noqa: N802
        """Halt, resume, limits, chat, terminal, cancel, exit, login code."""
        from urllib.parse import parse_qs, urlparse
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if path == "/api/action":
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
                return self._json(api_action(body))
            except Exception as exc:                    # noqa: BLE001
                return self._json({"ok": False, "message": f"{type(exc).__name__}: {exc}"[:300]})
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
                    chat_mod.handle(text, sh.Shoonya())
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
                    flash = cancel_pair(sh.Shoonya(), oid)
                    err = "FAILED" in flash or "not in" in flash
            elif path == "/exit":
                token = (form.get("token") or [""])[0].strip()
                if not token:
                    flash, err = "no instrument token", True
                else:
                    flash = exit_position(sh.Shoonya(), token)
                    err = flash.startswith("no open") or flash.startswith("No open")
            elif path == "/shoonya/code":
                code = (form.get("code") or [""])[0].strip()
                if not code:
                    flash, err = "paste the redirected address or the code", True
                else:
                    rec = sh.exchange_code(code)
                    flash = f"Logged in to Shoonya as {rec['uid']}."
                    common.log("dash", {"event": "logged_in", "uid": rec["uid"]})
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
        self.send_header("Location", "/classic?" + q)
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
