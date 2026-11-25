# 04 Operating the system

## A normal day

| Time (IST) | What happens |
|---|---|
| 08:40 | Auto-login; company data refresh |
| 09:05 | Tape connects |
| 09:15 | Bench starts; first AI scan after the opening range |
| 09:22 | Tuner sets the @ai knobs (then every 30 min) |
| 12:00-13:00 | No new AI entries (lunch chop) |
| 14:35 | Last AI entry |
| 14:50 | Flatten (and 14:53, 14:57 as backups); bench flat |
| 15:50 | PIP scorecard (phone note), AI trader nightly review and lessons |
| 16:10 | Per-strategy learnings |
| 16:30 | Company data refresh |
| 00:00 | Data sync to GitHub (`data-sync` branch) |

## Checking the system

Run diagnose (`gh api -X POST .../workflows/diagnose.yml/dispatches -f ref=main`), then
read: `"event": "pnl"` (latest net), `placed` / `trade_result` (trades), `day_mode`
(`halted` must be null to trade), `brain_view` (why the AI did or did not trade), the
`paper total` table, `== tape` (connected?), `== pip`, `== login journal`.

## The dashboard (tailnet only)

Tabs: Overview, **Desk** (live terminal: 34-stock monitor, book pressure, sectors,
breadth, news, company data, deals), Market (sentiment, Control card), AI (every
call's prompt and reply), **Paper** (every bench model, the AI's knob settings, every
paper trade), Orders, Activity, Chat. Phone notifications need the HTTPS address.

## Owner actions

| Want | How |
|---|---|
| Lift a halt | Market tab, Control card, Resume (owner outranks the AI) |
| Undo an AI risk change | Control card history, Undo; or a one-time profile calling `control.undo` |
| Close everything now | Halt button (flattens), or the phone card's Exit all |
| Sell one position | Phone card Sell button, or the position's exit button |
| "No trades and no halt" | Read the latest `brain_view`: usually no setup, the loss room, or the size floor |
| Change a setting | A new one-time profile in deploy.yml (runbook 03) |
