# Portfolio replay

`portfolio_sim.py` replays the live system on real 5-minute candles from Rs 26,000,
calling the live modules (playbook, trend scores, strategy book, sizing, risk checks,
charges). `run_versions.py` runs V1 (live), V2 and V3 over three months.

Data: run the `export-candles` workflow; it publishes `candles.json.gz` to the
`sim-data` branch. Then:

    git fetch origin +refs/heads/sim-data:refs/remotes/origin/sim-data
    git show origin/sim-data:candles.json.gz > indstocks/research/candles.json.gz
    cd research && python3 run_versions.py

Claude's live judgement and historical sentiment are not replayed.

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
