"""
A second pair of eyes, every few minutes. It reports; it never trades.

  LOGIN       no Shoonya session in the half hour before the open: nothing
              can trade or be protected until someone taps Log in.
  MONITOR     the fill monitor service is not running: fills would go
              unprotected and the flatten would rely on the timers alone.
  UNPROTECTED an intraday long with no pending SELL order: no stop at the
              exchange. The monitor should fix this within seconds; if it
              is still true here, something is wrong.
  NEAR CLOSE  anything still open after the flat-by time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common                                            # noqa: E402
import session as sess                                   # noqa: E402
from shoonya import LoginRequired, Shoonya, ShoonyaError, load_token  # noqa: E402

JOB = "watch"


def monitor_running() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "fillmonitor"], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip() == "active"
    except Exception:                                   # noqa: BLE001
        return True                                     # cannot tell; do not cry wolf


def alerts(broker, now) -> list:
    session = sess.session_for(now)
    if session is None:
        return []
    out = []
    if session.open - timedelta(minutes=45) <= now < session.close and load_token(now) is None:
        out.append(("LOGIN", "No Shoonya session. Open the dashboard and tap Log in."))
        return out
    if not (session.open <= now < session.close + timedelta(minutes=10)):
        return out
    if not monitor_running():
        out.append(("MONITOR", "fillmonitor service is not running"))
    try:
        positions = [p for p in broker.positions() if p["qty"] != 0]
        orders = broker.orders()
    except LoginRequired:
        return [("LOGIN", "Shoonya session ended. Log in from the dashboard.")]
    except ShoonyaError as exc:
        return [("ERROR", f"broker unreadable: {exc}")]
    pending_sell = {o["token"] for o in orders if o["pending"] and o["side"] == "S"}
    for p in positions:
        if p["qty"] > 0 and p["token"] not in pending_sell and now < session.flat_by:
            out.append(("UNPROTECTED", f"{p['symbol']} x{p['qty']} has no stop at the exchange"))
        if now >= session.flat_by + timedelta(minutes=8):
            out.append(("NEAR CLOSE", f"{p['symbol']} x{p['qty']} still open after the flatten"))
    return out


def run(broker=None, now=None) -> int:
    now = now or sess.now()
    broker = broker or Shoonya()
    found = alerts(broker, now)
    if any(level == "LOGIN" for level, _ in found):
        import autologin
        if autologin.configured():
            try:
                autologin.run()
                found = alerts(broker, now)
            except autologin.AutoLoginError as exc:
                common.log(JOB, {"event": "autologin_failed", "reason": str(exc)[:200]})
    if not found:
        common.log(JOB, {"event": "ok"})
        return 0
    for level, msg in found:
        common.log(JOB, {"event": "ALERT", "level": level, "action": msg})
        common.notify(f"trader: {level}", msg,
                      priority="urgent" if level in ("UNPROTECTED", "NEAR CLOSE") else "high")
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
