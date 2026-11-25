# 08 Journals, learnings and data sync

## Why (owner, 26 Nov)

Every strategy runs indefinitely on paper so that its trades and the AI's review of
them build a data set. Later: a base model trained on this data, a branch per
strategy with its own learnings, then fusion models combining strategies, with an
agentic AI allocating between them.

## Layout (data disk, never overwritten by a deploy)

```
/var/lib/indstocks/strategies/
  <strategy>/                       orb15, liq_sweep, vwap_trend@ai, ..., ai_trader
    logs/YYYY-MM-DD.md              every trade as it happens (journal.py)
    learnings/YYYY-MM-DD.md         the AI's review of the day (strategylearn.py)
```

**Log** (table, one row per event): time, event (open / close / entry order / stop
moved / exit / closed), stock, side, qty, price, stop, target, net after charges, note;
the header states the wallet and the day target; a "DAY TARGET reached" line when it is.
The bench rewrites a day's logs only when it replays that day after a restart.
The AI trader's log comes from its own events (`common.log` hook), results deduplicated.

**Learning** (16:10, one AI call per strategy that traded): a one-line headline, the
date, what happened (with numbers), 3-6 learnings tied to specific trades, and 1-3
changes to try as values of that strategy's own knobs. A strategy with no trades gets a
short "No trades" file. A day is reviewed once.

## Nightly sync to GitHub (`datasync.yml`, 00:00 IST)

Copies the data set into the repository's `data-sync` branch (never `main`, so it never
deploys). The repository is **public**, so it copies only paper data: strategies,
`auto/paper`, `auto/paperbroker`, `auto/pip.json`, `auto/company`, `learning` lessons and
journals, with the account ID scrubbed. Never the env file, tokens, push subscriptions,
the main event logs or the tape. Run by hand with
`gh api -X POST .../workflows/datasync.yml/dispatches -f ref=main`.

To keep everything (event logs, tape), move the sync to a private repository first.
