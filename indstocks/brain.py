"""
The decision layer. Claude decides; the code guards.

Every scan, Claude (the logged-in Claude Code CLI on this box, so no API key)
receives a compact picture of the market and the account and answers with
decisions in JSON:

    enter   symbol, side BUY/SELL, order stop|limit|now, entry, stop, target,
            size (0.25-1.0 of a slot), confidence, reason
    cancel  a resting entry it placed earlier
    exit    an open position, now

It decides WHAT and WHERE. It does not decide HOW MUCH beyond `size`, and it
cannot override a limit: autotrader.py sizes every entry from your max loss
per trade, your leverage cap and the free cash, and refuses anything that
breaks a rule (risk.py). Stops, targets, trailing and the flatten stay with
the fill monitor, because they must act in seconds even if Claude is slow.

If Claude cannot be reached (CLI missing, timeout, unparseable answer), the
opening-range rule decides instead, both sides, and the log says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import regime                                            # noqa: E402
import session as sess                                   # noqa: E402
import strategy as strat                                 # noqa: E402
from market import Candle, opening_range, to_tick        # noqa: E402

CLI_USER = os.environ.get("TRADER_CHAT_USER", "ubuntu")
TIMEOUT = int(os.environ.get("TRADER_BRAIN_TIMEOUT", "120"))


# ------------------------------------------------------------------ context

def _bars(candles: List[Candle], minutes: int) -> List[Candle]:
    """Merge 5-minute candles into `minutes`-minute bars."""
    out: List[Candle] = []
    for c in candles:
        slot = c.opens_at.replace(minute=(c.opens_at.minute // minutes) * minutes, second=0)
        if out and out[-1].opens_at == slot:
            b = out[-1]
            out[-1] = Candle(b.opens_at, b.open, max(b.high, c.high), min(b.low, c.low), c.close,
                             b.volume + c.volume, minutes)
        else:
            out.append(Candle(slot, c.open, c.high, c.low, c.close, c.volume, minutes))
    return out


def symbol_view(sym: str, inst, candles: List[Candle], quote: dict, session) -> dict:
    today = session.day
    tc = [c for c in candles if c.opens_at.date() == today]
    prev = [c for c in candles if c.opens_at.date() < today]
    pday = prev[-1].opens_at.date() if prev else None
    pc = [c for c in prev if c.opens_at.date() == pday]
    rng = opening_range(tc, session)
    vol = sum(c.volume for c in tc)
    vwap = (sum((c.high + c.low + c.close) / 3 * c.volume for c in tc) / vol) if vol else None
    v = {"symbol": sym, "tick": str(inst.tick), "ltp": str(quote["ltp"]),
         "band": [str(quote["lc"]), str(quote["uc"])],
         "bid_ask": [str(quote["bid"]), str(quote["ask"])],
         "prev_day": ({"o": str(pc[0].open), "h": str(max(c.high for c in pc)), "l": str(min(c.low for c in pc)),
                       "c": str(pc[-1].close)} if pc else None),
         "today": ({"o": str(tc[0].open), "h": str(max(c.high for c in tc)), "l": str(min(c.low for c in tc)),
                    "vwap": str(round(vwap, 2)) if vwap else None, "volume": vol} if tc else None),
         "opening_range": ({"high": str(rng[0]), "low": str(rng[1])} if rng else None),
         "bars_15m": [f"{b.opens_at:%H:%M} {b.open} {b.high} {b.low} {b.close} {b.volume}" for b in _bars(tc, 15)],
         "last_5m": [f"{c.opens_at:%H:%M} {c.open} {c.high} {c.low} {c.close} {c.volume}" for c in tc[-6:]]}
    return v


def position_view(p: dict, orders: list, ours: dict, adj: dict, limits) -> dict:
    stop = next((o["trigger"] for o in orders if o["pending"] and o["token"] == p["token"]
                 and o["side"] == ("S" if p["qty"] > 0 else "B")), None)
    parent = next((oid for oid, r in ours.items() if str(r.get("token")) == str(p["token"])
                   and any(o["id"] == oid and o["filled"] > 0 for o in orders)), None)
    plan = ours.get(parent, {}) if parent else {}
    target = (adj.get(parent) or {}).get("target") or plan.get("target")
    qty = abs(p["qty"])
    risk_now = (abs(p["avg"] - stop) * qty) if stop is not None else None
    return {"symbol": p["symbol"], "side": "LONG" if p["qty"] > 0 else "SHORT", "qty": qty,
            "avg": str(p["avg"]), "ltp": str(p["ltp"]), "unrealised": str(p["unrealised"]),
            "stop": str(stop) if stop is not None else None, "target": target,
            "loss_if_stop_now": str(risk_now) if risk_now is not None else None,
            "opened_by": "you" if not parent else "this system",
            "plan_reason": plan.get("why")}


def build(broker, session, now, *, cash, positions, orders, placements, limits) -> dict:
    views, insts = [], {}
    for sym in common.symbols():
        try:
            inst = broker.instrument(sym)
            candles = broker.candles(inst.token, now - timedelta(days=4), now)
            q = broker.quote(inst.token, inst.exch)
        except Exception as exc:                        # noqa: BLE001
            views.append({"symbol": sym, "error": str(exc)[:120]})
            continue
        insts[sym] = (inst, candles, q)
        views.append(symbol_view(sym, inst, candles, q, session))
    trends = {s: regime.stock_trend(c, session.day, _bars([x for x in c if x.opens_at.date() == session.day], 15))
              for s, (_, c, _) in insts.items()}
    for v in views:
        if v.get("symbol") in trends:
            v["trend"] = trends[v["symbol"]]
    direction = {"market": regime.market(trends, common.setting("TRADER_INDEX", "NIFTYBEES").upper()),
                 "stocks": trends}
    held = [p for p in positions if p["qty"] != 0]
    exposure = sum((abs(p["avg"] * p["qty"]) for p in held), Decimal(0))
    realised = sum((p["realised"] for p in positions), Decimal(0))
    unrealised = sum((p["unrealised"] for p in positions), Decimal(0))
    ours = {str(p.get("order_id")): p for p in placements}
    adj = {}
    for r in common.events("plan_adjusted"):
        if r.get("target"):                             # a stop-only adjust keeps the last target
            adj[str(r.get("parent"))] = r
    fills = {}
    for o in orders:
        if o["id"] in ours and o["filled"] > 0:
            fills[ours[o["id"]]["symbol"]] = fills.get(ours[o["id"]]["symbol"], 0) + 1
    ctx = {
        "now": now.strftime("%H:%M"), "phase": session.phase(now),
        "session": {"open": f"{session.open:%H:%M}", "last_entry": f"{session.last_entry:%H:%M}",
                    "flat_by": f"{session.flat_by:%H:%M}", "special": bool(session.special)},
        "account": {"cash": str(cash), "max_leverage": str(limits.max_leverage),
                    "buying_power": str(cash * limits.max_leverage),
                    "exposure": str(exposure), "realised": str(realised), "unrealised": str(unrealised),
                    "daily_loss_limit": str(limits.daily_loss_limit),
                    "loss_room_left": str(limits.daily_loss_limit + min(realised + unrealised, Decimal(0))),
                    "max_loss_per_trade": str(limits.max_loss_per_trade),
                    "max_concurrent": limits.max_concurrent_positions,
                    "fills_today": sum(fills.values()), "max_trades": limits.max_trades_per_day,
                    "extra_trades": _extra_view(positions, orders, placements, limits),
                    "max_entries_per_symbol": int(common.setting("TRADER_MAX_ENTRIES_PER_SYMBOL", "2")),
                    "shorts_allowed": allow_short()},
        "direction": {"market": direction["market"],
                      "stocks": {s: {"score": x["score"], "state": x["state"]} for s, x in trends.items()}},
        "sentiment": sentiment_view(now),
        "control": control_view(),
        "positions": [position_view(p, orders, ours, adj, limits) for p in held],
        "resting_entries": [{"order_id": o["id"], "symbol": ours[o["id"]]["symbol"],
                             "side": "BUY" if o["side"] == "B" else "SELL", "type": o["type"],
                             "trigger": str(o["trigger"]), "limit": str(o["price"]), "qty": o["qty"],
                             "stop": ours[o["id"]].get("stop"), "target": ours[o["id"]].get("target")}
                            for o in orders if o["pending"] and o["id"] in ours and o["filled"] == 0],
        "entries_today": fills,
        "recent_decisions": [{k: r.get(k) for k in ("ts", "event", "symbol", "reason", "reasons")}
                             for r in common.read_log()[-200:]
                             if r.get("event") in ("placed", "blocked", "entry_cancelled", "target_exit",
                                                   "stop_moved", "sold", "brain_view")][-14:],
        "markets": views,
    }
    return {"ctx": ctx, "insts": insts}


# ------------------------------------------------------------------ claude

def allow_short() -> bool:
    return common.setting("TRADER_ALLOW_SHORT", "true").lower() in ("1", "true", "yes")


SYSTEM = """You are the trader for a small automated NSE intraday account (Shoonya, MIS product).
YOU make every trading decision: what to enter, which side, where, the stop, the target, how long
an entry may rest, when to move a stop or target, and when to exit. Nothing else decides for you:
no rule-based fallback trades, and nothing moves your stops on its own. If you do not act, nothing
happens.

The code only executes and guards. It sizes each order from the owner's risk limits and your stop,
refuses an order that breaks those limits (below), places YOUR stop at the exchange the moment an
entry fills, exits at YOUR target, cancels an entry when it outlives your valid_minutes or when
price has already crossed its stop before filling, and closes everything at flat_by (MIS rule).

INPUTS (measured by code, for you to weigh):
- STATE.direction: the market and each stock, scored -1..+1 from VWAP, today's open, yesterday's
  close, 9/21 EMA on 5-minute closes and 15-minute swing structure (index proxy double weight,
  plus breadth). UP / DOWN / MIXED / FORMING. Trading with it is usually right: on a DOWN day the
  money is on the short side (SELL first, buy back lower), short the weakest stocks on bounces into
  VWAP or breaks of support; on an UP day buy the strongest on pullbacks or breaks; on MIXED trade
  each stock with its own trend. Going against it needs a specific reason you can state.
- STATE.sentiment (may be null): India VIX, Nifty option-chain put/call OI ratio with the call
  wall (resistance) and put wall (support), FII/DII flows, today's events, a news read with
  per-stock items. Weaker than price. Avoid a stock on its results day or against material news
  on it; avoid entries just before a timed macro event; size down against a clear bias or when VIX
  is high. Use the walls as levels: targets short of the opposite wall; be wary of shorting into
  the put wall or buying into the call wall.
Each placed trade records what these inputs said, and a scorecard measures whether following
them paid. Your reasoning for every decision is shown to the owner.

Your job each scan: look at every market and every open position, then decide. {style}
Prefer clean structure (opening-range breaks/breakdowns, VWAP
reclaims/rejections, prior-day high/low breaks, trend pullbacks) with a stop at a level that
invalidates the idea. Avoid stops inside normal noise. Respect charges: brokerage is Rs 5 or 0.03%
per order; a target under ~0.25% of price rarely pays. Never chase a candle that already ran.
Give every entry valid_minutes (5-180): how long the idea stays good if it does not fill.

Risk appetite (the owner's, enforced by code): max loss per trade, daily loss limit and the loss
room left today are in STATE.account. Size is computed from max loss per trade and your stop, so a
tighter stop means a bigger position and a wider stop a smaller one. size (0.25-1) scales it down.

Open positions are yours to manage every scan, with "adjust" (new stop and/or target) or "exit".
Nothing trails for you: move the stop to breakeven once a trade has earned it, trail it behind
structure as it runs, extend or pull in the target as momentum changes, and exit when the idea is
broken. loss_if_stop_now shows what each current stop risks. To keep a plan as it is, send no
decision for it: an adjust that changes nothing is refused.

Hard limits the code enforces (anything else is refused, so don't waste a decision):
- side BUY: stop < entry < target. side SELL (short): target < entry < stop.
- reward:risk at least {min_rr}. No new entries after last_entry.
- order "stop": a trigger that fills only if price trades through it (BUY above ltp, SELL below).
  order "limit": rests at a better price (BUY below ltp, SELL above). order "now": fill immediately.
- at most {max_conc} positions at once, {max_entries} entries per symbol per day, leverage {lev}x.
- shorts allowed: {shorts}. Symbols: only those listed in markets.

CONTROL COMMANDS steer the system for today. Each needs a "trigger" that the code checks against
the data; if the data does not confirm it the command is refused. Use them only when it does:
{{"action":"control","command":"<name>","trigger":"<trigger>","args":{{...}},"reason":"..."}}
- halt_today {{reason}}: no new entries today. triggers: drawdown (day P&L <= -50% of the daily
  limit), stop_streak (3+ stops, no target hit), vix_spike (VIX +15% or >= 25), shock_news (news
  score <= -0.7), chop (after 11:00: 3+ UP/DOWN flips in an hour or an hour of flat MIXED),
  data_broken (half the markets failed to load), event_risk (within an hour of a timed event).
- flatten_now {{reason}}: close all and halt. triggers: drawdown (<= -75%), vix_spike (+25%),
  shock_news (<= -0.85).
- resume_today {{reason}}: lift your own halt. trigger: condition_cleared (30+ min later and the
  halt's trigger no longer holds).
- set_risk_today {{max_loss_per_trade, max_trades, max_concurrent, max_leverage, size, sides}}.
  Lowering: triggers drawdown, stop_streak, vix_spike, shock_news, chop, event_risk,
  sentiment_against (|bias| >= 0.5). Raising: trigger trend_day only (6 straight direction
  readings UP or all DOWN with |score| >= 0.6, realised P&L > 0, a target already hit), and only
  up to owner_ceilings, after 09:45, while today's P&L >= 0; raises revert if the day turns red.
  The daily loss limit can only be lowered. Raise and lower in separate commands.
- calendar_add {{kind: holiday|special_session|event, date, what, source, ...}}: holiday and
  special_session need an nseindia.com or bseindia.com source; special_session needs open,
  last_entry, flat_by, close (HH:MM); event takes at (HH:MM), symbol, results. calendar_remove
  {{kind, date}}. notify {{message}} needs no trigger.
STATE.control shows today's overrides, the owner's limits and ceilings, the calendar ahead and the
commands already issued. Most days need no command at all.

Reply with ONE JSON object, nothing else:
{{"view": "<one or two sentences on the market>",
  "decisions": [
    {{"action":"enter","symbol":"NIFTYBEES","side":"BUY","order":"stop","entry":281.05,"stop":280.30,
      "target":282.60,"size":1.0,"valid_minutes":45,"confidence":0.6,"reason":"<why, citing levels>"}},
    {{"action":"adjust","symbol":"NIFTYBEES","stop":281.10,"target":283.40,"reason":"..."}},
    {{"action":"cancel","order_id":"<resting entry id>","reason":"..."}},
    {{"action":"exit","symbol":"...","reason":"..."}}
  ]}}
"decisions" may be empty. size is 0.25 to 1.0 of a normal slot. In "adjust", give stop, target or both;
the new stop must stay on the losing side of the price and its loss must fit max_loss_per_trade."""


def find_claude() -> Optional[str]:
    import chat
    return chat.find_claude()


LEAN_SYSTEM = ("You are the decision engine of an automated NSE intraday trading system. Follow the "
               "instructions in the user message exactly and reply only in the format it asks for.")


def run_cli(prompt: str, timeout: int = TIMEOUT, tools: str = "") -> str:
    """One `claude -p` call: text in, text out. No tools unless `tools` names
    read-only ones (the ops review may search the web to check a calendar)."""
    exe = find_claude()
    if not exe:
        raise RuntimeError("claude CLI not found on this box")
    cmd = ["sudo", "-u", CLI_USER, "-H", exe, "-p", "--output-format", "json", "--tools", tools]
    if tools:
        cmd += ["--allowedTools", *tools.split(",")]
    model = common.setting("TRADER_BRAIN_MODEL")
    if model:
        cmd[6:6] = ["--model", model]
    full = list(cmd)
    if common.setting("TRADER_CLI_LEAN", "true").lower() in ("1", "true", "yes"):
        # Claude Code's own coding-agent context (~70k tokens: its system prompt,
        # skills, MCP servers, CLAUDE.md) is useless here and burns the plan's
        # usage on every scan. Keep the subscription login; drop the rest.
        cmd += ["--system-prompt", LEAN_SYSTEM, "--strict-mcp-config", "--disable-slash-commands",
                "--setting-sources", "user"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                       cwd=f"/home/{CLI_USER}")
    if r.returncode != 0 and cmd != full:
        common.log("brain", {"event": "lean_cli_failed", "reason": (r.stderr or r.stdout)[-200:]})
        r = subprocess.run(full, input=prompt, capture_output=True, text=True, timeout=timeout,
                           cwd=f"/home/{CLI_USER}")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-300:])
    try:
        u = json.loads(r.stdout).get("usage") or {}
        common.log("brain", {"event": "cli_usage", "input": u.get("input_tokens"),
                             "cache_write": u.get("cache_creation_input_tokens"),
                             "cache_read": u.get("cache_read_input_tokens"), "output": u.get("output_tokens")})
    except (ValueError, AttributeError):
        pass
    try:
        return json.loads(r.stdout).get("result") or ""
    except ValueError:
        return r.stdout


def ask_claude(ctx: dict, limits, run: Optional[Callable] = None, transcript: Optional[dict] = None) -> dict:
    """Ask Claude. `transcript` (if given) is filled with the exact prompt,
    the raw reply and the latency, even when the reply cannot be parsed."""
    import time as _t
    transcript = transcript if transcript is not None else {}
    style = ("STYLE: AGGRESSIVE (the owner's choice). Keep capital working: while slots are free and it is "
             "before last_entry, place every valid setup you can justify (resting stop triggers on good "
             "levels are fine), re-enter a stock after a stop when its structure resets, and rotate out of "
             "stalled positions into better ones. Idle slots cost money on trend days. Aim for real moves: "
             "targets of 2-3R at the next meaningful level, not the nearest tick; once a trade has earned "
             "it, trail the stop and extend the target so winners run. On a profitable day "
             "the trade limit grows (account.extra_trades): use it on a strong trend. Never take a trade "
             "without a real stop and a target that pays after charges."
             if common.setting("TRADER_STYLE", "normal").lower() == "aggressive"
             else "Most scans the right answer is no new entry.")
    prompt = SYSTEM.format(style=style, min_rr=limits.min_reward_to_risk, max_conc=limits.max_concurrent_positions,
                           max_entries=ctx["account"]["max_entries_per_symbol"], lev=limits.max_leverage,
                           shorts="yes" if allow_short() else "no")
    prompt += "\n\nSTATE:\n" + json.dumps(ctx, separators=(",", ":"))
    transcript["prompt"] = prompt
    t0 = _t.monotonic()
    raw = (run or run_cli)(prompt)
    transcript["raw"] = raw
    transcript["latency_s"] = round(_t.monotonic() - t0, 1)
    return parse(raw)


def parse_obj(raw: str) -> dict:
    """The first-to-last brace span of a reply, as a JSON object."""
    s = raw.strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        raise ValueError(f"no JSON in reply: {raw[:160]!r}")
    out = json.loads(s[a:b + 1])
    if not isinstance(out, dict):
        raise ValueError("reply is not a JSON object")
    return out


def _extra_view(positions, orders, placements, limits) -> dict:
    import profit_budget
    r = profit_budget.room(positions, orders, placements, limits.max_loss_per_trade)
    return {"rule": f"past max_trades, an entry is allowed if its worst case fits profit_room "
                    f"(keeps {100 - r['giveback_pct']}% of today's realised net profit)",
            "profit_room": str(r["room"]), "realised_net": str(r["realised_net"]),
            "hard_cap": profit_budget.hard_cap(limits.max_trades_per_day)}


def control_view() -> dict:
    import control
    v = control.state_view()
    return {"halt": v["halt"], "today_overrides": v["risk"], "raised": v["raised"],
            "owner_limits": v["base"], "owner_ceilings": v["ceilings"],
            "next_days": [d for d in v["upcoming"][:7]],
            "commands_today": [{k: h.get(k) for k in ("ts", "by", "command", "outcome", "message")}
                               for h in v["history"][-6:]]}


def sentiment_view(now) -> Optional[dict]:
    import sentiment
    snap = sentiment.load(now)
    if not snap:
        return None
    pos = snap.get("positioning") or {}
    return {"as_of": snap["ts"][11:16], "tilt": snap["tilt"],
            "news": (snap.get("news") or {}).get("market"),
            "stock_news": (snap.get("news") or {}).get("stocks"),
            "vix": pos.get("vix"), "nifty": pos.get("nifty"), "options": pos.get("options"),
            "flows": snap.get("flows"), "events": snap.get("events")}


def parse(raw: str) -> dict:
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        raise ValueError(f"no JSON in reply: {raw[:160]!r}")
    out = json.loads(s[a:b + 1])
    if not isinstance(out.get("decisions", []), list):
        raise ValueError("decisions is not a list")
    out.setdefault("decisions", [])
    out.setdefault("view", "")
    return out


# ------------------------------------------------------------------ rules

def rules(ctx: dict, insts: dict, session) -> dict:
    """Fallback decider: opening-range breakout AND breakdown, both resting."""
    decisions = []
    held = {p["symbol"] for p in ctx["positions"]}
    resting = {(r["symbol"], r["side"]) for r in ctx["resting_entries"]}
    for m in ctx["markets"]:
        sym = m["symbol"]
        if "error" in m or not m.get("opening_range") or sym in held:
            continue
        if ctx["entries_today"].get(sym, 0) >= ctx["account"]["max_entries_per_symbol"]:
            continue
        inst = insts[sym][0]
        hi, lo = Decimal(m["opening_range"]["high"]), Decimal(m["opening_range"]["low"])
        ltp = Decimal(m["ltp"])
        if not (lo <= ltp <= hi):
            continue
        for side in ("BUY", "SELL") if allow_short() else ("BUY",):
            if (sym, side) in resting or regime.check(side, sym, ctx.get("direction")):
                continue
            import sentiment
            if sentiment.check(side, sym, sentiment.load(), sess.now(),
                               ((ctx.get("direction") or {}).get("market") or {}).get("state"))[0]:
                continue
            try:
                p = strat.plan_trigger_exit(hi, lo, inst.tick) if side == "BUY" else strat.plan_breakdown(hi, lo, inst.tick)
            except strat.PlanError:
                continue
            trig = to_tick(hi + inst.tick, inst.tick, mode="up") if side == "BUY" else to_tick(lo - inst.tick, inst.tick, mode="down")
            decisions.append({"action": "enter", "symbol": sym, "side": side, "order": "stop",
                              "entry": float(trig), "stop": float(p.stop), "target": float(p.target),
                              "size": 1.0, "confidence": 0.5,
                              "reason": f"opening range {lo}-{hi}: {'break above' if side == 'BUY' else 'break below'} "
                                        f"{trig}, stop at the midpoint {p.stop}, target 2R {p.target}"})
    return {"view": "rule-based: opening-range break either way", "decisions": decisions}


def decide(broker, session, now, *, cash, positions, orders, placements, limits,
           run: Optional[Callable] = None) -> dict:
    b = build(broker, session, now, cash=cash, positions=positions, orders=orders,
              placements=placements, limits=limits)
    mode = common.setting("TRADER_DECIDER", "claude").lower()
    tr: dict = {}
    if mode == "claude":
        try:
            out = ask_claude(b["ctx"], limits, run=run, transcript=tr)
            out["source"] = "claude"
            return {**out, "insts": b["insts"], "ctx": b["ctx"], "transcript": tr}
        except Exception as exc:                        # noqa: BLE001
            tr["error"] = f"{type(exc).__name__}: {exc}"[:500]
            common.log("brain", {"event": "brain_unavailable", "reason": str(exc)[:300],
                                 "action": ("rule-based decisions this scan" if common.setting("TRADER_BRAIN_FALLBACK", "none").lower() == "rules" else "no trades this scan")})
            if common.setting("TRADER_BRAIN_FALLBACK", "none").lower() != "rules":
                return {"view": "", "decisions": [], "source": "none", "insts": b["insts"], "ctx": b["ctx"],
                        "transcript": tr}
    out = rules(b["ctx"], b["insts"], session)
    out["source"] = "rules"
    tr.setdefault("prompt", "(no model call: the opening-range rule decided)\n\nSTATE:\n"
                  + json.dumps(b["ctx"], separators=(",", ":")))
    tr["raw"] = json.dumps({"view": out["view"], "decisions": out["decisions"]})
    return {**out, "insts": b["insts"], "ctx": b["ctx"], "transcript": tr}


# ------------------------------------------------------------------ transcript

def transcript_path(day: Optional[str] = None) -> Path:
    return common.STATE_DIR / "brain" / f"{day or sess.now().strftime('%Y-%m-%d')}.jsonl"


def save_transcript(rec: dict) -> None:
    """One line per scan: what was asked, what came back, what was done."""
    path = transcript_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def read_transcripts(day: Optional[str] = None) -> List[dict]:
    out = []
    try:
        for line in transcript_path(day).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out
