# 03 Deploy, profiles and commits

## Deploy (`.github/workflows/deploy.yml`)

1. **test** job: the whole suite (`python3 -m unittest discover -s tests`, ~790 tests).
2. **deploy** job: SSH to the box, `git reset` the code in `/opt/indstocks/day-trader-skill`,
   run the one-time **profiles**, install systemd units, restart the daemons
   (dashboard, fillmonitor, newswatch, tape, paper), `tailscale serve` for HTTPS.

Triggers: push to `main` **except** commits that only touch `.github/workflows/**` or
`indstocks/research/**` (so a workflow edit never restarts the fill monitor mid-session).
**A workflow-only change needs a manual run:**

```
gh api -X POST repos/Sammisam8888/day-trader-skill/actions/workflows/deploy.yml/dispatches -f ref=main
```

A deploy restarts the fill monitor and the tape. Avoid it in market hours unless the
change is needed now; the profit-lock memory is on disk, so a restart is safe.

## Profiles (one-time server changes)

A profile is a block in `deploy.yml` guarded by a sentinel file:

```
if [ ! -f /var/lib/indstocks/.profile-NAME ]; then
  for kv in KEY=VALUE ...; do k=${kv%%=*}; sudo sed -i "/^$k=/d" /etc/indstocks.env; echo "$kv" | sudo tee -a /etc/indstocks.env; done
  sudo touch /var/lib/indstocks/.profile-NAME
fi
```

It runs once, then never again, so the owner can still change a value by hand later.
To apply a new value, add a **new** profile with a new name.

Pitfalls that bit us:
- **YAML:** every line of a `run: |` block must stay indented. A multi-line
  `python3 -c "..."` whose lines start at column 1 breaks the whole workflow (19 Nov,
  24 Nov). Keep inline Python on one line, or put it in a module (`foo.py --flag`).
  Check with `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"`.
- **Order inside one deploy:** a profile that starts a service runs before the units
  are installed if it sits above the install step (24 Nov, company data). Put such
  steps after `systemctl enable`.
- Units must be added in two places: the `cp scripts/...` list and the `enable --now` list.

## Read-only server access: `diagnose.yml`

The only window into the box. It prints services, timers, settings (no secrets), the
day's events, P&L, the paper bench, the tape, the tuner, PIP, company data, login and
service journals. It must stay read-only: no orders, no writes, no arbitrary commands.

## Commits

- Author: `Sammisam8888 <samuelpriyatam@gmail.com>`; verify with
  `git log -1 --format='%an <%ae> | %cn <%ce>'`.
- No AI attribution trailers, and do not name the authoring tool (enforced by
  `.githooks/commit-msg`; `git config core.hooksPath .githooks`).
- ASCII only in files. Vocabulary: "the AI" / `ai` for the decision layer.
- Never change `LICENSE` line 3 or `tests/test_repo.py:94`; never rename the CLI-related
  lines listed in the handoff (ANTHROPIC_API_KEY, `claude -p`, model id, API host...).
- Never bind the dashboard to 0.0.0.0. No remote-command workflow.
