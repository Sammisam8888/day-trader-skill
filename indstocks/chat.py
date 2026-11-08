"""
Chat control for the autotrader.

Two layers:

  COMMANDS  a small, deterministic grammar that needs no model and no
            network: stop, continue, faster/slower, buy, sell, cancel,
            status, why, limits. Every action goes through the same code the
            timers use (halt file, env file, flatten.sell, cancel) so the
            chat cannot do anything the buttons cannot.

  MODEL     when ANTHROPIC_API_KEY is set, free-form messages are sent to
            Claude with today's state (positions, orders, decisions) and a
            tool list that maps 1:1 onto the commands above. The model can
            explain and it can act, but only through those commands, with
            the same validation. No key: free-form messages get a plain
            "I can do these things" reply.

Standard library only. The model call is a urllib POST.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                           # noqa: E402
import session as sess                                  # noqa: E402
from shoonya import ShoonyaError                        # noqa: E402

# Indirections so tests can stub them without patching the real modules.
_sleep = time.sleep


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, timeout=20)

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
ENV_FILE = Path(os.environ.get("TRADER_ENV_FILE", "/etc/indstocks.env"))
HALT_FILE = Path(os.environ.get("TRADER_KILL_SWITCH", "/etc/indstocks.halt"))
TIMER_DROPIN = Path(os.environ.get("TRADER_TIMER_DROPIN",
                                   "/etc/systemd/system/auto.timer.d/override.conf"))
MODEL = os.environ.get("TRADER_CHAT_MODEL", "claude-sonnet-4-5")
MAX_BUY_VALUE_PCT = Decimal("50")     # one chat buy may use at most half of cash


def chat_log(role: str, text: str, extra: dict | None = None) -> None:
    rec = {"ts": sess.now().isoformat(timespec="seconds"), "role": role,
           "text": text, **(extra or {})}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "chat.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def history(n: int = 40) -> list:
    try:
        lines = (STATE_DIR / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ------------------------------------------------------------------ env

def read_env() -> dict:
    out = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def write_env(updates: dict) -> None:
    try:
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    kept = [ln for ln in lines if not any(ln.startswith(k + "=") for k in updates)]
    kept += [f"{k}={v}" for k, v in updates.items()]
    tmp = ENV_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    os.replace(tmp, ENV_FILE)


# ------------------------------------------------------------------ actions
# Each returns a human sentence. Each is also a "tool" for the model.

def act_stop(reason: str = "stopped from chat") -> str:
    HALT_FILE.write_text(reason + "\n", encoding="utf-8")
    return ("Stopped. No new orders will be placed. Resting orders at the exchange "
            "are still live -- say 'cancel all' to pull them too.")


def act_continue() -> str:
    try:
        HALT_FILE.unlink()
    except FileNotFoundError:
        return "Already running."
    return "Running again. The next scan may place orders."


def act_frequency(minutes: int) -> str:
    """Scan every N minutes. Rewrites the timer drop-in and reloads it."""
    if not 2 <= minutes <= 60:
        return "Scan interval must be between 2 and 60 minutes."
    spec = f"*-*-* 09..15:00/{minutes}:00 Asia/Kolkata"
    TIMER_DROPIN.parent.mkdir(parents=True, exist_ok=True)
    TIMER_DROPIN.write_text(f"[Timer]\nOnCalendar=\nOnCalendar={spec}\n", encoding="utf-8")
    try:
        _systemctl("daemon-reload")
        _systemctl("restart", "auto.timer")
    except Exception as exc:                            # noqa: BLE001
        return f"Wrote the schedule but could not reload the timer: {exc}"
    write_env({"TRADER_SCAN_MINUTES": str(minutes)})
    return (f"Scanning every {minutes} minutes through the entry window. "
            f"Each scan costs nothing; only fills cost brokerage.")


def act_limits(**kv) -> str:
    keys = {"max_loss": "TRADER_MAX_LOSS", "daily_loss": "TRADER_DAILY_LOSS",
            "max_trades": "TRADER_MAX_TRADES", "max_concurrent": "TRADER_MAX_CONCURRENT",
            "max_leverage": "TRADER_MAX_LEVERAGE", "max_resting": "TRADER_MAX_RESTING",
            "manual_target_pct": "TRADER_MANUAL_TARGET_PCT",
            "manual_stop_pct": "TRADER_MANUAL_STOP_PCT",
            "reward_to_cost": "TRADER_MIN_REWARD_TO_COST", "resting_ttl_min": "TRADER_RESTING_TTL_MIN"}
    updates = {}
    for k, v in kv.items():
        if k not in keys or v is None:
            continue
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            return f"{k}: not a number"
        if d < 0:
            return f"{k}: must not be negative"
        if k == "max_leverage" and d > 5:
            return "leverage above 5x refused"
        if k == "reward_to_cost" and d < 1:
            return "reward-to-cost below 1 refused: that takes trades that lose when they win"
        if k in ("max_trades", "max_concurrent", "max_resting"):
            d = int(d)
        updates[keys[k]] = str(d)
    if not updates:
        return "Nothing to change."
    write_env(updates)
    return "Set " + ", ".join(f"{k}={v}" for k, v in updates.items()) + ". Applies from the next scan."


def act_cancel_all(broker) -> str:
    """Cancel every pending BUY entry. Stops protecting open positions stay."""
    n, failed = 0, []
    for o in broker.orders():
        if o["pending"] and o["side"] == "B":
            try:
                broker.cancel(o["id"])
                n += 1
            except ShoonyaError as exc:
                failed.append(f"{o['symbol']}: {exc}")
    common.log("chat", {"event": "cancel_all", "count": n, "failed": failed})
    return (f"Cancelled {n} pending entr{'y' if n == 1 else 'ies'}. Stops on open positions are kept."
            + (f" Failed: {'; '.join(failed)}" if failed else ""))


def _match(positions: list, symbol: str):
    want = symbol.strip().upper()
    for p in positions:
        if want in (p["symbol"].upper(), p["tsym"].upper(), p["token"]):
            return p
    return next((p for p in positions if p["symbol"].upper().startswith(want)), None)


def act_sell(broker, symbol: str, qty: int | None = None) -> str:
    """Exit an intraday long: cancel its pending sells (the stop), then a
    marketable limit for the quantity."""
    p = _match([x for x in broker.positions() if x["qty"] > 0], symbol)
    if not p:
        return f"No open intraday position matching {symbol}."
    n = min(qty or p["qty"], p["qty"])
    for o in broker.orders():
        if o["pending"] and o["side"] == "S" and o["token"] == p["token"]:
            try:
                broker.cancel(o["id"])
            except ShoonyaError:
                pass
    _sleep(1)
    inst = broker.instrument_for(p)
    px = broker.marketable(inst, "S")
    oid = broker.place(inst, side="S", qty=n, price=px, remarks=f"chat/{p['symbol']}")
    common.log("chat", {"event": "chat_sell", "symbol": p["symbol"], "qty": n,
                        "price": str(px), "order_id": oid})
    left = p["qty"] - n
    return (f"Selling {p['symbol']} x{n} at limit {px} (order {oid})."
            + (f" {left} left; the fill monitor will re-protect them." if left else ""))


def act_buy(broker, symbol: str, qty: int) -> str:
    """Marketable-limit intraday buy, capped at half of cash. The fill
    monitor then guards it with the manual stop/target %."""
    if qty <= 0:
        return "Quantity must be positive."
    try:
        inst = broker.instrument(symbol.upper())
    except KeyError:
        return f"Unknown symbol {symbol}."
    q = broker.quote(inst.token, inst.exch)
    cash = broker.cash()
    value = q["ltp"] * qty
    cap = cash * MAX_BUY_VALUE_PCT / 100
    if value > cap:
        return (f"{qty} x {q['ltp']} = {value:.0f} is over the chat buy cap of {cap:.0f} "
                f"(half of cash {cash:.0f}). Try up to {int(cap / q['ltp'])} units.")
    px = broker.marketable(inst, "B", q)
    oid = broker.place(inst, side="B", qty=qty, price=px, remarks=f"chat/{inst.symbol}")
    common.log("chat", {"event": "chat_buy", "symbol": inst.symbol, "qty": qty,
                        "price": str(px), "order_id": oid})
    return (f"Buying {inst.symbol} x{qty} at limit {px} (order {oid}). The fill monitor will sell "
            f"it at +{common.setting('TRADER_MANUAL_TARGET_PCT', '1.0')}% or "
            f"-{common.setting('TRADER_MANUAL_STOP_PCT', '0.5')}%, and the flatten closes it.")


def act_status(broker) -> str:
    env_ = read_env()
    halted = HALT_FILE.exists()
    try:
        cash = broker.cash()
        pos = broker.positions()
        orders = broker.orders()
    except ShoonyaError as exc:
        return f"Broker unreadable: {exc}"
    r = sum((p["realised"] for p in pos), Decimal(0))
    u = sum((p["unrealised"] for p in pos), Decimal(0))
    open_ = [p for p in pos if p["qty"] != 0]
    resting = [o for o in orders if o["pending"] and o["side"] == "B"]
    stops = [o for o in orders if o["pending"] and o["side"] == "S"]
    return "\n".join([
        "HALTED" if halted else ("LIVE" if env_.get("TRADER_LIVE", "").lower() == "true" else "SHADOW"),
        f"cash {cash:.0f}",
        f"P&L today {r + u:+.2f} (realised {r:+.2f}, open {u:+.2f}) before charges",
        f"{len(open_)} open: " + (", ".join(f"{p['symbol']} x{p['qty']} @ {p['avg']}" for p in open_) or "none"),
        f"{len(resting)} resting entr{'y' if len(resting) == 1 else 'ies'}: "
        + (", ".join(f"{o['symbol']} trg {o['trigger']}" for o in resting) or "none"),
        f"{len(stops)} stop(s) at the exchange: " + (", ".join(f"{o['symbol']} {o['trigger']}" for o in stops) or "none"),
        f"symbols {env_.get('TRADER_SYMBOLS', common.DEFAULT_SYMBOLS)}; max "
        f"{env_.get('TRADER_MAX_CONCURRENT', '2')} positions, {env_.get('TRADER_MAX_LEVERAGE', '1.0')}x, "
        f"daily loss {env_.get('TRADER_DAILY_LOSS', '1500')}",
    ])


def act_why(symbol: str | None = None) -> str:
    day = sess.now().strftime("%Y-%m-%d")
    try:
        lines = (STATE_DIR / f"{day}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "No decisions logged today yet."
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        s = str(r.get("symbol") or r.get("name") or "")
        if symbol and symbol.upper() not in s.upper():
            continue
        ev = r.get("event")
        ts = (r.get("ts") or "")[11:16]
        if ev == "blocked":
            out.append(f"{ts} {s}: blocked -- " + "; ".join(r.get("reasons") or []))
        elif ev == "placed":
            out.append(f"{ts} {s}: placed {r.get('qty')} @ trigger {r.get('entry')}, stop {r.get('stop')}, "
                       f"target {r.get('target')}, net if target {r.get('net_if_target')}")
        elif ev in ("unsized", "skip", "no_data", "order_failed", "halted", "entry_cancelled",
                    "sold", "stop_placed", "stop_moved", "target_exit", "stop_failed", "login_required"):
            out.append(f"{ts} {s}: {ev} -- {r.get('reason') or ''}")
    if not out:
        return f"Nothing logged for {symbol} today." if symbol else "No decisions yet today."
    return "\n".join(out[-25:])


# ------------------------------------------------------------------ grammar

NUM = r"(\d+(?:\.\d+)?)"


def handle_command(text: str, client) -> str | None:
    """Deterministic layer. Returns None when the text is not a command."""
    t = text.strip().lower()
    if re.fullmatch(r"(stop|halt|pause|stop trading|stop everything)\b.*", t):
        return act_stop(f"chat: {text.strip()}"[:120])
    m = re.search(r"every\s+(\d+)\s*(min|minute|minutes|m)\b", t)
    if m and re.search(r"scan|check|frequen|run|trade|faster|slower|interval", t):
        return act_frequency(int(m.group(1)))
    if re.search(r"\b(faster|higher frequency|more frequent|more often)\b", t):
        return act_frequency(5)
    if re.search(r"\b(slower|lower frequency|less often)\b", t):
        return act_frequency(15)
    if re.fullmatch(r"(continue|resume|start|go|run|start trading|continue trading)\b.*", t):
        return act_continue()
    if re.fullmatch(r"(cancel|cancel all|cancel all orders|cancel everything|pull all orders)\b.*", t):
        return act_cancel_all(client)
    if re.fullmatch(r"(sell|exit|close|flatten)\s*(all|everything)?", t):
        msgs = [act_sell(client, p["symbol"]) for p in client.positions() if p["qty"] > 0]
        return "\n".join(msgs) or "Nothing open to sell."
    m = re.fullmatch(r"(?:sell|exit|close)\s+(?:(\d+)\s+)?([a-z&\-\. ]+?)(?:\s+(\d+))?", t)
    if m:
        qty = m.group(1) or m.group(3)
        return act_sell(client, m.group(2).strip(), int(qty) if qty else None)
    m = re.fullmatch(r"buy\s+(\d+)\s+([a-z0-9&\-\.]+)|buy\s+([a-z0-9&\-\.]+)\s+(\d+)", t)
    if m:
        qty = m.group(1) or m.group(4)
        sym = m.group(2) or m.group(3)
        return act_buy(client, sym, int(qty))
    if re.fullmatch(r"(status|state|how are we|what'?s happening|summary|report)\b.*", t):
        return act_status(client)
    m = re.fullmatch(r"why(?:\s+(?:did|was|is|not|no)\s+)?\s*([a-z0-9&\-\.]+)?.*", t)
    if m and t.startswith("why"):
        sym = m.group(1)
        if sym in ("did", "was", "is", "not", "no", "nothing", "it", "this", "that", None):
            sym = None
        return act_why(sym)
    m = re.search(r"(max loss|daily loss|max trades|max concurrent|max positions|leverage|max resting|target pct|stop pct|reward to cost|cost gate|ttl)\s*(?:to|=|at)?\s*" + NUM, t)
    if m:
        key = {"max loss": "max_loss", "daily loss": "daily_loss", "max trades": "max_trades",
               "max concurrent": "max_concurrent", "max positions": "max_concurrent",
               "leverage": "max_leverage", "max resting": "max_resting",
               "target pct": "manual_target_pct", "stop pct": "manual_stop_pct",
               "reward to cost": "reward_to_cost", "cost gate": "reward_to_cost", "ttl": "resting_ttl_min"}[m.group(1)]
        return act_limits(**{key: m.group(2)})
    m = re.fullmatch(r"(?:trade|symbols?|watch)\s+([a-z0-9, ]+)", t)
    if m and not t.startswith("trade every"):
        syms = ",".join(s.strip().upper() for s in re.split(r"[ ,]+", m.group(1)) if s.strip())
        write_env({"TRADER_SYMBOLS": syms})
        return f"Trading list is now {syms}. Applies from the next scan."
    if re.fullmatch(r"(help|\?|what can you do|commands)\b.*", t):
        return HELP
    return None


HELP = """I can do these, with or without an AI key:
- stop / continue
- scan every N minutes  (or: faster / slower)
- buy 50 NIFTYBEES  /  sell NIFTYBEES  /  sell all  /  cancel all
- symbols NIFTYBEES SENSEXIETF
- status  /  why  /  why HDFCBANK
- set max loss 500, daily loss 1500, max positions 3, leverage 1.5, target pct 1.5, stop pct 0.7
With an ANTHROPIC_API_KEY saved in Controls, you can just talk to me."""


# ------------------------------------------------------------------ model

TOOLS = [
    {"name": "stop", "description": "Halt: stop placing new orders.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}}},
    {"name": "resume", "description": "Resume placing orders.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "set_scan_interval", "description": "Scan every N minutes (2-60).",
     "input_schema": {"type": "object", "properties": {"minutes": {"type": "integer"}}, "required": ["minutes"]}},
    {"name": "set_limits", "description": "Change risk limits. Only pass the ones to change.",
     "input_schema": {"type": "object", "properties": {
         "max_loss": {"type": "number"}, "daily_loss": {"type": "number"},
         "max_trades": {"type": "integer"}, "max_concurrent": {"type": "integer"},
         "max_leverage": {"type": "number"}, "max_resting": {"type": "integer"},
         "manual_target_pct": {"type": "number"}, "manual_stop_pct": {"type": "number"}}}},
    {"name": "cancel_all", "description": "Cancel every resting order.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "sell", "description": "Market-sell an open intraday position.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["symbol"]}},
    {"name": "buy", "description": "Market-buy a stock intraday. Capped at half of cash.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["symbol", "qty"]}},
]


def run_tool(name: str, args: dict, client) -> str:
    if name == "stop":
        return act_stop(args.get("reason") or "stopped from chat")
    if name == "resume":
        return act_continue()
    if name == "set_scan_interval":
        return act_frequency(int(args.get("minutes", 10)))
    if name == "set_limits":
        return act_limits(**args)
    if name == "cancel_all":
        return act_cancel_all(client)
    if name == "sell":
        return act_sell(client, args["symbol"], args.get("qty"))
    if name == "buy":
        return act_buy(client, args["symbol"], int(args["qty"]))
    return f"unknown tool {name}"


def _api(key: str, body: dict) -> dict:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def handle_model(text: str, client, key: str) -> str:
    context = act_status(client) + "\n\nToday's decisions:\n" + act_why(None)
    env = read_env()
    system = (
        "You are the control desk for an automated NSE intraday trading bot on the owner's own "
        "Shoonya (Finvasia) account, trading NSE ETFs intraday. Be brief and concrete; rupees, not vibes. Use the tools when the owner "
        "asks for an action; otherwise answer from the state below. Never invent orders or prices. "
        "Brokerage is Rs 5 or 0.03% per executed order; a round trip on Rs 12k costs about Rs 15. "
        f"Scan interval currently {env.get('TRADER_SCAN_MINUTES', '10')} minutes.\n\nSTATE:\n" + context)
    msgs = [{"role": h["role"] if h["role"] in ("user", "assistant") else "user",
             "content": h["text"]} for h in history(12) if h.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": text})
    # collapse consecutive same-role turns, which the API rejects
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    if merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(context)"})

    body = {"model": MODEL, "max_tokens": 600, "system": system, "tools": TOOLS, "messages": merged}
    out_lines = []
    for _ in range(4):
        resp = _api(key, body)
        content = resp.get("content") or []
        texts = [c["text"] for c in content if c.get("type") == "text"]
        tool_uses = [c for c in content if c.get("type") == "tool_use"]
        out_lines += texts
        if not tool_uses:
            break
        results = []
        for tu in tool_uses:
            r = run_tool(tu["name"], tu.get("input") or {}, client)
            out_lines.append(f"[{tu['name']}] {r}")
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": r})
        body["messages"] = body["messages"] + [{"role": "assistant", "content": content},
                                               {"role": "user", "content": results}]
    return "\n".join(out_lines) or "(no reply)"


# ------------------------------------------------------------------ claude cli
# The server already has a logged-in Claude Code session (it is how the MCP
# server was registered). `claude -p` runs a one-shot prompt on that login,
# so no API key is needed. It runs as the login's user, not root.

CLI_USER = os.environ.get("TRADER_CHAT_USER", "ubuntu")
CLI_TIMEOUT = int(os.environ.get("TRADER_CHAT_TIMEOUT", "120"))


def find_claude() -> str | None:
    home = Path(f"/home/{CLI_USER}")
    for cand in (os.environ.get("TRADER_CLAUDE_BIN"),
                 home / ".local/bin/claude", home / ".npm-global/bin/claude",
                 home / ".claude/local/claude", "/usr/local/bin/claude", "/usr/bin/claude"):
        if cand and Path(cand).exists():
            return str(cand)
    try:
        r = subprocess.run(["sudo", "-u", CLI_USER, "-H", "bash", "-lc", "command -v claude"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:                                   # noqa: BLE001
        return None


CLI_SYSTEM = """You are the control desk for an automated NSE intraday trading bot on the
owner's own Shoonya (Finvasia) account, trading NSE ETFs intraday. Be brief and concrete; rupees,
not vibes. Never invent orders, prices or fills: everything you know is in STATE below. Brokerage
is Rs 5 or 0.03% per executed order; a round trip on Rs 12k costs about Rs 15.

Reply with ONE JSON object and nothing else:
{"reply": "<what to say to the owner, plain text>", "actions": [ {"name": ..., "args": {...}} ]}
"actions" may be empty. Available actions and args:
  stop {reason}            resume {}            set_scan_interval {minutes: 2-60}
  set_limits {max_loss, daily_loss, max_trades, max_concurrent, max_leverage, max_resting,
              manual_target_pct, manual_stop_pct}   (only the ones to change)
  cancel_all {}            sell {symbol, qty?}  buy {symbol, qty}   (buy is capped at half of cash)
Only act when the owner clearly asks for an action. Questions get actions: [].
"""


def _run_cli(prompt: str) -> str:
    exe = find_claude()
    if not exe:
        raise RuntimeError("claude CLI not found on this box")
    cmd = ["sudo", "-u", CLI_USER, "-H", exe, "-p", prompt, "--output-format", "json",
           "--tools", ""]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT,
                       cwd=f"/home/{CLI_USER}")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    try:
        return json.loads(r.stdout).get("result") or ""
    except ValueError:
        return r.stdout


def _parse_json_reply(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("{"):]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return {"reply": raw, "actions": []}
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        return {"reply": raw, "actions": []}


def handle_cli(text: str, client) -> str:
    state = act_status(client) + "\n\nToday's decisions:\n" + act_why(None)
    recent = "\n".join(f"{h['role']}: {h['text'][:300]}" for h in history(10)
                        if h.get("role") in ("user", "assistant"))
    prompt = (CLI_SYSTEM + "\nSTATE:\n" + state + "\n\nRECENT CHAT:\n" + recent
              + "\n\nOWNER: " + text)
    got = _parse_json_reply(_run_cli(prompt))
    lines = [str(got.get("reply") or "").strip()]
    for a in got.get("actions") or []:
        try:
            lines.append(f"[{a.get('name')}] " + run_tool(a.get("name"), a.get("args") or {}, client))
        except Exception as exc:                        # noqa: BLE001
            lines.append(f"[{a.get('name')}] failed: {str(exc)[:200]}")
    return "\n".join(ln for ln in lines if ln) or "(no reply)"


def handle(text: str, client) -> str:
    chat_log("user", text)
    try:
        reply = handle_command(text, client)
        if reply is None:
            key = read_env().get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if key:
                reply = handle_model(text, client, key)
            elif find_claude():
                reply = handle_cli(text, client)
            else:
                reply = ("I didn't recognise that as a command, and there is no Claude login "
                         "or API key on this box.\n\n" + HELP)
    except Exception as exc:                            # noqa: BLE001
        reply = f"Failed: {str(exc)[:300]}"
    chat_log("assistant", reply)
    return reply
