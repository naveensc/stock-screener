# Stock Screener

Daily technical screener over S&P 500 + NASDAQ-100 + Russell 2000, delivered as an email digest. No UI, no server — runs on a GitHub Actions schedule. See [PLAN.md](PLAN.md) for the full design rationale.

## What it screens for

- **RSI** — oversold/overbought using each ticker's own rolling percentile rank (falls back to static 30/70 for tickers with insufficient history)
- **MA cross (50/200)** — golden cross / death cross
- **Volume spike** — 1.5x the trailing 20-day average
- **Bollinger Bands (20, 2)** — price at the upper or lower band

Each match adds +1 to that ticker's score; the email lists matches sorted by score descending.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
cp .env.example .env          # then fill in the values below
```

### Gmail App Password (for sending the digest)

1. Enable 2-Step Verification on the Google account: https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (any name, e.g. "stock-screener"), copy the 16-character password
4. Put your Gmail address in `EMAIL_SMTP_USER` and the app password in `EMAIL_SMTP_PASSWORD` in `.env`
5. Set `RECIPIENTS` to a comma-separated list of email addresses

### Run it locally

```bash
# from the repo root, with .env loaded into the environment
python src/main.py
```

### Rebuild the ticker universe (occasionally, not part of the daily run)

```bash
# from the repo root
python scripts/build_universe.py
```

S&P 500 is scraped live from Wikipedia. NASDAQ-100 comes from a manually maintained snapshot (`scripts/nasdaq100_snapshot.py`) since no reliable free scrape source was available — re-check it against https://www.nasdaq.com/market-activity/quotes/nasdaq-100-index-components a couple times a year (index reconstitution happens each December). Russell 2000 is sourced live from IWM's (iShares Russell 2000 ETF) public holdings feed, used as a free proxy for the index's constituent list.

## GitHub Actions setup

The workflow at `.github/workflows/screen.yml` runs daily at 21:15 UTC (4:15 PM EST, ~5:15 PM during daylight saving), Mon-Fri, shortly after market close. It needs these repo secrets (**Settings → Secrets and variables → Actions → New repository secret**):

| Secret | Value |
|---|---|
| `EMAIL_SMTP_USER` | Your Gmail address |
| `EMAIL_SMTP_PASSWORD` | The Gmail App Password from above |
| `RECIPIENTS` | Comma-separated recipient email addresses |

To trigger a run manually instead of waiting for the schedule: **Actions tab → Daily Stock Screen → Run workflow**.

## Project layout

```
stock-screener/
├── .github/workflows/screen.yml     # cron trigger
├── config/
│   ├── rules.yaml                   # thresholds
│   └── universe.csv                 # S&P 500 + NASDAQ-100 + Russell 2000 ticker list
├── scripts/
│   ├── build_universe.py            # regenerates config/universe.csv
│   └── nasdaq100_snapshot.py        # manually maintained NASDAQ-100 list
├── src/
│   ├── data.py                      # yfinance fetch (chunked, retried)
│   ├── indicators.py                # RSI / MA cross / volume / Bollinger calcs
│   ├── screen.py                    # apply rules, score, rank
│   ├── email_digest.py              # build + send email
│   └── main.py                      # orchestrates the run
├── requirements.txt
├── .env.example
└── PLAN.md
```
