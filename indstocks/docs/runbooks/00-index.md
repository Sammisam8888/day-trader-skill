# Runbooks

Read in order. Each runbook says what a part of the system is, how it runs, and
what to do when it goes wrong. The history (09) records every decision and why.

| # | Runbook | What it covers |
|---|---|---|
| 01 | [Architecture](01-architecture.md) | Components, services and timers, where data lives |
| 02 | [Broker, login and market data](02-broker-login-data.md) | Shoonya OAuth auto-login, static IP, the websocket tape, holidays, NSE data |
| 03 | [Deploy, profiles and commits](03-deploy-profiles.md) | CI, one-time server profiles, the pitfalls that bit us, commit rules |
| 04 | [Operating the system](04-operations.md) | Diagnose, reading a day, the dashboard, common owner actions |
| 05 | [Risk and profit protection](05-risk-protection.md) | Stops, the trailing lock, the day lock, the daily kill, what is on now |
| 06 | [Paper trading and the PIP](06-paper-pip.md) | The paper broker, the 14-day Rs 400+ bar, the scorecard |
| 07 | [Bench, tuner and strategies](07-bench-strategies.md) | The 30+ paper models, knobs, the AI tuner, Rs 1 lakh wallets, liquidity models |
| 08 | [Journals, learnings and data sync](08-journals-learnings-sync.md) | Per-strategy logs and learnings on the data disk, the nightly GitHub sync |
| 09 | [History](09-history.md) | Day by day, 9 Nov to now: what happened, what changed, why |
| 10 | [Incident playbooks](10-incidents.md) | No trades, halted, login failed, tape down, deploy did not run, broken YAML |

Server: AWS Lightsail, `/opt/indstocks/day-trader-skill` (code, replaced on every
deploy), `/var/lib/indstocks` (data, never touched by a deploy), `/etc/indstocks.env`
(settings and credentials, root only). Dashboard on the tailnet only:
`https://ip-172-26-2-71.tailb7a0b4.ts.net` (also `http://100.127.19.72:8080`).
