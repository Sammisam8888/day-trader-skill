import json
from dataclasses import replace, asdict
from datetime import date, time
from decimal import Decimal as D
import portfolio_sim as sim
data = sim.load("candles.json.gz")
M = {"aug": (date(2026,8,6), date(2026,9,9)), "val": (date(2026,9,10), date(2026,10,9)), "test": (date(2026,10,12), date(2026,11,11))}
NOVWAP = {f"{s} {d}" for s in ("orb", "failed_break", "day_break") for d in ("long", "short")}
V1 = sim.Config(name="V1")
V2 = sim.Config(name="V2", use_book=False, direction_gate=True, target_r=D(3), lunch_block=(time(12,0), time(13,0)), max_concurrent=3, stop_day_after_losses=3)
V3 = replace(V2, name="V3", allowed_setups=NOVWAP, leverage=D(5), target_r=D(4))
out = {}
for cfg in (V1, V2, V3):
    out[cfg.name] = {m: sim.run(data, cfg, a, b) for m, (a, b) in M.items()}
json.dump(out, open("final.json", "w"), default=str)
for v, ms in out.items():
    print(v, {m: (round(r["net"]), r["trades"], r["green_days"], r["days"], round(r["max_drawdown"]), round(r["gross"]), round(r["charges"])) for m, r in ms.items()})
