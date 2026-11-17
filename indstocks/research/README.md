# Portfolio replay

`portfolio_sim.py` replays the live system on real 5-minute candles from Rs 26,000,
calling the live modules (playbook, trend scores, strategy book, sizing, risk checks,
charges). `run_versions.py` runs V1 (live), V2 and V3 over three months.

Data: run the `export-candles` workflow; it publishes `candles.json.gz` to the
`sim-data` branch. Then:

    git fetch origin +refs/heads/sim-data:refs/remotes/origin/sim-data
    git show origin/sim-data:candles.json.gz > indstocks/research/candles.json.gz
    cd research && python3 run_versions.py

The AI's live judgement and historical sentiment are not replayed.

## Exit styles, 13 Nov 2026 (V3, 3 months, after charges)

| exit | 3-mo net | green days | trades |
|---|---|---|---|
| V3 single 4R target | +2,825 | 31/66 | 270 |
| profit lock armed at Rs 40, keep 50% | -9,444 | 20/66 | 508 |
| profit lock armed at 1R / 2R, keep 50% | -269 / +184 | 29 / 27 | 345 / 285 |
| profit lock armed at 3R, keep 70% (live) | +2,844 | 31/66 | 277 |
| scale-out 50% at 1R, 30% trailed, 20% runner | -1,919 | 29/66 | 562 |
| day lock at Rs 150-1000 | -1,534 to +2,008 | | |
| day lock at Rs 1500 (live: never fired) | +2,825 | 31/66 | 270 |

Early profit taking cuts the few 4R winners that pay for the many small
losers, and re-entries after an early exit pay charges again. `lock`,
`lock_arm_r`, `daylock` and `scale` in `portfolio_sim.Config` reproduce these.

## Daily kill levels, 16 Nov 2026 (V3 at Rs 60.6k, 3 months, hard close at the limit)

| daily kill | max loss per trade | 3-mo net | green days | worst day (sim, pessimistic) |
|---|---|---|---|---|
| 300 | 200 | +61 | 26/66 | -437 |
| 300 (live from 17 Nov) | 300 | +2,032 | 22/66 | -581 |
| 1000 | 400 | +4,907 | 30/66 | -1,173 |
| 2500 | 600 | +10,261 | 31/66 | -2,132 |

`hard_daily=True` closes every position at the bar's worst price once the
day's net reaches the limit (the live check runs every few seconds).

## Ratchet day lock, 17 Nov 2026 (live V3 at Rs 60.6k, kill 300, 3 months)

| day lock | 3-mo net | green days | max drawdown | Aug / Sep / Oct |
|---|---|---|---|---|
| none | +2,640 | 22/66 | 3,700 | -1,959 / +2,426 / +2,173 |
| single step 600 keep 60% | +432 | 22/66 | 4,238 | |
| ratchet 150:40,300:55,600:65,1000:75,2000:80 | +1,454 | 34/66 | 2,168 | +116 / +157 / +1,181 |
| same + bank the day at +1000 (live from 18 Nov) | +2,468 | 34/66 | 2,160 | +487 / +47 / +1,934 |

`daylock_steps` and `day_target` in `portfolio_sim.Config`. Checked at bar
closes in the sim; live it runs every few seconds, so it trips a little earlier.
