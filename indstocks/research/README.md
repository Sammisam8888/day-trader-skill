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
