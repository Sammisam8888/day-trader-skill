"""
The AI as the bench's portfolio manager: it reads the market and how every
paper model is doing, then sets the knobs of each model's "@ai" copy for the
rest of the day. It never places an order and never touches the real account.

Runs at 09:22 and every 30 minutes to 14:20 (tuner.timer). Each run:

  reads   the time, NIFTY/BANKNIFTY tape (momentum, volume, book), market
          breadth and sectors, sentiment and news, today's bench (fixed vs
          inverted vs @ai, per model), the last days' bench results, and the
          AI's own earlier settings today
  writes  STATE_DIR/paper/params-DAY.json (current knobs, read live by
          paper.py) and params-DAY.jsonl (every change, with the reason)

Every knob is clamped to its tested range (paper.KNOBS); a reply that cannot
be parsed leaves the previous settings in place. Whether this tuning adds
anything is measured on the bench: "@ai" against the fixed copies.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import paper  # noqa: E402
import session as sess  # noqa: E402

PROMPT = """You manage a bench of rule-based intraday trading models on NSE stocks (paper money;
Rs 10,000 each at 5x, real charges, flat by 14:50). You do not trade. You set the KNOBS of each
model's "@ai" copy for the rest of today, so that it fits the market you see. Fixed copies with
default knobs, and their inverted twins ("_inv"), run beside yours: your settings are judged by
whether "@ai" beats them after charges.

Models (recipe: what it does):
  orb                opening-range breakout: trade the break of the first `minutes`
  vwap_revert        fade a stretch of band_sd standard deviations from VWAP, target VWAP
  vwap_trend         ride `confirm` closes on the far side of VWAP after crossing it
  ema_cross          5-minute EMA fast/slow crossover
  rsi2               5-minute RSI(2) extremes in the direction of the 20-EMA
  bollinger_fade     fade a close outside the 5-minute Bollinger band (sd)
  squeeze_break      break out of a Bollinger-inside-Keltner squeeze
  volume_burst       follow a 1-minute bar with vol_x times normal volume and a big body
  opening_drive_fade fade a first-30-minute move larger than move_pct once it retraces

Every recipe also has: enabled (0 = off for the day), invert (1 = take the opposite side of every
signal), side (0 both, 1 long only, 2 short only).

Read the day: trending (breakouts and trend models win, fades lose) or choppy/range (breakouts
fail and reverse, fades and inverted breakouts win), the direction, volatility and volume, and the
bench so far. Switch off what does not fit, invert what keeps failing the same way, widen or
tighten what is close. Charges are about Rs 24 per Rs 50,000 round trip: fewer, better trades win.
Change only what you have a reason to change.

KNOBS (value ranges you may use; anything outside is clamped):
{knobs}

Reply with ONLY this JSON:
{{"day_read": "<one or two sentences: what kind of day this is and why>",
  "models": {{"<recipe>": {{"<knob>": <number>, ..., "why": "<short reason>"}}, ...}}}}
Recipes you leave out keep their current settings.

STATE:
{state}
"""


def _tape(sym: str) -> Optional[dict]:
    try:
        import tape
        f = tape.read_live(max_age_s=120).get(sym)
        return {k: f.get(k) for k in ("ltp", "chg_1m", "chg_5m", "vol_x", "upticks", "imbalance", "momentum")} if f else None
    except Exception:                                    # noqa: BLE001
        return None


def _day_open_move(sym: str, day: str) -> Optional[dict]:
    """The index's move so far today and its range, from the tape recording."""
    p = common.STATE_DIR / "tape" / day / f"{sym}.jsonl"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    px = []
    for line in lines[::20]:
        try:
            r = json.loads(line)
            if r.get("lp") is not None:
                px.append(float(r["lp"]))
        except ValueError:
            continue
    if len(px) < 2:
        return None
    o, last, hi, lo = px[0], px[-1], max(px), min(px)
    return {"from_open_pct": round((last - o) / o * 100, 2), "range_pct": round((hi - lo) / o * 100, 2),
            "where_in_range": round((last - lo) / (hi - lo), 2) if hi > lo else 0.5}


def _bench(day: str) -> Optional[dict]:
    try:
        d = json.loads((paper.PAPER_DIR / f"{day}-summary.json").read_text())
    except (OSError, ValueError):
        return None
    return {m: {"net": round(float(r["net"])), "trades": r["trades"], "wins": r["wins"]}
            for m, r in d.get("models", {}).items() if r.get("trades")}


def _past_days(day: str, n: int = 5) -> list:
    out = []
    for f in sorted(paper.PAPER_DIR.glob("*-summary.json"), reverse=True):
        dd = f.name[:10]
        if dd >= day:
            continue
        b = _bench(dd)
        if b:
            ranked = sorted(b.items(), key=lambda x: -x[1]["net"])
            out.append({"day": dd, "best": ranked[:4], "worst": ranked[-3:],
                        "total": sum(v["net"] for v in b.values())})
        if len(out) >= n:
            break
    return out


def current(day: str) -> dict:
    try:
        return json.loads((paper.PAPER_DIR / f"params-{day}.json").read_text())
    except (OSError, ValueError):
        return {"models": {}}


def state(now) -> dict:
    day = now.strftime("%Y-%m-%d")
    st = {"time": now.strftime("%H:%M"), "nifty": {"tape": _tape("NIFTYBEES"), "today": _day_open_move("NIFTYBEES", day)},
          "banknifty": {"tape": _tape("BANKBEES"), "today": _day_open_move("BANKBEES", day)},
          "bench_today": _bench(day), "bench_past_days": _past_days(day),
          "your_settings_now": current(day).get("models", {})}
    try:
        import brain
        st["internals"] = brain._internals()
        st["sentiment"] = brain.sentiment_view(now)
    except Exception:                                    # noqa: BLE001
        pass
    return st


def knob_table() -> str:
    lines = []
    for r in paper.RECIPES:
        ks = ", ".join(f"{k} {lo}..{hi} (default {d})" for k, (d, lo, hi, _) in paper.knobs_for(r).items())
        lines.append(f"  {r}: {ks}")
    return "\n".join(lines)


def validate(reply: dict, prev: dict) -> dict:
    """Merge the reply into the previous settings; unknown recipes and knobs are dropped,
    values clamped to the tested ranges."""
    models = dict(prev.get("models") or {})
    for r, kv in (reply.get("models") or {}).items():
        if r not in paper.RECIPES or not isinstance(kv, dict):
            continue
        why = str(kv.get("why", ""))[:200]
        merged = {**(models.get(r) or {}), **{k: v for k, v in kv.items() if k in paper.knobs_for(r)}}
        clean = paper.clamp(r, merged)
        models[r] = {k: clean[k] for k in merged if k in clean}
        if why:
            models[r]["why"] = why
    return {"day_read": str(reply.get("day_read", ""))[:400], "models": models}


def run(now=None, ask: Optional[Callable[[str], str]] = None) -> dict:
    now = now or sess.now()
    day = now.strftime("%Y-%m-%d")
    prev = current(day)
    prompt = PROMPT.format(knobs=knob_table(), state=json.dumps(state(now), separators=(",", ":"), default=str))
    if ask is None:
        import brain
        ask = lambda p: brain.run_cli(p, timeout=240)            # noqa: E731
    t0 = time.monotonic()
    try:
        import brain
        reply = brain.parse_obj(ask(prompt))
    except Exception as exc:                                     # noqa: BLE001
        common.log("tuner", {"event": "tuner_failed", "reason": str(exc)[:300]})
        return prev
    out = validate(reply, prev)
    out["at"] = now.isoformat(timespec="seconds")
    paper.PAPER_DIR.mkdir(parents=True, exist_ok=True)
    p = paper.PAPER_DIR / f"params-{day}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(p)
    with open(paper.PAPER_DIR / f"params-{day}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, separators=(",", ":")) + "\n")
    common.log("tuner", {"event": "tuner_set", "day_read": out["day_read"],
                         "models": {r: {k: v for k, v in kv.items() if k != "why"} for r, kv in out["models"].items()},
                         "latency_s": round(time.monotonic() - t0, 1)})
    return out


if __name__ == "__main__":
    n = sess.now()
    if n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) < (14, 40):
        run(n)
