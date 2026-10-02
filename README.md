# Insider Moves

A daily screen of SEC Form 4 filings. It keeps open-market buys (code P) and sales (code S), drops planned 10b5-1 sales unless they cut a large share of holdings, and scores each insider's past timing: how the stock did over the 6 months after each earlier buy or sale, versus the S&P 500.

Output: a page on GitHub Pages, plus an optional email digest on days with new flags.

## Tuning

- `watchlist.txt`: tickers that get looser thresholds (one per line).
- `config.json`: dollar and percent thresholds for the whole-market scan and the watchlist.
  - `market.buy_min_value`: smallest open-market buy to flag (default $100K).
  - `market.discretionary_sell_min_value`: smallest sale by choice to flag ($1M), or `discretionary_sell_min_pct` of holdings (10%) if at least $250K.
  - `market.plan_sell_min_pct`: planned 10b5-1 sales show only above this share of holdings (25%).
  - `market.include_institutional_owners`: set `true` to include funds and companies that are 10% owners (off by default).

## Secrets (repo Settings > Secrets and variables > Actions)

| Secret | What |
|---|---|
| `SEC_USER_AGENT` | Required by the SEC: `Your Name you@example.com` |
| `GMAIL_USER` | Gmail address that sends the digest (optional) |
| `GMAIL_APP_PASSWORD` | A Gmail app password, not your real password (optional) |
| `DIGEST_TO` | Where to send it; defaults to `GMAIL_USER` (optional) |

## Running

Runs at 7:15am ET Tuesday to Saturday. To backfill: Actions > screen > Run workflow, with a start date.

Locally: `SEC_USER_AGENT="Name you@example.com" python3 screener.py [YYYY-MM-DD]`

## Notes

- Foreign issuers (ASML, Tokyo Electron) don't file Form 4s.
- Holdings counts exclude unvested options, so "% of holdings" can overstate how much of an executive's total stake was sold.
- If a filed price is more than 3x off that day's close (a typo in the filing), the trade is re-priced and labeled.
- Prices come from Yahoo Finance's public chart endpoint; if it's unavailable, flags still appear without price context.
