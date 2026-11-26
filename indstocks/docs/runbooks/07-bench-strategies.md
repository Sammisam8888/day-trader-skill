# 07 Bench, tuner and strategies

## The paper bench (`paper.py`)

Rule-based models trading the tape's live prices with paper money, beside the AI
trader. No broker access at all (a test enforces it).

- **Wallet:** `TRADER_BENCH_CAPITAL` = Rs 1,00,000 per model (26 Nov), 5x; each trade
  sized so its stop loses `TRADER_PAPER_RISK` (Rs 500), capped by the model's free
  wallet (open notional <= wallet x 5).
- **Day target:** `TRADER_BENCH_TARGET` = Rs 1,000 is a milestone, not a stop (owner, 26 Nov:
  "it should be hungry to book more profits"). The first time a model's day net reaches it, the
  journal notes "DAY TARGET reached ... Keeps trading for more." and the model trades on. The same
  holds for its futures and options sleeves. `TRADER_TARGET_STOPS=true` brings back the old rule
  (close and done for the day). No loss limit, no trade cap (`TRADER_PAPER_UNCAPPED=true`);
  where a Rs 75 kill would have fired is recorded for study.
- Entries 09:30-14:35, flat 14:50; a restart replays the day from the tape recording.

## Models: recipes with knobs (`paper.RECIPES`, `paper.KNOBS`)

| Recipe | Idea |
|---|---|
| orb | Opening-range breakout (`minutes` 5-30) |
| vwap_revert / vwap_trend | Fade a VWAP stretch / ride closes beyond VWAP |
| ema_cross | 5-minute EMA fast/slow cross |
| rsi2 | RSI(2) extremes with the 20-EMA |
| bollinger_fade, squeeze_break | Band fade / Bollinger-in-Keltner breakout |
| volume_burst | Follow a big-volume, big-body minute |
| opening_drive_fade | Fade a large first-30-minute move |
| **liq_sweep** (26 Nov) | Smart-money liquidity sweep: price runs through a pool of equal lows/highs (where retail stops sit) and snaps back inside: trade the reversal |
| **liq_run** (26 Nov) | Same pools; the break holds: ride the stop-run |

Every recipe also has `enabled`, `invert`, `side`. Copies that run every day: fixed
defaults (`orb5`, `orb15`, `orb30`, `vwap_revert`, ...), inverted twins (`*_inv`), and
the AI-tuned copy of each recipe (`*@ai`). Knob values are clamped to tested ranges.

## F&O sleeves (`fno.py`, 26 Nov)

Every model's stock signal is also traded, side by side, in two sleeves with their own
Rs 1,00,000 paper wallets (`TRADER_FNO_CAPITAL`): `<model>~fut` (1 lot of the stock's
near-month future, long or short) and `<model>~opt` (the at-the-money call bought on a BUY
signal, the put on a SELL; up to `TRADER_FNO_OPT_BUDGET` Rs 20,000 of premium). They open
and close with the stock trade, use live NFO quotes (bid/ask), Shoonya's daily NFO
master for contracts (rolled when expiry is within 2 days), and F&O charges at the April
2026 STT (0.05% futures sell, 0.15% option premium sold). A futures lot whose margin (20%
of the lot value) exceeds the free wallet is skipped and logged: one lot of several large
stocks (RELIANCE: about Rs 1.3 lakh) does not fit Rs 1 lakh. The book persists in
`auto/fno/DAY.json`; F&O trades cannot be replayed from the equity tape, so after a
restart the sleeves continue from that file.

## The tuner (`tuner.py`)

At 09:22 and every 30 min to 14:22 the AI reads the tape, breadth, sectors, sentiment
and the bench, and sets the @ai knobs (writes `auto/paper/params-DAY.json`, history in
`.jsonl`). A bad reply keeps the previous settings. The scorecard of the idea is @ai vs
fixed on the bench.

## The AI agent (`agent.py`, book `ai_agent`)

Owner, 26 Nov: one more Rs 1 lakh paper wallet run by the AI as a manager of the other
strategies. It has no recipe of its own. At 09:42 and every 15 minutes to 14:27
(`agent.timer`) the AI reads the bench (today, the last hour, the last days), the
index tape, breadth, sentiment and its own book, and writes a decision to
`auto/paper/agent-DAY.jsonl`:

- `follow`: up to 8 models; when one of them opens a trade, the agent opens the same
  trade (stock, side, stop, target) in its own wallet, sized so the stop loses
  `TRADER_AGENT_RISK` (default `TRADER_PAPER_RISK`, Rs 500) x a risk multiplier
  0.25..2, with a side filter (both / long / short). One position per stock.
- `exit` (models) / `exit_all`: close its positions that followed those models, now.
- `stand_aside`: no new trades until the next decision.

The bench applies each decision from its timestamp, so a restart replays the agent's
day exactly. Its log shows every DECISION line and, per trade, which model it followed;
the dashboard's Strategies tab has an "AI agent" filter and a card of what it follows.
No loss limit, the Rs 1,000 target is a milestone like every other book.
`TRADER_AGENT=false` switches it off. The question it answers: does managing the bench
beat every single model after charges?

## Adding a strategy

1. Write `m_<name>(bars, state, p) -> ("B"|"S", stop, target) | None` in `paper.py`.
2. Add it to `RECIPES`, its knobs with ranges to `KNOBS`, a fixed copy to `MODELS`
   (the @ai copy is automatic), one line to the tuner's model list. The AI agent can
   follow it from the next decision on.
3. Test it in `tests/test_strategies.py`, run the 20-day replay, deploy. Its journal
   folder appears on the first trade.
