# 10 Incident playbooks

Each: the symptom, how to confirm it (diagnose), the fix.

## No trades, nothing halted
Confirm: `day_mode` has `"halted": null`; read the latest `brain_view`.
Causes seen: no setup that passes the rules; the loss room left is smaller than a
full-size trade (19 Nov: size floor 1.0 with Rs 121 room); the AI cut its own risk
(`control` event `by: ai`); the no-entry window (12:00-13:00); past 14:35.
Fix: depends on the cause; undo the AI's risk change (runbook 04), lower
`TRADER_SIZE_FLOOR`, or accept that the AI stood aside.

## Halted
Confirm: `"halted": "..."` or `halted for today by ...`. Lift: Control card, Resume.
A halt from a stale day-lock peak after a mid-session restart (17 Nov) is fixed: the
lock memory is keyed per day and loaded from disk.

## Login failed / "no login token yet today"
Confirm: `== login journal`. "not a trading day" = holiday (nothing to do). A rejected
login: do not retry automatically; log in from the dashboard button.

## Tape down
Confirm: `== tape` shows `tape_error`; Desk shows 0 stocks in market hours.
Causes: no login token; a second websocket login (never run `tape.py --check` in market
hours); network. It reconnects by itself with backoff.

## A change did not deploy
Workflow-only commits do not deploy (runbook 03): dispatch `deploy.yml`. A 422
"Workflow does not have 'workflow_dispatch' trigger" means the YAML is broken: validate
it locally, fix, push again.

## A one-time profile did nothing
Its sentinel is already there (it ran before, maybe too early in the deploy). Add a new
profile with a new sentinel name, after the unit install if it starts a service.

## Server unreachable
Lightsail console: check the instance; the dashboard is tailnet-only. Positions are
protected by exchange stops even if the box is down (and flat by Shoonya's 15:00 MIS
square-off at worst).
