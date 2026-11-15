# Stock Screener

Daily screener over the $10B+ names in the S&P 500 + NASDAQ-100 + Russell 2000 (~490 tickers). Every weekday after the close a GitHub Action scores each stock, publishes a card per stock to GitHub Pages, and emails a summary in which every ticker links to its card. See [PLAN.md](PLAN.md) for the original design rationale.

## The daily scores

Each stock gets three scores, each a **0–100 percentile across that day's universe** (80 = better than 80% of the universe), and a composite:

| Score | Built from |
|---|---|
| **Quality** | Cash flow quality, capital efficiency and financial strength (`src/valuation.py`) |
| **Valuation** | Multiples vs same-industry peers and its own history, plus base-case DCF upside — re-priced daily |
| **Technical** | Trend (50/200-day averages + slope), 12-1 month momentum, 6-month strength vs sector, RSI pullback, up/down volume (`src/technical.py`); a 5-day average so one session can't swing it |
| **Composite** | 40% Quality + 30% Valuation + 30% Technical (`config/rules.yaml`) |

The email shows the top 10 by composite, **movers** (composite change of 5+ points since the previous run, with the score that drove it), **state changes** (life-cycle phase, DCF verdict, crossing the 200-day average, entering/leaving the top 20) and one-day technical alerts (RSI extremes, golden/death cross, Bollinger touches, volume spikes — context only, they don't move the scores). Valuation is relative; the DCF verdict is absolute — the email says when they disagree.

Scores are appended daily to `data/score_history.csv`, which drives the movers and each card's score chart — and, after a few months, lets the weights be tested against what the stocks actually did. Fundamentals are cached and refreshed for ~1/5 of the universe each weekday (every stock at least weekly); prices are fresh every day.

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
# from the repo root: builds site/, email.html and appends to data/score_history.csv
python src/main.py
python src/main.py --tickers my_list.txt   # a smaller universe for testing
python src/main.py --backfill 5            # also score the previous 5 trading days (first run)

# send the built email (needs the .env values above)
python src/email_digest.py email.html
```

Locally the email's links point at `http://localhost:8765`; set `SITE_URL` to point them elsewhere. To browse the site: `python -m http.server 8765 --directory site`.

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

To trigger a run manually instead of waiting for the schedule: **Actions tab → Daily Stock Screen → Run workflow**. Set **backfill** to `5` on the very first run so 5-day movers work straight away (backfilled days reuse current fundamentals with that day's prices).

**GitHub Pages (one-time):** the stock cards are published to `https://<owner>.github.io/<repo>/`. Enable it under **Settings → Pages → Source: GitHub Actions**. On a free plan Pages requires a public repository, and the published site is readable by anyone with the link (it contains only public market data). The workflow also commits `data/score_history.csv` back to the repo each day.

## Life-cycle phase screener

A separate, on-demand tool (`src/lifecycle.py`) that classifies each company into a business life-cycle phase — 1 Startup, 2 Hyper Growth, 3 Operating Leverage, 4 Capital Return, 5 Decline — from three questions: is revenue growing, is it profitable (operating cash flow if asset-light, net income otherwise), and is it returning capital (buybacks + dividends, last twelve months). It also re-runs each company as of its previous fiscal year and flags phase changes.

Each company is also labeled **Asset-light** or **Asset-heavy** by majority vote of five signals: PP&E / total assets, capex / revenue, depreciation / revenue (stand-in for maintenance cost), operating margin, and ROIC. That label is what picks the profit metric above. Companies with 2–3 of 5 light signals are labeled **Hybrid** and valued with both frameworks.

Each stock card also carries a **valuation & quality analysis** (`src/valuation.py`), computed from the numbers with generated reasoning: five-year growth, margins, returns and balance-sheet trends; valuation multiples chosen by asset model; relative valuation against same-industry peers in the screened universe and the company's own fiscal-year-end history; a bear/base/bull DCF (analyst revenue estimates, CAPM WACC from the 10-year Treasury yield) with cross-checks; capital allocation; rule-based red flags; and a 100-point score (relative valuation 30%, cash flow quality 25%, capital efficiency 25%, financial strength 20%). It's model output, not investment advice: no buy/sell ratings or entry prices, and no qualitative judgments (moat, management) the statements can't support. Valuation thresholds live in the config block at the top of `src/valuation.py`. Peer comparisons get more reliable on larger universes.

```bash
python src/lifecycle.py --demo                          # synthetic data, no network
python src/lifecycle.py --universe tickers.txt          # one ticker per line
python src/lifecycle.py --universe sp500 --phase 3      # or nasdaq100; --phase filters the report
python src/lifecycle.py --universe sp500 --refresh      # ignore the 24h cache in .cache/lifecycle/
```

Writes `lifecycle_report/` (change with `--out`): `screener.html` (sortable/filterable table, phase changes at the top), `cards/TICKER.html` (one-page card per company), `results.csv`, and `errors.csv` (tickers skipped for missing data). Thresholds live in the config block at the top of the file. Tests: `python -m pytest tests/`.

## Project layout

```
stock-screener/
├── .github/workflows/screen.yml     # daily run: score, publish to Pages, email
├── config/
│   ├── rules.yaml                   # score weights, alert thresholds, mover settings
│   └── universe.csv                 # S&P 500 + NASDAQ-100 + Russell 2000 ticker list
├── data/
│   └── score_history.csv            # one row per ticker per run (committed by the workflow)
├── scripts/
│   ├── build_universe.py            # regenerates config/universe.csv
│   └── nasdaq100_snapshot.py        # manually maintained NASDAQ-100 list
├── src/
│   ├── main.py                      # daily run: scores, site, email.html
│   ├── scores.py                    # percentile ranks, composite, history, movers, state changes
│   ├── technical.py                 # continuous technical factors
│   ├── email_digest.py              # build + send email
│   ├── data.py                      # yfinance price fetch (chunked, retried)
│   ├── indicators.py                # RSI / MA / volume / Bollinger calcs
│   ├── screen.py                    # one-day technical alert tags
│   ├── fundamentals.py              # SEC EDGAR market caps for build_universe.py
│   ├── lifecycle.py                 # life-cycle phases, stock cards (also a standalone CLI)
│   ├── valuation.py                 # valuation & quality analysis for the cards
│   └── statements.py                # shared helpers for reading Yahoo statements
├── tests/
│   ├── test_lifecycle.py            # phase + valuation tests (pytest)
│   └── test_daily.py                # technical score, composite, movers, email
├── requirements.txt
├── .env.example
└── PLAN.md
```
