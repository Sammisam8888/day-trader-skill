"""
Fill monitor: the part that reacts in seconds, not every ten minutes.

The autotrader rests many trigger orders across the watchlist because an
unfilled order costs nothing. What must not happen is all of them firing on
the same index move. So this loop polls the broker every few seconds during
the session and does three mechanical things, none of them a judgement call:

  1. CANCEL ON FILL. The moment the number of open intraday positions
     (bot-filled or bought by hand -- capital is capital) reaches
     max_concurrent, every other resting auto/ entry is cancelled, parent
     and leg. The race window is one poll interval; intraday margin absorbs
     a third fill landing inside it, and the flatten closes it.

  2. PROTECT MANUAL POSITIONS. A position the owner opened by hand has no
     stop or target. Each poll compares its LTP to its average price: past
     TRADER_MANUAL_TARGET_PCT it is sold at market, past
     -TRADER_MANUAL_STOP_PCT it is sold at market. Bot positions are left to
     their own OCO legs.

  3. NEAR CLOSE. From LAST_ENTRY onward no new fills are wanted: every
     resting entry is cancelled so nothing opens into the flatten.

Standard library only. Polling, not the order-update WebSocket: that needs
a third-party library and a reconnect dance, and five seconds is fast
enough for a cancel. It can be swapped in later without changing the
decisions here.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import session as sess                                 # noqa: E402
from client import Client                               # noqa: E402
from flatten import cancel_order, cancel_resting_legs, open_intraday, sell  # noqa: E402

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
HALT_FILE = Path(os.environ.get("TRADER_KILL_SWITCH", "/etc/indstocks.halt"))
POLL_S = float(os.environ.get("TRADER_FILL_POLL_S", "5"))
ENV_FILE = Path(os.environ.get("TRADER_ENV_FILE", "/etc/indstocks.env"))


def setting(key: str, default: str) -> str:
    """Read a TRADER_* value fresh each tick, so a limit changed on the
    dashboard applies without a restart."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.environ.get(key, default)


def max_concurrent() -> int:
    return int(setting("TRADER_MAX_CONCURRENT", "2"))


def max_leverage() -> Decimal:
    return Decimal(setting("TRADER_MAX_LEVERAGE", "1.0"))


def resting_ttl_min() -> int:
    """Minutes a resting entry may wait for its trigger. 0 = forever."""
    return int(setting("TRADER_RESTING_TTL_MIN", "30"))


def placed_at(order_id: str) -> datetime | None:
    """When we placed this order, from our own log (the broker's
    created_at field is not consistently present)."""
    try:
        lines = (STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for ln in lines:
        if order_id not in ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("event") == "placed" and str(r.get("parent_order_id")) == order_id:
            try:
                return datetime.fromisoformat(r["ts"])
            except (KeyError, ValueError):
                return None
    return None


def placed_records() -> dict:
    """parent_order_id -> placement record, from today's log."""
    out = {}
    try:
        lines = (STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        if '"placed"' not in ln:
            continue
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("event") == "placed" and r.get("parent_order_id"):
            out[str(r["parent_order_id"])] = r
    return out


def round_down(x: Decimal, tick: Decimal) -> Decimal:
    return (x // tick) * tick


def breakeven_r() -> Decimal:
    return Decimal(setting("TRADER_BREAKEVEN_R", "1.0"))


def trail_r() -> Decimal:
    return Decimal(setting("TRADER_TRAIL_R", "1.0"))


def stop_band_pct() -> Decimal:
    return Decimal(setting("TRADER_STOP_BAND_PCT", "1.0"))


def new_stop(entry: Decimal, stop: Decimal, ltp: Decimal, tick: Decimal,
             current_sl: Decimal) -> Decimal | None:
    """Where the stop should be now, or None if it should not move.

    +breakeven_r:  stop to entry (a winner must not become a loser)
    beyond that:   stop trails ltp by trail_r, only ever upward
    """
    r = entry - stop
    if r <= 0 or ltp <= 0:
        return None
    gain_r = (ltp - entry) / r
    if gain_r < breakeven_r():
        return None
    candidate = entry
    if gain_r >= breakeven_r() + Decimal("0.5"):
        candidate = max(entry, ltp - r * trail_r())
    candidate = round_down(candidate, tick)
    if candidate <= current_sl:
        return None
    return candidate


def modify_leg(client, leg: dict, sl: Decimal, tick: Decimal, tgt: Decimal) -> None:
    band = sl * stop_band_pct() / 100
    body = {"order_id": str(leg.get("id") or leg.get("order_id")), "segment": "EQUITY",
            "algo_id": "99999",
            "sl_trigger_price": float(sl),
            "sl_limit_price": float(round_down(sl - band, tick)),
            "tgt_trigger_price": float(tgt),
            "tgt_limit_price": float(leg.get("tgt_limit_price") or tgt)}
    client.post("/smart/order/modify", body=body)


def manage_stops(client, rows: list, positions: list, ltps: dict) -> int:
    """Move stops up on winners. Returns how many were moved."""
    recs = placed_records()
    legs = {str(r.get("parent_order_id")): r for r in rows
            if r.get("parent_order_id") and str(r.get("txn_type", "")).upper() == "SELL"
            and str(r.get("status", "")).upper() in PENDING}
    held = {p["security_id"] for p in positions}
    moved = 0
    for pid, rec in recs.items():
        leg = legs.get(pid)
        if not leg or str(leg.get("security_id")) not in held:
            continue
        try:
            entry, stop = Decimal(rec["entry"]), Decimal(rec["stop"])
            tgt = Decimal(rec["target"])
            tick = Decimal(rec.get("tick") or "0.05")
            current_sl = Decimal(str(leg.get("sl_trigger_price") or stop))
        except Exception:                               # noqa: BLE001
            continue
        ltp = ltps.get(str(leg.get("security_id")))
        if ltp is None:
            continue
        sl = new_stop(entry, stop, ltp, tick, current_sl)
        if sl is None:
            continue
        try:
            modify_leg(client, leg, sl, tick, tgt)
            moved += 1
            log({"event": "stop_moved", "name": leg.get("name") or rec.get("symbol"),
                 "order_id": leg.get("id"), "from": str(current_sl), "to": str(sl),
                 "ltp": str(ltp), "entry": str(entry),
                 "reason": f"up {(ltp - entry) / (entry - stop):.2f}R; "
                           + ("locked at breakeven" if sl == round_down(entry, tick) else "trailing")})
        except Exception as exc:                        # noqa: BLE001
            log({"event": "modify_failed", "order_id": leg.get("id"), "reason": str(exc)[:200]})
    return moved


def invalidate_by_price(client, rows: list, ltps: dict) -> int:
    """A resting breakout order whose stock has fallen below the opening
    range LOW is a dead thesis: cancel it now rather than at the TTL."""
    recs = placed_records()
    n = 0
    for r in resting_entries(rows):
        rec = recs.get(str(r.get("id") or r.get("order_id")))
        if not rec or not rec.get("range_low"):
            continue
        ltp = ltps.get(str(r.get("security_id")))
        if ltp is None:
            continue
        if ltp < Decimal(rec["range_low"]) and cancel_entry(client, r):
            n += 1
            log({"event": "invalidated", "name": r.get("name"), "order_id": r.get("id"),
                 "ltp": str(ltp), "range_low": rec["range_low"],
                 "reason": "price fell below the opening range; breakout thesis dead"})
            time.sleep(0.2)
    return n


def expire_stale(client, rows: list, now: datetime) -> int:
    """Cancel resting entries older than the TTL, so the scanner can move
    on to a name that is actually moving."""
    ttl = resting_ttl_min()
    if ttl <= 0:
        return 0
    n = 0
    for r in resting_entries(rows):
        oid = str(r.get("id") or r.get("order_id"))
        t0 = placed_at(oid)
        if t0 is None:
            continue
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=sess.IST)
        age = (now - t0).total_seconds() / 60
        if age >= ttl and cancel_entry(client, r):
            n += 1
            log({"event": "expired", "name": r.get("name"), "order_id": oid,
                 "reason": f"resting {age:.0f} min without a fill, ttl {ttl} min"})
            time.sleep(0.2)
    return n
MANUAL_TARGET_PCT = Decimal(os.environ.get("TRADER_MANUAL_TARGET_PCT", "1.0"))
MANUAL_STOP_PCT = Decimal(os.environ.get("TRADER_MANUAL_STOP_PCT", "0.5"))
PENDING = ("PENDING", "SL-PENDING", "O-PENDING", "QUEUED", "CREATED",
           "INITIATED", "MODIFIED", "TRIGGER PENDING")


def log(record: dict) -> None:
    record = {"ts": sess.now().isoformat(timespec="seconds"), "job": "fill",
              **record}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{sess.now():%Y-%m-%d}.jsonl", "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(json.dumps(record, default=str), flush=True)


def funds(client):
    try:
        bal = ((client.get("/funds") or {}).get("data") or {}).get("detailed_avl_balance") or {}
        return Decimal(str(bal["eq_mis"]))
    except Exception:                                   # noqa: BLE001
        return None


def order_book(client) -> list:
    rows = (client.get("/order-book") or {}).get("data") or []
    return rows if isinstance(rows, list) else []


def resting_entries(rows: list) -> list:
    """Our auto/ BUY parents that have not traded and are still pending."""
    out = []
    for r in rows:
        if not str(r.get("remarks", "")).startswith("auto/"):
            continue
        if r.get("parent_order_id"):
            continue
        if str(r.get("txn_type", "")).upper() != "BUY":
            continue
        if str(r.get("status", "")).upper() not in PENDING:
            continue
        try:
            if int(r.get("traded_qty") or 0) > 0:
                continue
        except (TypeError, ValueError):
            pass
        out.append(r)
    return out


def protected_ids(rows: list) -> set:
    """security_ids that have a pending SELL (an OCO leg or any stop)."""
    out = set()
    for r in rows:
        if str(r.get("txn_type", "")).upper() != "SELL":
            continue
        if str(r.get("status", "")).upper() in PENDING:
            out.add(str(r.get("security_id")))
    return out


def ltp_for(client, security_ids: list) -> dict:
    out = {}
    codes = [f"NSE_{s}" for s in security_ids]
    for i in range(0, len(codes), 5):
        batch = codes[i:i + 5]
        try:
            data = (client.get("/market/quotes/ltp",
                               params={"scrip-codes": ",".join(batch)})
                    or {}).get("data") or {}
        except Exception:                               # noqa: BLE001
            continue
        for code, blk in data.items():
            try:
                out[code.split("_", 1)[1]] = Decimal(str((blk or {}).get("live_price")))
            except Exception:                           # noqa: BLE001
                pass
    return out


def cancel_entry(client, row: dict) -> bool:
    """Cancel a resting parent. The linked leg never activates without it,
    but it is cancelled too so the book shows nothing dangling."""
    oid = row.get("id") or row.get("order_id")
    try:
        cancel_order(client, oid)
    except Exception as exc:                            # noqa: BLE001
        log({"event": "cancel_failed", "order_id": oid, "reason": str(exc)[:200]})
        return False
    return True


def cancel_all_entries(client, rows: list, reason: str) -> int:
    n = 0
    for r in resting_entries(rows):
        if cancel_entry(client, r):
            n += 1
            log({"event": "cancelled_on_fill", "name": r.get("name"),
                 "order_id": r.get("id") or r.get("order_id"), "reason": reason})
        time.sleep(0.2)
    return n


def manual_decision(avg: Decimal, ltp: Decimal) -> str | None:
    if avg <= 0 or ltp <= 0:
        return None
    move = (ltp - avg) / avg * 100
    if move >= MANUAL_TARGET_PCT:
        return f"up {move:.2f}% >= target {MANUAL_TARGET_PCT}%"
    if move <= -MANUAL_STOP_PCT:
        return f"down {-move:.2f}% >= stop {MANUAL_STOP_PCT}%"
    return None


def tick(client, now: datetime) -> dict:
    """One poll. Returns what it did, for tests and the log."""
    session = sess.session_for(now)
    if session is None or now < session.open or now >= session.close:
        return {"idle": True}

    rows = order_book(client)
    positions = open_intraday(client)
    held = {p["security_id"]: p for p in positions}
    out = {"held": len(held), "resting": len(resting_entries(rows)), "cancelled": 0, "sold": 0}

    # 3. nothing new should open into the flatten
    if not session.accepts_new_entries(now) and out["resting"]:
        out["cancelled"] = cancel_all_entries(client, rows, "past last-entry time")
        return out

    # LTPs for everything we hold or have resting, one batch
    ids = list(held) + [str(r.get("security_id")) for r in resting_entries(rows)]
    ltps = ltp_for(client, list(dict.fromkeys(ids))) if ids else {}

    # 0a. winners: move the stop up (breakeven at +1R, then trail)
    out["moved"] = manage_stops(client, rows, positions, ltps)

    # 0b. a resting order whose stock broke the range low, or has waited
    #     too long, is not going to work
    out["cancelled"] += invalidate_by_price(client, rows, ltps)
    out["cancelled"] += expire_stale(client, rows, now)
    if out["cancelled"]:
        rows = order_book(client)
        out["resting"] = len(resting_entries(rows))

    # 1. cancel on fill: by count, and by exposure. A resting order that
    #    would push held value past cash x leverage is cancelled even when
    #    the count is under the cap.
    cap = max_concurrent()
    if len(held) >= cap and out["resting"]:
        out["cancelled"] = cancel_all_entries(
            client, rows, f"{len(held)} position(s) open, cap {cap}")
    elif out["resting"]:
        held_value = sum(Decimal(str(p.get("avg_price") or 0)) * p["qty"] for p in positions)
        cash = funds(client)
        if cash is not None and held_value > 0:
            ceiling = cash * max_leverage()
            for r in resting_entries(rows):
                try:
                    value = Decimal(str(r.get("trigger_price") or r.get("requested_price") or 0)) \
                        * int(r.get("requested_qty") or 0)
                except Exception:                       # noqa: BLE001
                    continue
                if held_value + value > ceiling and cancel_entry(client, r):
                    out["cancelled"] += 1
                    log({"event": "cancelled_on_fill", "name": r.get("name"),
                         "order_id": r.get("id") or r.get("order_id"),
                         "reason": f"held {held_value:.0f} + this {value:.0f} > "
                                   f"{ceiling:.0f} ({max_leverage()}x of cash {cash:.0f})"})

    # 2. protect positions that have no sell order of their own
    protected = protected_ids(rows)
    bare = [p for p in positions if p["security_id"] not in protected]
    if bare:
        ltps = ltp_for(client, [p["security_id"] for p in bare])
        for p in bare:
            raw = p.get("avg_price")
            try:
                avg = Decimal(str(raw))
            except Exception:                           # noqa: BLE001
                continue
            ltp = ltps.get(p["security_id"])
            if ltp is None:
                continue
            why = manual_decision(avg, ltp)
            if not why:
                continue
            cancel_resting_legs(client, p["security_id"])
            try:
                resp = sell(client, p)
                oid = ((resp or {}).get("data") or {}).get("order_id")
                log({"event": "sold", "name": p["name"], "qty": p["qty"],
                     "avg": str(avg), "ltp": str(ltp), "order_id": oid,
                     "reason": f"manual position {why}"})
                out["sold"] += 1
            except Exception as exc:                    # noqa: BLE001
                log({"event": "sell_failed", "name": p["name"],
                     "reason": str(exc)[:200]})
    return out


def run() -> int:
    client = Client()
    log({"event": "fillmonitor_start", "poll_s": POLL_S,
         "max_concurrent": max_concurrent()})
    last = None
    while True:
        now = sess.now()
        if HALT_FILE.exists():
            time.sleep(POLL_S)
            continue
        try:
            got = tick(client, now)
        except Exception as exc:                        # noqa: BLE001
            log({"event": "error", "reason": str(exc)[:200]})
            time.sleep(POLL_S * 2)
            continue
        if got.get("idle"):
            time.sleep(30)
            continue
        key = (got.get("held"), got.get("resting"))
        if key != last or got.get("cancelled") or got.get("sold") or got.get("moved"):
            log({"event": "state", **got})
            last = key
        time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(run())
