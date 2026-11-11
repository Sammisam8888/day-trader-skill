from dataclasses import replace
from datetime import date, time
from decimal import Decimal as D
import portfolio_sim as sim
data = sim.load("candles.json.gz")
M = {"aug": (date(2026,8,6), date(2026,9,9)), "val": (date(2026,9,10), date(2026,10,9)), "test": (date(2026,10,12), date(2026,11,11))}
NOVWAP = {f"{s} {d}" for s in ("orb", "failed_break", "day_break") for d in ("long", "short")}
V3 = sim.Config(name="V3", use_book=False, direction_gate=True, target_r=D(4), lunch_block=(time(12,0), time(13,0)),
                max_concurrent=3, stop_day_after_losses=3, allowed_setups=NOVWAP, leverage=D(5))
exps = {
    "V3": V3,
    "V3 + flip after 3 (mirror must win)": replace(V3, flip=True),
    "V3 + flip after 2": replace(V3, flip=True, flip_after_losses=2),
    "V3 + flip after 3, halt after 1 flipped loser": replace(V3, flip=True, flip_halt_losses=1),
}
for name, cfg in exps.items():
    out, tot, flips = [], 0, 0
    for label, (a, b) in M.items():
        r = sim.run(data, cfg, a, b); tot += r["net"]
        fl = [x for x in r["daily"] if x.get("flipped_at")]
        flips += len(fl)
        out.append(f"{label} {r['net']:+6.0f} n{r['trades']:3d} g{r['green_days']:2d}/{r['days']} dd{r['max_drawdown']:5.0f} flips{len(fl)}")
    print(f"{name:46s} | " + " | ".join(out) + f" | 3-mo {tot:+6.0f}", flush=True)
r = sim.run(data, replace(V3, flip=True), *M["test"])
print([ (x["day"][5:], x["flipped_at"], round(x["net"]), round(x["mirror_net"])) for x in r["daily"] if x.get("flipped_at") or x.get("halted")])
