# 09 History

Day by day: results (net after charges), what changed and why.

| Date | Result | What changed and why |
|---|---|---|
| 9 Nov | | Owner's profile: the AI decides, shorts allowed, aggressive, up to 5x |
| 12 Nov | | Shoonya adapter live; workflows for diagnose/research/export |
| 13 Nov | -74 | Three shorts +214 open at 12:21 ended +17: per-trade profit lock built (3R arm after back-test) |
| 16 Nov | -469 | Untested vwap_pullback trades: setups restricted to orb, failed_break, day_break. Flatten moved 15:05 -> 14:50 (Shoonya MIS cut at 15:00). Owner added Rs 35k (capital ~Rs 60k). Daily kill Rs 300 incl. costs, closes everything |
| 17 Nov | about -30 | +895 at the peak given back in the lunch fade. Day lock (600/60) then a ratchet day lock (150:40 ... 2000:80) and a +1000 target. Halt at 13:20 from a stale peak: resumed. 3-loser rule overridden for the afternoon. Tape recorder, momentum exit (shadow), Desk-style UI fixes, phone cards with Sell buttons. Research: 10-month back-test of the live strategy about -20,800 |
| 18 Nov | +52 | Paper bench of 11 models (later 22 with inverted twins); the AI reads the bench; tape extended to 34 stocks. Evidence: all textbook models lost over 20 days; the Rs 75 kill saved money on every model that day |
| 19 Nov | -214 (incl. manual) | Trailing lock (0.5R arm, 40/60/75/85%); stops only tighten. The AI cut its own risk to Rs 80 after one loss: undone by owner. Size floor 1.0 -> 0.25 so trades fit the loss room. Kill at 13:55 (manual trades included) froze the AI on a trend day. The bench instruction "breakouts failing = stand aside" corrected. **Owner withdrew funds; PIP on paper** (14 days of Rs 400+). Paper broker built; parameterised models with tested knob ranges; tuner (AI sets @ai knobs) |
| 20 Nov | -181 (paper) | First PIP day |
| 23 Nov | -585 (paper) | Owner removed the daily kill on paper; it would have stopped near -300. 4 of 5 AI trades were shorts on a non-falling day. Bench: vwap_trend@ai +630, several @ai copies positive. Owner asked to trade opposite to the AI: the mirror lost more (-674), kept as a daily comparison |
| 24 Nov | holiday | Desk tab (terminal-style live screen); company data from NSE filings (deals, results dates, shareholding) |
| 26 Nov | | Owner: every strategy gets a Rs 1 lakh paper wallet and a Rs 1,000 day target, no daily loss limit, no blockers. Two smart-money liquidity models (liq_sweep, liq_run) from the owner's videos. Per-strategy journals and nightly AI learnings on the data disk; nightly data sync to GitHub; these runbooks. Dashboard Strategies tab (pipeline health, every strategy, drill-down). F&O sleeves: a futures and an options copy of every model, Rs 1 lakh each |

## Decisions that stand

- Paper only until the PIP bar is met (owner, 19 Nov).
- No daily loss limit on paper (owner, 23 Nov); the day target banks the day (26 Nov).
- The AI decides; code enforces. The AI never loosens a protection.
- A strategy earns real money only on evidence: positive after charges over a long run,
  not one good season (the 3-month vs 10-month lesson of 17 Nov).
