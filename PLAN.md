# Stock Screener — Design & Plan

Document-only stage. No code has been written yet. All design decisions are now finalized (see §3 and §4) — this is ready to move into implementation planning.

---

## 1. Goal

A free, no-UI stock screener that runs on a schedule (GitHub Actions), filters/ranks a universe of stocks against a set of technical criteria (RSI, moving-average cross, volume, Bollinger Bands), and emails a digest of matches sorted by score — the highest-scoring tickers are the ones meant to be actioned on. No server, no database, no dashboard — GitHub Actions is both the scheduler and the compute.

---

## 2. Architecture

```
GitHub Actions (cron, scheduled)
        │
        ▼
Python script: fetch universe → compute indicators → apply filters → rank → build email → send SMTP
        │
        ▼
Email digest (Gmail SMTP, same pattern as the AP Business Scanner project:
             EMAIL_SMTP_USER / EMAIL_SMTP_PASSWORD / RECIPIENTS env vars)
```

Stateless per run unless day-over-day tracking is added later (see Open Decisions, §7).

---

## 3. Decisions Made

| Area | Decision |
|---|---|
| Core feature | **Screener** — filter/rank a universe of stocks by criteria. No charting UI. |
| Project location | Standalone project at `D:\projects\stock-screener` (separate from AP Business Scanner). |
| Data source | **Free/delayed** — `yfinance` (Yahoo Finance). No paid API key required. |
| Delivery mechanism | **GitHub Actions** cron job. No UI. Output is an **email**. |
| Universe | **S&P 500 + NASDAQ-100** (combined, deduplicated ticker list). |
| RSI methodology | **Option B — rolling percentile rank ("relative RSI")**, see §4. Phase 2 will move to **Option D — swing-reversal calibration** (deriving each stock's empirical "reversal RSI" from its own historical swing highs/lows). Rejected for now: Option A (static per-run quantile snapshot) and Option C (Bollinger Bands on RSI) — both plausible but B was chosen as the adaptive-yet-simple starting point. |
| Schedule/cadence | **Daily email, shortly after market close.** Cron: `15 21 * * 1-5` (21:15 UTC = 4:15 PM EST, Mon–Fri). Note: GitHub Actions cron is fixed UTC and doesn't auto-adjust for US DST, so during daylight saving months (EDT, UTC-4) this same cron fires at ~5:15 PM ET instead of 4:15 PM — treated as a minor, acceptable cosmetic drift rather than something to engineer around. |
| Day-over-day tracking | **Fresh re-screen every run** — no dedup/state persistence. Chosen because cost/time impact is negligible (see §7.1 for reasoning); no AI/LLM calls exist anywhere in this design, and the `yfinance` pull for the full universe is small (~600 tickers × ~1yr daily OHLCV, a few MB) and fast (~1–5 min via batched/threaded requests) — well within GitHub Actions free-tier minutes. |
| MA cross window | **50/200** ("golden cross" / "death cross") — rare, high-conviction major trend-change signal, chosen over the faster/noisier 20/50 pair. See §4.2. |
| Bollinger Bands filter | **Standalone signal**, added alongside RSI/MA-cross/volume — flags when closing price is at/beyond the upper or lower band. Uses the same 20-day/2-stdev basis already used in the user's own charting tool. Contributes its own point to the score independently (not conditioned on RSI agreement). See §4.4. |
| Volume spike threshold | **1.5× trailing 20-day average volume.** See §4.3. |
| RSI lookback window | **252 trading days (~1yr).** See §4.1. |
| Insufficient-history fallback | Tickers without enough price history for the RSI percentile calc **fall back to static global 30/70 RSI** for that run, rather than being skipped. See §4.1. |

---

## 4. Screening Criteria — Detail

### 4.1 RSI — Option B: Rolling Percentile Rank

Rather than a fixed global 30/70 oversold/overbought threshold (which doesn't fit every stock — e.g. TSLA has historically reversed near RSI ~41, not 30), each stock's "oversold"/"overbought" zone is derived from **its own RSI history**:

1. Compute the standard RSI(14) daily series for the stock.
2. Over a trailing lookback window (**252 trading days / ~1 year, confirmed**), take the distribution of that stock's own past RSI values.
3. Compute the **percentile rank** of today's RSI value within that trailing distribution (i.e. what fraction of days in the lookback window had RSI ≤ today's value).
4. Flag:
   - **Oversold** when percentile rank ≤ a low cutoff (default **10th percentile**)
   - **Overbought** when percentile rank ≥ a high cutoff (default **90th percentile**)

This means each ticker effectively gets its own personalized RSI bands, recalculated on every run as new price data comes in. A stock needs at least `lookback_days + 14` days of price history to qualify; tickers with insufficient history (e.g. recent IPOs) **fall back to the static global 30/70 RSI threshold** for that run instead of being skipped.

**Phase 2 (Option D):** replace or augment this with a calibration derived from actual historical swing lows/highs — find local price reversals, record the RSI value at each, and use the empirical median/percentile of *those* values as the stock's "reversal RSI." More precise but requires swing-detection logic and enough historical reversals per ticker to be statistically meaningful.

### 4.2 Moving Average Cross — 50/200

Track a fast 50-day MA against a slow 200-day MA:
- **Golden cross** — 50-day crosses above the 200-day → bullish trend-change signal.
- **Death cross** — 50-day crosses below the 200-day → bearish trend-change signal.

Chosen over a faster pair like 20/50 because it fires rarely (a handful of times a year per stock) but with high conviction — by the time it triggers, a real, sustained trend shift is already underway. This keeps the email quiet most days and only flags genuinely major trend changes, rather than the more frequent but noisier/whipsaw-prone signals a faster pair would produce.

### 4.3 Volume
Flag a **volume spike** when current volume exceeds **1.5× its trailing 20-day average** (confirmed).

### 4.4 Bollinger Bands — standalone signal

Same basis as the Bollinger Bands already shown in the user's own charting tool: **20-day SMA** as the middle band, **±2 standard deviations** for the upper/lower bands.

- **At upper band** — closing price ≥ upper band → stretched/overbought (mean-reversion framing, same idea as RSI overbought).
- **At lower band** — closing price ≤ lower band → stretched/oversold (mean-reversion framing, same idea as RSI oversold).

This is a **standalone** criterion — it adds its own point to a ticker's score independently, regardless of what RSI or the MA cross say for that same ticker (not a confluence-only signal gated on RSI agreement).

---

## 5. Scoring / Ranking

Each ticker is scored by how many rules it matches (e.g. RSI oversold = +1, golden/death cross = +1, volume spike = +1, at upper/lower Bollinger Band = +1) — each criterion contributes independently, no confluence requirement between them. The email is sorted by score descending, with the highest-scoring tickers surfaced as the ones to action — same pattern as the keyword-scoring approach used in the AP Business Scanner project, applied to technicals instead of text.

---

## 6. Email Format

- Plain-text or simple HTML table: ticker, price, matched signals, score.
- Sent only if at least one match exists that day — stays silent otherwise (no empty-email spam).
- Reuses the `EMAIL_SMTP_USER` / `EMAIL_SMTP_PASSWORD` / `RECIPIENTS` env-var pattern from the AP Business Scanner project, stored as GitHub Actions repo secrets.

---

## 7. Open Decisions

None remaining — all decisions resolved. See §3 and §4 for the finalized settings.

### 7.1 Day-over-day dedup vs. fresh re-screen — resolved (fresh re-screen chosen)

**Decision:** fresh re-screen every run, no dedup state. Rationale: no AI/LLM calls exist anywhere in this design (unlike the AP Business Scanner's Gemini/Claude layer), so there's no per-call AI cost to worry about either way. The `yfinance` pull itself is free (no API key/billing), small (~600 tickers × ~1yr daily OHLCV, a few MB), and fast (~1–5 min via batched/threaded requests) — negligible against GitHub Actions' free-tier minutes. The main practical risk isn't cost but reliability: `yfinance` is an unofficial Yahoo Finance scraper, not a supported API, and can be rate-limited/blocked in bursts — more common from shared cloud IPs (GitHub Actions runners) than a home connection. Mitigate with request batching/chunking (e.g. 50–100 tickers per call) and basic retry-with-backoff from the start; keep a fallback free source (e.g. Stooq) in mind only if Yahoo proves consistently flaky in practice.

Original trade-off analysis kept below for reference:

**Fresh re-screen every run:** the script screens the whole universe from scratch each day and emails whatever currently matches, with no memory of prior runs.
- Pro: simple, no state to store — fits naturally with ephemeral GitHub Actions runners.
- Con: a stock that stays oversold for a week shows up in the email every single day, identical entry, until it stops matching — repetitive and harder to spot what's actually new.

**Day-over-day dedup:** the script remembers what it already alerted on (per ticker + signal type) and only re-emails a ticker when something changes — e.g. it newly enters oversold territory rather than every day it remains there. Same philosophy as the AP Business Scanner's seen-database (never re-send an already-reported RFP).
- Pro: email only shows genuinely new signals — far less noise.
- Con: needs state to persist *across* runs, which GitHub Actions runners don't retain by default. Options to store it:
  - Commit a small state file (JSON/SQLite) back into the repo at the end of each run — simplest, no extra infra, but adds one bot commit per day.
  - GitHub Actions cache — simpler than a commit, but not guaranteed durable long-term (can be evicted).
  - External store (S3, gist, hosted DB) — most robust, but reintroduces a "server-like" dependency the project is otherwise avoiding.

A dedup approach effectively means "only re-alert on a transition" (e.g. re-notify if a stock leaves oversold and later re-enters) rather than muting a ticker forever once seen.

**Recommendation:** commit-the-state-file-to-the-repo, since it avoids external infra at the cost of a small daily auto-commit. Still awaiting the user's decision between dedup vs. fresh re-screen.

---

## 8. Proposed Project Structure (for reference — not yet created)

```
stock-screener/
├── .github/workflows/screen.yml     # cron trigger
├── config/
│   ├── rules.yaml                   # thresholds (RSI percentiles, MA windows, volume multiple, Bollinger Band params)
│   └── universe.csv                 # S&P 500 + NASDAQ-100 ticker list
├── src/
│   ├── data.py                      # yfinance fetch
│   ├── indicators.py                # RSI / MA / volume calcs
│   ├── screen.py                    # apply rules, score, rank
│   ├── email_digest.py              # build + send email
│   └── main.py                      # orchestrates the run
├── requirements.txt
├── .env.example
└── README.md
```
