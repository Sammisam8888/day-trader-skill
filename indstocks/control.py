"""
Claude's control channel: a fixed menu of typed commands, never a shell.

Claude can issue these from any trading scan (an "action":"control"
decision) or from the daily pre-market ops review (ops.py). Every command
is validated here, applied to today only (or to a dated calendar entry),
logged, pushed to the owner's phone, shown on the dashboard, and can be
undone from there with one tap.

  halt_today       {reason}                      stop new entries for the rest of today
  flatten_now      {reason}                      close everything now and halt today
  resume_today     {reason}                      lift a halt Claude set (never the owner's)
  set_risk_today   {max_loss_per_trade, max_trades, max_concurrent,
                    max_leverage, size, sides, reason}
                   Lowering anything: always allowed.
                   Raising: only up to the owner's ceilings (TRADER_CEIL_*),
                   only while today's P&L is >= 0, never before 09:45, and
                   it reverts on its own if the day turns red past half the
                   daily loss limit. The daily loss limit itself can only
                   be lowered, never raised: it is the account's last line.
  calendar_add     {kind: holiday|special_session|event, date, ...}
  calendar_remove  {kind, date}                  only entries Claude added
  notify           {message}                     push a note to the owner

Nothing here can place an order, touch credentials, change the symbol
list, or run anything on the box.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import session as sess                                   # noqa: E402

JOB = "control"
RAISE_NOT_BEFORE = time(9, 45)
CAL_HORIZON_DAYS = 45

# field -> (env key for base, env key for ceiling, type)
RISK = {
    "max_loss_per_trade": ("TRADER_MAX_LOSS", "TRADER_CEIL_MAX_LOSS", Decimal),
    "daily_loss_limit": ("TRADER_DAILY_LOSS", None, Decimal),          # lower only
    "max_trades": ("TRADER_MAX_TRADES", "TRADER_CEIL_MAX_TRADES", int),
    "max_concurrent": ("TRADER_MAX_CONCURRENT", "TRADER_CEIL_MAX_CONCURRENT", int),
    "max_leverage": ("TRADER_MAX_LEVERAGE", "TRADER_CEIL_MAX_LEVERAGE", Decimal),
}
COMMANDS = ("halt_today", "flatten_now", "resume_today", "set_risk_today", "calendar_add",
            "calendar_remove", "notify")


def control_dir() -> Path:
    return common.STATE_DIR / "control"


def day_file(day: Optional[str] = None) -> Path:
    return control_dir() / f"day-{day or sess.now().strftime('%Y-%m-%d')}.json"


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def today() -> dict:
    return read_json(day_file())


def halt_reason() -> Optional[str]:
    h = today().get("halt")
    return f"halted for today by {h['by']}: {h['reason']}" if h else None


def base(field: str):
    env, _, typ = RISK[field]
    lim = common.base_limits()
    return {"max_loss_per_trade": lim.max_loss_per_trade, "daily_loss_limit": lim.daily_loss_limit,
            "max_trades": lim.max_trades_per_day, "max_concurrent": lim.max_concurrent_positions,
            "max_leverage": lim.max_leverage}[field]


def ceiling(field: str):
    _, ceil_env, typ = RISK[field]
    if not ceil_env:
        return None
    raw = common.setting(ceil_env)
    if not raw:
        return None
    try:
        return typ(raw)
    except (ValueError, InvalidOperation):
        return None


def size_mult() -> Decimal:
    try:
        return min(Decimal(str(today().get("risk", {}).get("size", 1))), Decimal(1))
    except InvalidOperation:
        return Decimal(1)


def sides_allowed() -> list:
    s = today().get("risk", {}).get("sides", "BOTH")
    return ["BUY"] if s == "BUY" else ["SELL"] if s == "SELL" else ["BUY", "SELL"]


def history(day: Optional[str] = None) -> list:
    return read_json(day_file(day)).get("history", [])


def _record(state: dict, entry: dict) -> None:
    state.setdefault("history", []).append(entry)


def _notify(title: str, body: str, priority: str = "high") -> None:
    try:
        common.notify(title, body, priority=priority)
    except Exception:                                   # noqa: BLE001
        pass


def _gate(name: str, args: dict, trigger: str, f: dict, state: dict) -> Optional[str]:
    """None if the named trigger holds for this command, else why not."""
    import triggers as trg
    if name == "notify":
        return None
    if name in ("halt_today", "flatten_now"):
        allowed = trg.ALLOWS[name]
    elif name == "resume_today":
        h = state.get("halt") or {}
        if trigger != "condition_cleared":
            return "resume needs trigger condition_cleared"
        try:
            since = (f["now"] - datetime.fromisoformat(h["ts"])).total_seconds() / 60
        except (KeyError, ValueError):
            since = 999
        if since < 30:
            return f"halted {since:.0f} min ago; resume needs 30 min"
        prev = h.get("trigger")
        if prev and trg.check(prev, f, near_event=True)[0]:
            return f"the halt's trigger {prev} still holds"
        return None
    elif name == "set_risk_today":
        kinds = set()
        for field in RISK:
            if field in args and args[field] is not None:
                try:
                    kinds.add("raise" if RISK[field][2](str(args[field])) > base(field) else "lower")
                except (ValueError, InvalidOperation):
                    pass
        if args.get("size") is not None or args.get("sides"):
            kinds.add("lower")
        if len(kinds) > 1:
            return "raise and lower in separate commands"
        allowed = trg.ALLOWS[kinds.pop() if kinds else "lower"]
    elif name in ("calendar_add", "calendar_remove"):
        kind = str(args.get("kind", ""))
        src = str(args.get("source") or "")
        official = any(d in src for d in ("nseindia.com", "bseindia.com"))
        if kind in ("holiday", "special_session"):
            return None if official else "holidays and special sessions need an nseindia.com / bseindia.com source"
        return None if (src.strip() or official) else "events need a source"
    else:
        return None
    if trigger not in allowed:
        return f"{name} needs one of these triggers: {', '.join(sorted(allowed))}"
    ok, evidence = trg.check(trigger, f, strict=(name == "flatten_now"), near_event=(name == "halt_today"))
    if not ok:
        return f"trigger {trigger} does not hold: {evidence}"
    args["_evidence"] = evidence
    return None


def apply(cmd: dict, *, by: str = "claude", day_pnl: Optional[Decimal] = None, broker=None,
          now: Optional[datetime] = None, facts: Optional[dict] = None) -> tuple:
    """Validate and apply one command. Returns (outcome, message).
    Anyone but the owner must name a trigger that the data confirms."""
    now = now or sess.now()
    name = str(cmd.get("command", "")).strip()
    args = dict(cmd.get("args") or {k: v for k, v in cmd.items() if k not in ("action", "command")})
    reason = str(args.get("reason") or cmd.get("reason") or "")[:300]
    trigger = str(cmd.get("trigger") or args.pop("trigger", "") or "").strip()
    if name not in COMMANDS:
        return _done(name, args, by, "refused", f"unknown command {name!r}; allowed: {', '.join(COMMANDS)}", now)
    state = today()
    if by != "owner":
        import triggers as trg
        f = facts or trg.facts(day_pnl=day_pnl, now=now)
        f.setdefault("now", now)
        why = _gate(name, args, trigger, f, state)
        if why:
            return _done(name, args, by, "refused", why, now)
        if args.get("_evidence"):
            reason = f"{reason} [{trigger}: {args['_evidence']}]"
            args["reason"] = reason
    if name == "halt_today":
        state["halt"] = {"by": by, "reason": reason or "no reason given", "ts": now.isoformat(timespec="seconds"),
                         "trigger": trigger or None}
        write_json(day_file(), state)
        _notify("trader: halted for today", f"{by}: {reason}")
        return _done(name, args, by, "applied", f"no new entries for the rest of today: {reason}", now)
    if name == "flatten_now":
        state["halt"] = {"by": by, "reason": f"flatten: {reason}", "ts": now.isoformat(timespec="seconds"),
                         "trigger": trigger or None}
        write_json(day_file(), state)
        closed = 0
        if broker is not None and common.live():
            import flatten
            for o in broker.orders():
                if o["pending"] and o["id"] in common.our_order_ids():
                    try:
                        broker.cancel(o["id"])
                    except Exception:                   # noqa: BLE001
                        pass
            failed = []
            for p in broker.positions():
                if p["qty"] != 0:
                    try:                                # one failure must not leave the rest open
                        if flatten.close_position(broker, p, remarks="ctl/flatten") is not None:
                            closed += 1
                    except Exception as exc:            # noqa: BLE001
                        failed.append(f"{p['symbol']}: {exc}"[:120])
            if failed:
                _notify("trader: flatten_now could not close", "; ".join(failed), "urgent")
        _notify("trader: flattened and halted", f"{by}: {reason}", "urgent")
        return _done(name, args, by, "applied", f"closed {closed} position(s), halted today: {reason}", now)
    if name == "resume_today":
        h = state.get("halt")
        if not h:
            return _done(name, args, by, "ignored", "not halted", now)
        if h["by"] != by and by != "owner":
            return _done(name, args, by, "refused", f"halt was set by {h['by']}; only they can lift it", now)
        state.pop("halt")
        write_json(day_file(), state)
        _notify("trader: resumed", f"{by}: {reason}", "default")
        return _done(name, args, by, "applied", "halt lifted", now)
    if name == "set_risk_today":
        return _set_risk(state, args, by, reason, day_pnl, now)
    if name == "calendar_add":
        return _cal_add(args, by, reason, now)
    if name == "calendar_remove":
        return _cal_remove(args, by, now)
    if name == "notify":
        msg = str(args.get("message") or reason)[:400]
        _notify("trader: note from Claude", msg, "default")
        return _done(name, args, by, "applied", msg, now)
    return _done(name, args, by, "refused", "unhandled", now)


def _done(name, args, by, outcome, msg, now) -> tuple:
    state = today()
    _record(state, {"ts": now.isoformat(timespec="seconds"), "by": by, "command": name,
                    "args": {k: v for k, v in (args or {}).items() if k not in ("reason", "_evidence")},
                    "reason": str((args or {}).get("reason", ""))[:300], "outcome": outcome, "message": msg})
    write_json(day_file(), state)
    common.log(JOB, {"event": "control", "by": by, "command": name, "outcome": outcome, "message": msg[:300]})
    return outcome, msg


def _set_risk(state: dict, args: dict, by: str, reason: str, day_pnl, now) -> tuple:
    risk = dict(state.get("risk") or {})
    raised = dict(state.get("raised") or {})
    msgs, refused = [], []
    for field, (_, _, typ) in RISK.items():
        if field not in args or args[field] is None:
            continue
        try:
            want = typ(str(args[field]))
        except (ValueError, InvalidOperation):
            refused.append(f"{field}: not a number")
            continue
        b = base(field)
        if want <= 0:
            refused.append(f"{field}: must be positive")
            continue
        if want <= b:
            risk[field] = str(want)
            raised.pop(field, None)
            msgs.append(f"{field} {b} -> {want}")
            continue
        ceil = ceiling(field)
        if ceil is None:
            refused.append(f"{field}: raising is not allowed" + ("" if RISK[field][1] else " ever (it is the last line)")
                           + ("" if not RISK[field][1] else f" (owner has not set {RISK[field][1]})"))
            continue
        if want > ceil:
            refused.append(f"{field}: {want} is over the owner's ceiling {ceil}")
            continue
        if now.time() < RAISE_NOT_BEFORE:
            refused.append(f"{field}: no raises before {RAISE_NOT_BEFORE:%H:%M}")
            continue
        if day_pnl is None or day_pnl < 0:
            refused.append(f"{field}: raises only while today's P&L is >= 0 (it is {day_pnl})")
            continue
        risk[field] = str(want)
        raised[field] = str(want)
        msgs.append(f"{field} {b} -> {want} (raised, ceiling {ceil})")
    if "size" in args and args["size"] is not None:
        try:
            s = min(max(Decimal(str(args["size"])), Decimal("0.25")), Decimal(1))
            risk["size"] = str(s)
            msgs.append(f"size x{s}")
        except InvalidOperation:
            refused.append("size: not a number")
    if "sides" in args and args["sides"]:
        s = str(args["sides"]).upper()
        if s in ("BUY", "SELL", "BOTH"):
            risk["sides"] = s
            msgs.append(f"sides {s}")
        else:
            refused.append("sides: BUY, SELL or BOTH")
    if not msgs:
        return _done("set_risk_today", args, by, "refused", "; ".join(refused) or "nothing to change", now)
    state["risk"], state["raised"] = risk, raised
    state.setdefault("risk_by", by)
    write_json(day_file(), state)
    note = "; ".join(msgs) + (f" | refused: {'; '.join(refused)}" if refused else "")
    _notify("trader: risk changed for today", f"{by}: {note}. {reason}", "high" if raised else "default")
    return _done("set_risk_today", args, by, "applied" if not refused else "partly", note, now)


def check_raises(day_pnl: Decimal) -> Optional[str]:
    """Drop today's raises if the day has turned red past half the daily loss limit."""
    state = today()
    if not state.get("raised"):
        return None
    if day_pnl > -(common.base_limits().daily_loss_limit / 2):
        return None
    for field in list(state["raised"]):
        state.get("risk", {}).pop(field, None)
    dropped = ", ".join(state["raised"])
    state["raised"] = {}
    write_json(day_file(), state)
    msg = f"raised limits reverted ({dropped}): day P&L {day_pnl}"
    _notify("trader: raises reverted", msg)
    common.log(JOB, {"event": "control", "by": "code", "command": "revert_raises", "outcome": "applied", "message": msg})
    return msg


# ------------------------------------------------------------------ calendar

def _date(s) -> Optional[date]:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except ValueError:
        return None


def _cal_add(args: dict, by: str, reason: str, now: datetime) -> tuple:
    kind, d = str(args.get("kind", "")), _date(args.get("date"))
    if d is None:
        return _done("calendar_add", args, by, "refused", "date must be YYYY-MM-DD", now)
    if not (now.date() <= d <= now.date() + timedelta(days=CAL_HORIZON_DAYS)):
        return _done("calendar_add", args, by, "refused", f"date must be today .. +{CAL_HORIZON_DAYS} days", now)
    cal = read_json(sess.CALENDAR_FILE)
    src = str(args.get("source") or reason)[:200]
    iso = d.isoformat()
    if kind == "holiday":
        cal.setdefault("holidays", {})[iso] = {"by": by, "what": str(args.get("what") or "holiday")[:120], "source": src}
        msg = f"{iso} marked as a market holiday"
    elif kind == "special_session":
        try:
            ts = [sess._t(str(args[k])) for k in ("open", "last_entry", "flat_by", "close")]
        except (KeyError, ValueError):
            return _done("calendar_add", args, by, "refused", "special_session needs open, last_entry, flat_by, close as HH:MM", now)
        if not (ts[0] < ts[1] < ts[2] < ts[3]) or ts[0] < time(9, 0) or ts[3] > time(18, 0):
            return _done("calendar_add", args, by, "refused", "times must be open < last_entry < flat_by < close, 09:00-18:00", now)
        cal.setdefault("special", {})[iso] = {"open": f"{ts[0]:%H:%M}", "last_entry": f"{ts[1]:%H:%M}",
                                              "flat_by": f"{ts[2]:%H:%M}", "close": f"{ts[3]:%H:%M}",
                                              "by": by, "what": str(args.get("what") or "special session")[:120],
                                              "source": src}
        msg = f"special session {iso} {ts[0]:%H:%M}-{ts[3]:%H:%M}"
    elif kind == "event":
        ev = {"date": iso, "what": str(args.get("what") or "event")[:120], "by": by, "source": src}
        if args.get("at"):
            try:
                ev["at"] = f"{sess._t(str(args['at'])):%H:%M}"
            except ValueError:
                return _done("calendar_add", args, by, "refused", "at must be HH:MM", now)
        if args.get("symbol"):
            sym = str(args["symbol"]).upper()
            if sym not in common.symbols():
                return _done("calendar_add", args, by, "refused", f"{sym} is not traded", now)
            ev["symbol"], ev["results"] = sym, bool(args.get("results"))
        cal.setdefault("events", []).append(ev)
        msg = f"event {iso}{' ' + ev['at'] if 'at' in ev else ''}: {ev['what']}"
    else:
        return _done("calendar_add", args, by, "refused", "kind must be holiday, special_session or event", now)
    write_json(sess.CALENDAR_FILE, cal)
    _notify("trader: calendar changed", f"{by}: {msg} ({src})", "high" if kind != "event" else "default")
    return _done("calendar_add", args, by, "applied", msg, now)


def _cal_remove(args: dict, by: str, now: datetime) -> tuple:
    kind, d = str(args.get("kind", "")), _date(args.get("date"))
    cal = read_json(sess.CALENDAR_FILE)
    iso = d.isoformat() if d else ""
    bucket = {"holiday": "holidays", "special_session": "special"}.get(kind)
    if bucket:
        entry = (cal.get(bucket) or {}).get(iso)
        if not entry:
            return _done("calendar_remove", args, by, "ignored", f"no {kind} on {iso}", now)
        if entry.get("by") != by and by != "owner":
            return _done("calendar_remove", args, by, "refused", f"added by {entry.get('by')}", now)
        cal[bucket].pop(iso)
    elif kind == "event":
        before = len(cal.get("events") or [])
        cal["events"] = [e for e in (cal.get("events") or []) if not (e["date"] == iso and (e.get("by") == by or by == "owner"))]
        if len(cal["events"]) == before:
            return _done("calendar_remove", args, by, "ignored", f"no removable event on {iso}", now)
    else:
        return _done("calendar_remove", args, by, "refused", "kind must be holiday, special_session or event", now)
    write_json(sess.CALENDAR_FILE, cal)
    return _done("calendar_remove", args, by, "applied", f"removed {kind} {iso}", now)


def calendar_events(day: date) -> list:
    return [e for e in (read_json(sess.CALENDAR_FILE).get("events") or []) if e.get("date") == day.isoformat()]


def sync_nse_holidays(get) -> str:
    """Replace this year's holiday list with NSE's published one."""
    rows = get("holiday-master?type=trading")
    cm = rows.get("CM") if isinstance(rows, dict) else None
    if not cm:
        raise RuntimeError("NSE holiday list has no CM segment")
    by_year: dict = {}
    for r in cm:
        try:
            d = datetime.strptime(str(r.get("tradingDate")), "%d-%b-%Y").date()
        except ValueError:
            continue
        by_year.setdefault(str(d.year), []).append(d.isoformat())
    cal = read_json(sess.CALENDAR_FILE)
    cal["nse_holidays"] = {**(cal.get("nse_holidays") or {}), **{y: sorted(v) for y, v in by_year.items()}}
    cal["nse_synced"] = sess.now().isoformat(timespec="seconds")
    write_json(sess.CALENDAR_FILE, cal)
    return f"NSE holidays synced: " + ", ".join(f"{y}: {len(v)}" for y, v in sorted(by_year.items()))


def undo(day: str, index: int) -> tuple:
    """The owner reverses one of today's commands from the dashboard."""
    hist = history(day)
    if not (0 <= index < len(hist)):
        return "refused", "no such command"
    h = hist[index]
    if h["outcome"] not in ("applied", "partly"):
        return "ignored", "that command did not change anything"
    name, args = h["command"], h.get("args") or {}
    if name in ("halt_today", "flatten_now"):
        return apply({"command": "resume_today", "reason": "undone by owner"}, by="owner")
    if name == "set_risk_today":
        state = today()
        for k in list(args):
            state.get("risk", {}).pop(k, None)
            state.get("raised", {}).pop(k, None)
        write_json(day_file(), state)
        return _done("undo", {"of": name}, "owner", "applied", f"risk override removed: {', '.join(args)}", sess.now())
    if name == "calendar_add":
        return apply({"command": "calendar_remove", "args": {"kind": args.get("kind"), "date": args.get("date")}}, by="owner")
    return "ignored", f"{name} has nothing to undo"


def state_view() -> dict:
    t = today()
    up = []
    for i in range(0, 10):
        d = sess.now().date() + timedelta(days=i)
        s = sess.session_for(datetime.combine(d, time(10, 0), tzinfo=sess.IST))
        sp = sess.special_for(d)
        up.append({"date": d.isoformat(), "dow": d.strftime("%a"), "trading": s is not None,
                   "special": f"{sp[0]:%H:%M}-{sp[3]:%H:%M}" if sp else None,
                   "events": [e["what"] for e in calendar_events(d)]})
    cal = read_json(sess.CALENDAR_FILE)
    modes = common.events("day_mode")
    flips = common.events("flip_on")
    dm = dict(modes[-1]) if modes else None
    if dm is not None:
        dm["flipped_at"] = flips[0]["ts"][11:16] if flips else None
    return {"day_mode": dm, "halt": t.get("halt"), "risk": t.get("risk") or {}, "raised": t.get("raised") or {},
            "history": t.get("history") or [], "upcoming": up, "nse_synced": cal.get("nse_synced"),
            "ceilings": {f: (str(ceiling(f)) if ceiling(f) is not None else None) for f in RISK},
            "base": {f: str(base(f)) for f in RISK}}
