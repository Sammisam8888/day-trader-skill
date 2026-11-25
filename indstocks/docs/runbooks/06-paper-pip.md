# 06 Paper trading and the PIP

## Why (19 Nov)

After losses on most live days, the owner withdrew the money and put the AI on a
performance improvement plan: **no real money until 14 consecutive trading days each
net at least Rs 400 after charges** (`TRADER_PIP_DAYS`, `TRADER_PIP_DAILY_MIN`). On that
day the owner deploys Rs 50,000 again.

## The paper broker (`paperbroker.py`)

`TRADER_BROKER=paper` makes `shoonya.Shoonya()` return the paper broker; the whole
pipeline (AI scans, stops, trailing lock, target, flatten, learning) runs unchanged.

- Prices: live Shoonya data (read-only session, `Shoonya(data_only=True)`).
- Fills: from the tape since the order was placed. Buys fill at the ask, sells at the
  bid; a stop triggers on the tick that crosses it; a gap past the limit leaves it
  resting (the fill monitor chases it, as live).
- Margin: `TRADER_PAPER_CAPITAL` (Rs 1,00,000 from 26 Nov), MIS margin 20%; over it the
  order is REJECTED, exits always allowed.
- Charges: the pipeline's own P&L uses `charges.py` (Shoonya's real intraday charges).
- **Real orders are impossible:** `Shoonya.place/modify/cancel` raise in paper mode on
  any object. Diagnose prints `broker: PaperBroker | paper_mode: True`.

## The scorecard (`pip.py`, 15:50)

Records the day's last `pnl` net into `auto/pip.json`, computes the streak (a red,
under-400 or no-trade day resets it), sends a phone note, shows on the dashboard's
Agent tile ("PIP streak X/14").

## Going live again (when the bar is met)

1. Owner deposits; 2. new profile: `TRADER_BROKER=live` (remove the line) and the
real capital; 3. a wide daily backstop; 4. start at reduced size for a week.
