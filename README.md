# data-sync
The paper-trading data set, copied nightly from the server (00:00 IST) by
.github/workflows/datasync.yml. Paper money only; the account ID is scrubbed.
- data/strategies/<strategy>/logs/YYYY-MM-DD.md: every trade, as it happened
- data/strategies/<strategy>/learnings/YYYY-MM-DD.md: the AI's review of the day
- data/auto/paper: the bench's ledgers and daily summaries
- data/auto/paperbroker: the AI trader's simulated orders and fills
- data/auto/pip.json: the PIP scorecard
- data/auto/company: company data from NSE filings
- data/learning: the AI trader's lessons and nightly journals
