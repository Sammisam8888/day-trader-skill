# 01 Architecture

## The decision and the guards

The AI ("the AI" in code and docs: Claude, called through its CLI) decides; plain
code enforces every limit. The AI can never place an order that the code has not
checked, and it can never loosen a protection.

```
            tape.py (websocket, every tick)      sentiment / newswatch / companydata (NSE, RSS)
                       |                                       |
   paper.py bench  <---+---> brain.py (AI context) <-----------+
   (30+ rule models)          |
        ^                     v
   tuner.py (AI sets      autotrader.py (scan every 5 min + on shocks/news)
   the @ai knobs)             |  places orders (Shoonya, or the paper broker)
                              v
                     fillmonitor.py (every 4 s): protective stops, trailing profit lock,
                     day lock / target, kill switch, P&L, phone cards
                              |
                     flatten.py (14:50, 14:53, 14:57): everything flat before Shoonya's 15:00 MIS cut
                              |
              learning.py 15:50 (AI trader lessons) | pip.py (PIP scorecard)
              strategylearn.py 16:10 (per-strategy learnings) | companydata 08:40/16:30
```

## Services and timers (systemd, installed by deploy)

| Unit | When | Job |
|---|---|---|
| `login.timer` | 08:40, 08:55, 13:45 | Shoonya OAuth auto-login (skips holidays) |
| `tape.service` | always (active 09:05-15:35) | Websocket ticks + 5-level book, recorded and summarised per second |
| `auto.timer` | every 5 min in market hours | The AI's scan and order placement |
| `fillmonitor.service` | always | Stops, locks, kill, P&L, push cards |
| `flatten.timer` | 14:50, 14:53, 14:57 | Close everything |
| `paper.service` | always (09:15-15:05) | The paper bench (30+ models) |
| `tuner.timer` | 09:22, then :52/:22 to 14:22 | AI sets the @ai models' knobs |
| `newswatch.service` | always | NSE filings + RSS, AI-scored alerts |
| `sentiment.timer` | every 15 min | Market tilt, VIX, PCR, FII/DII, events |
| `company.timer` | 08:40, 16:30 | Bulk/block deals, results dates, shareholding (NSE) |
| `learning.timer` | 15:50 | PIP scorecard, then the AI trader's nightly review and lessons |
| `strategylearn.timer` | 16:10 | Per-strategy learnings |
| `dashboard.service` | always | Web UI on 127.0.0.1:8080, published on the tailnet by `tailscale serve` |
| `ops.timer`, `book.timer`, `watch.timer` | various | Calendar/ops checks, strategy book, watcher |

## Where data lives (`/var/lib/indstocks`, never overwritten by a deploy)

| Path | What |
|---|---|
| `auto/YYYY-MM-DD.jsonl` | The day's event log (every decision, order, fill, P&L tick) |
| `auto/tape/DAY/SYMBOL.jsonl` | Every tick and book update; `auto/tape/live.json` the per-second features |
| `auto/paper/` | Bench ledger (`DAY.jsonl`), summary, AI knob settings (`params-DAY.json`) |
| `auto/paperbroker/DAY.json` | The paper broker's orders and fills |
| `auto/pip.json` | PIP scorecard, net per day |
| `auto/company/latest.json` | Company data |
| `auto/lock/` | Profit-lock memory (survives a fill-monitor restart) |
| `strategies/<strategy>/logs/`, `/learnings/` | Per-strategy journals (runbook 08) |
| `learning/` | AI trader lessons and journals |
| `brain/` | Every AI call: prompt, reply, latency |
