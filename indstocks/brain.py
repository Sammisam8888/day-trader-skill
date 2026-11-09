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
    held = [p for p in positions if p["qty"] != 0]
    exposure = sum((abs(p["avg"] * p["qty"]) for p in held), Decimal(0))
    realised = sum((p["realised"] for p in positions), Decimal(0))
    unrealised = sum((p["unrealised"] for p in positions), Decimal(0))
    ours = {str(p.get("order_id")): p for p in placements}
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
                    "max_entries_per_symbol": int(common.setting("TRADER_MAX_ENTRIES_PER_SYMBOL", "2")),
                    "shorts_allowed": allow_short()},
        "positions": [{"symbol": p["symbol"], "side": "LONG" if p["qty"] > 0 else "SHORT",
                       "qty": abs(p["qty"]), "avg": str(p["avg"]), "ltp": str(p["ltp"]),
                       "unrealised": str(p["unrealised"]),
                       "stop": next((str(o["trigger"]) for o in orders if o["pending"] and o["token"] == p["token"]
                                     and o["side"] == ("S" if p["qty"] > 0 else "B")), None)} for p in held],
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
You decide entries and exits. Code you cannot override will size each order from the account's
risk limits, reject anything that breaks them, place a protective stop the moment an entry
fills, take the target, trail the stop (breakeven at +1R), and close everything at flat_by.

Your job each scan: look at every market, then decide. Most scans the right answer is no new
entry. Prefer clean structure (opening-range breaks/breakdowns, VWAP reclaims/rejections,
prior-day high/low breaks, trend pullbacks) with a stop at a level that invalidates the idea.
Avoid stops inside normal noise. Respect charges: brokerage is Rs 5 or 0.03% per order;
a target under ~0.25% of price rarely pays. Never chase a candle that already ran.

Rules the code enforces (anything else is refused, so don't waste a decision):
- side BUY: stop < entry < target. side SELL (short): target < entry < stop.
- reward:risk at least {min_rr}. No new entries after last_entry.
- order "stop": a trigger that fills only if price trades through it (BUY above ltp, SELL below).
  order "limit": rests at a better price (BUY below ltp, SELL above). order "now": fill immediately.
- at most {max_conc} positions at once, {max_entries} entries per symbol per day, leverage {lev}x.
- shorts allowed: {shorts}. Symbols: only those listed in markets.

Reply with ONE JSON object, nothing else:
{{"view": "<one or two sentences on the market>",
  "decisions": [
    {{"action":"enter","symbol":"NIFTYBEES","side":"BUY","order":"stop","entry":281.05,"stop":280.30,
      "target":282.60,"size":1.0,"confidence":0.6,"reason":"<why, citing levels>"}},
    {{"action":"cancel","order_id":"<resting entry id>","reason":"..."}},
    {{"action":"exit","symbol":"...","reason":"..."}}
  ]}}
"decisions" may be empty. size is 0.25 to 1.0 of a normal slot."""


def find_claude() -> Optional[str]:
    import chat
    return chat.find_claude()


def ask_claude(ctx: dict, limits, run: Optional[Callable] = None) -> dict:
    prompt = SYSTEM.format(min_rr=limits.min_reward_to_risk, max_conc=limits.max_concurrent_positions,
                           max_entries=ctx["account"]["max_entries_per_symbol"], lev=limits.max_leverage,
                           shorts="yes" if allow_short() else "no")
    prompt += "\n\nSTATE:\n" + json.dumps(ctx, separators=(",", ":"))
    if run is None:
        exe = find_claude()
        if not exe:
            raise RuntimeError("claude CLI not found on this box")
        cmd = ["sudo", "-u", CLI_USER, "-H", exe, "-p", "--output-format", "json", "--tools", ""]
        model = common.setting("TRADER_BRAIN_MODEL")
        if model:
            cmd[6:6] = ["--model", model]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT,
                           cwd=f"/home/{CLI_USER}")
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[-300:])
        try:
            raw = json.loads(r.stdout).get("result") or ""
        except ValueError:
            raw = r.stdout
    else:
        raw = run(prompt)
    return parse(raw)


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
            if (sym, side) in resting:
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
    if mode == "claude":
        try:
            out = ask_claude(b["ctx"], limits, run=run)
            out["source"] = "claude"
            return {**out, "insts": b["insts"], "ctx": b["ctx"]}
        except Exception as exc:                        # noqa: BLE001
            common.log("brain", {"event": "brain_unavailable", "reason": str(exc)[:300],
                                 "action": "rule-based decisions this scan"})
            if common.setting("TRADER_BRAIN_FALLBACK", "rules").lower() != "rules":
                return {"view": "", "decisions": [], "source": "none", "insts": b["insts"], "ctx": b["ctx"]}
    out = rules(b["ctx"], b["insts"], session)
    out["source"] = "rules"
    return {**out, "insts": b["insts"], "ctx": b["ctx"]}
