# 05 Risk and profit protection

All of these are code in `fillmonitor.py` / `profitlock.py` / `risk.py`, checked every
few seconds, and the AI cannot override them.

## Per trade

- **Protective stop at the exchange** for every fill (SL-LMT). A stop whose limit is
  skipped by a gap is re-priced to marketable ("stop chased").
- **Max loss per trade** `TRADER_MAX_LOSS` (now Rs 1,000 for the Rs 1 lakh paper wallet).
- **Trailing profit lock** (19 Nov): arms at `TRADER_LOCK_ARM_R` = 0.5R net of charges
  (min `TRADER_LOCK_ARM_RS`); the exchange stop moves past breakeven and then trails a
  rising share of the trade's best net (`TRADER_LOCK_STEPS` = 0.5R:40%, 1R:60%, 2R:75%,
  3R:85%). Below the floor: cancel the stop, marketable exit.
- **Stops only tighten; targets only move closer.** The AI's `adjust` is refused otherwise.
- **Momentum exit** from the tape (`TRADER_MOMENTUM_EXIT`): shadow (log only).

## Per day

| Guard | Setting | Now (PIP, from 26 Nov) |
|---|---|---|
| Daily loss stop (close all, halt) | `TRADER_DAILY_LOSS` | **Off** (100000); where Rs 300 would have fired is logged (`daily_loss_would_stop`) |
| Day target (a milestone: logged as `day_target_reached`, trading continues; `TRADER_TARGET_STOPS=true` closes all and halts) | `TRADER_DAY_TARGET` | **Rs 1,000** |
| Day lock ratchet (give back a share of the best) | `TRADER_DAYLOCK_STEPS` / `_ARM_RS` | **Off** (owner: only the target banks a day) |
| Stop after N losers / flip | `TRADER_STOP_AFTER_LOSSES`, `TRADER_FLIP` | **Off** |
| No-entry window | `TRADER_NO_ENTRY_WINDOWS` | 12:00-13:00 |
| The AI's own risk control | `control.set_risk_today` | On: it may lower size or limits, never raise them past the owner's ceilings |

## What the evidence said (so these choices can be revisited with data)

- 10-month back-test (Feb-Nov 2026) of the AI trader's setups: about -Rs 20,800; gross
  negative even before charges. Earlier per-trade locks did not change that.
- Charges are about Rs 47 per Rs 1 lakh round trip; on 17 Nov charges were 72% of gross.
- The Rs 300 kill: on 23 Nov the day passed -300 and ended -585 without it.
- Real money only comes back after the PIP (runbook 06). Before that, add a wide
  backstop (for example Rs 1,500) even if the paper run passes without one.
