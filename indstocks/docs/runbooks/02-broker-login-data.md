# 02 Broker, login and market data

## Shoonya (Finvasia), account FN245398

- **OAuth only.** `autologin.py` opens the authorize URL, logs in with the stored
  credentials and TOTP, exchanges the code (`GenAcsTok`, checksum
  `sha256(client_id + secret + code)`). The response carries `susertoken` (REST) and
  `access_token` (websocket); both are saved to `auto/shoonya_token.json` (0600).
  Valid until the nightly settlement (~23:30).
- **Never retry a rejected password automatically** (account lock risk). A failed
  login notifies the owner; the dashboard has a "Log in to Shoonya" button.
- **Static IP.** Every API call must come from the IP registered on the API key page
  (the Lightsail box's static IP).
- **Orders:** LMT and SL-LMT only (MKT is rejected), so every "market" exit is a
  marketable limit priced through the touch (`marketable()`, 0.3%). Product `I` = MIS.
  No bracket/OCO: the stop rests at the exchange, the target is watched by the fill
  monitor. SEBI algo ID from `SHOONYA_ALGO_ID`.
- **MIS square-off for F&O stocks is 15:00** (since NSE's closing auction, 3 Aug
  2026): orders after it are rejected. Our flatten is 14:50 (`TRADER_FLAT_BY`), last
  entry 14:35.
- Credentials live only in `/etc/indstocks.env` (root, 0600). Never paste the
  password, TOTP secret, MPIN or API keys anywhere.

## Holidays

`login.service` skips non-trading days ("not a trading day" in its journal); no token
means no tape and no trading that day. Example: 24 Nov 2026 (Guru Nanak Jayanti).

## The tape (`tape.py`)

- Websocket `wss://api.shoonya.com/NorenWSAPI/`, login message
  `{"t":"a","uid","actid","accesstoken","source":"API"}`, ping every 3 s.
- Subscribes the 5-level book (`t=d`) for `TRADER_SYMBOLS` + `TRADER_TAPE_EXTRA`
  (34 stocks; the AI trades only `TRADER_SYMBOLS`).
- **One websocket per login:** a second connection drops the first. Never run
  `tape.py --check` in market hours (diagnose skips it 09:00-15:40).
- Per-second features: 1m/5m change, volume vs the day's average minute, upticks,
  book imbalance, spread, momentum (-1..1).

## NSE data (public site, light polling)

`sentiment.nse_json(api)`: `allIndices` (breadth, sectors), `fiidiiTradeReact`,
`corporate-announcements`, event calendar, `snapshot-capital-market-largedeal`,
`corporates-financial-results`, `corporate-share-holdings-master`.

Not used, and why: SEC EDGAR / WhaleWisdom (US only), Yahoo Finance (Shoonya is
better for our stocks), Screener.in and Trendlyne (their terms bar copying their data
into another database; owner reads them directly).

## Settings file pitfall

`/etc/indstocks.env` is read by systemd, by Python (`common.setting`) and by shell
scripts with `. /etc/indstocks.env`. Values with shell metacharacters break the shell
case (23 Nov: `M&M` in `TRADER_TAPE_EXTRA`). Keep values plain.
