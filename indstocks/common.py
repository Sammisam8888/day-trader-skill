"""
What every program here shares: the day log, the kill switch, settings read
fresh from the env file, the risk limits, the symbol list, and phone alerts.

Settings are read from /etc/indstocks.env on every call rather than once at
import: a limit changed on the dashboard or in chat applies on the next
tick of the long-running fill monitor without a restart.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import risk as risk_mod                                 # noqa: E402
import session as sess                                  # noqa: E402

STATE_DIR = Path(os.environ.get("TRADER_STATE_DIR", "/var/lib/indstocks/auto"))
ENV_FILE = Path(os.environ.get("TRADER_ENV_FILE", "/etc/indstocks.env"))
KILL_SWITCH = Path(os.environ.get("TRADER_KILL_SWITCH", "/etc/indstocks.halt"))

DEFAULT_SYMBOLS = "NIFTYBEES"


def setting(key: str, default: str = "") -> str:
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return os.environ.get(key, default).strip()


def live() -> bool:
    """Off unless explicitly on. A misread config fails closed."""
    return setting("TRADER_LIVE", "false").lower() in ("1", "true", "yes")


def symbols() -> List[str]:
    raw = setting("TRADER_SYMBOLS", DEFAULT_SYMBOLS)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def day_control() -> dict:
    """Today's control state (control.py): halt, risk overrides."""
    try:
        return json.loads((STATE_DIR / "control" / f"day-{sess.now().strftime('%Y-%m-%d')}.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def limits() -> risk_mod.Limits:
    """The owner's limits with today's control overrides applied (control.py
    has already checked every override against the owner's ceilings)."""
    import dataclasses
    lim = base_limits()
    risk = day_control().get("risk") or {}
    fields = {"max_loss_per_trade": ("max_loss_per_trade", Decimal), "daily_loss_limit": ("daily_loss_limit", Decimal),
              "max_trades": ("max_trades_per_day", int), "max_concurrent": ("max_concurrent_positions", int),
              "max_leverage": ("max_leverage", Decimal)}
    changes = {}
    for k, (attr, typ) in fields.items():
        if k in risk:
            try:
                v = typ(str(risk[k]))
            except Exception:                           # noqa: BLE001
                continue
            if k == "daily_loss_limit" and v > lim.daily_loss_limit:
                continue                                # never raised, whatever the file says
            changes[attr] = v
    return dataclasses.replace(lim, **changes) if changes else lim


def base_limits() -> risk_mod.Limits:
    return risk_mod.Limits(
        max_loss_per_trade=Decimal(setting("TRADER_MAX_LOSS", "500")),
        daily_loss_limit=Decimal(setting("TRADER_DAILY_LOSS", "1500")),
        max_trades_per_day=int(setting("TRADER_MAX_TRADES", "4")),
        max_leverage=Decimal(setting("TRADER_MAX_LEVERAGE", "1.0")),
        max_concurrent_positions=int(setting("TRADER_MAX_CONCURRENT", "2")),
        min_reward_to_cost=Decimal(setting("TRADER_MIN_REWARD_TO_COST", "2")),
        min_reward_to_risk=Decimal(setting("TRADER_MIN_RR", "1.5")),
        min_circuit_distance_pct=Decimal(setting("TRADER_MIN_CIRCUIT_PCT", "2")),
        cash_buffer_pct=Decimal(setting("TRADER_CASH_BUFFER_PCT", "5")))


def halted() -> Optional[str]:
    h = day_control().get("halt")
    if h:
        return f"halted for today by {h.get('by')}: {h.get('reason')}"
    if KILL_SWITCH.exists():
        try:
            reason = KILL_SWITCH.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
        return reason or "kill switch present"
    return None


def log_path(day: Optional[str] = None) -> Path:
    return STATE_DIR / f"{day or sess.now().strftime('%Y-%m-%d')}.jsonl"


def log(job: str, record: dict) -> dict:
    rec = {"ts": sess.now().isoformat(timespec="seconds"), "job": job,
           "live": live(), **record}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass
    print(json.dumps(rec, default=str), flush=True)
    return rec


def read_log(day: Optional[str] = None) -> List[dict]:
    out = []
    try:
        for line in log_path(day).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def events(name: str, day: Optional[str] = None) -> List[dict]:
    return [r for r in read_log(day) if r.get("event") == name]


def notify(title: str, body: str, *, priority: str = "default") -> None:
    """Push to the phone via ntfy.sh when TRADER_NTFY_TOPIC is set."""
    topic = setting("TRADER_NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:                                   # noqa: BLE001
        pass


def our_order_ids(day: Optional[str] = None) -> set:
    """Every broker order id this system created today, from the log."""
    ids = set()
    for r in read_log(day):
        if r.get("order_id") and r.get("event") in (
                "placed", "stop_placed", "target_exit", "sold", "covered",
                "short_covered", "chat_buy", "chat_sell", "stop_breached_exit"):
            ids.add(str(r["order_id"]))
    return ids
