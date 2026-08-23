"""Fetch company facts (for market cap + fundamentals) from SEC EDGAR's free XBRL API.

No API key, no cost — SEC EDGAR is the official source and covers virtually every
US-listed filer. `fetch_company_facts` does the one (rate-limited) network round-trip per
ticker; `extract_shares_outstanding` and `compute_fundamentals` are pure functions that both
derive their answer from that same payload, so market-cap filtering and fundamentals scoring
never require a second fetch.

Fundamentals-wise, the tradeoff is that EDGAR only has reported actuals (no analyst consensus
estimates), so there's no earnings-beat/miss detection here, only growth and margin trend off
the two most recent comparable quarters. Companies also tag the same concept inconsistently
(e.g. some use `Revenues`, others `RevenueFromContractWithCustomerExcludingAssessedTax`), so
each metric tries a fallback chain of tags. Tickers without a clean quarter-over-quarter-a-
year-ago match for every required tag come back as None — the downstream hard filter treats
missing fundamentals as "can't confirm quality" rather than a pass.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd
import requests

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
HEADERS = {"User-Agent": "stock-screener research naveensc@gmail.com"}

MAX_WORKERS = 8
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
PROGRESS_LOG_INTERVAL = 250  # this fetch can run silently for 5-10+ minutes otherwise, which
# looks like a hung job in CI — a heartbeat keeps the log visibly alive
REQUEST_MIN_INTERVAL_SECONDS = 0.2  # global pace across all workers — SEC's fair-use limit is ~10 req/sec


class _RateLimiter:
    """Paces requests to a fixed global interval regardless of how many threads call it —
    a per-thread sleep isn't enough, since N concurrent threads each sleeping the same delay
    still collectively fire at N times the intended rate.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._min_interval
        sleep_for = start - now
        if sleep_for > 0:
            time.sleep(sleep_for)


_rate_limiter = _RateLimiter(REQUEST_MIN_INTERVAL_SECONDS)

REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
SHARES_OUTSTANDING_DEI_TAGS = ["EntityCommonStockSharesOutstanding"]
SHARES_OUTSTANDING_GAAP_TAGS = ["CommonStockSharesOutstanding"]

MIN_QUARTER_DAYS = 80
MAX_QUARTER_DAYS = 100
YOY_TOLERANCE_DAYS = 20  # how close a quarter-end has to be to "~1 year earlier" to count


def load_ticker_to_cik() -> dict[str, int]:
    """Returns {ticker: cik} for every SEC filer."""
    resp = requests.get(TICKERS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return {row["ticker"]: row["cik_str"] for row in resp.json().values()}


def _quarterly_series(gaap: dict, tags: list[str]) -> list[dict]:
    """Returns discrete-quarter entries (~1 quarter long, not YTD/annual) for the first tag
    that has data, deduplicated by end-date (keeping the most recently filed value), sorted
    by end date ascending.
    """
    for tag in tags:
        concept = gaap.get(tag)
        if not concept:
            continue
        by_end = {}
        for unit_entries in concept["units"].values():
            for entry in unit_entries:
                start, end = entry.get("start"), entry.get("end")
                if start is None or end is None:
                    continue  # instant-in-time facts (no "start") can be mixed into the same array
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                if not (MIN_QUARTER_DAYS <= days <= MAX_QUARTER_DAYS):
                    continue
                existing = by_end.get(end)
                if existing is None or entry["filed"] > existing["filed"]:
                    by_end[end] = entry
        if by_end:
            return sorted(by_end.values(), key=lambda e: e["end"])
    return []


def _latest_instant(concept: dict) -> dict | None:
    """Returns the most-recently-filed instant-in-time entry for a concept (point-in-time
    facts like shares outstanding have no "start", unlike the duration facts above)."""
    best = None
    for unit_entries in concept["units"].values():
        for entry in unit_entries:
            if entry.get("start") is not None or entry.get("end") is None:
                continue
            if best is None or entry["filed"] > best["filed"]:
                best = entry
    return best


def extract_shares_outstanding(facts: dict) -> float | None:
    """Returns the most recently reported shares outstanding, or None if untagged."""
    dei = facts.get("facts", {}).get("dei", {})
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for source, tags in ((dei, SHARES_OUTSTANDING_DEI_TAGS), (gaap, SHARES_OUTSTANDING_GAAP_TAGS)):
        for tag in tags:
            concept = source.get(tag)
            if concept:
                entry = _latest_instant(concept)
                if entry:
                    return entry["val"]
    return None


def compute_market_caps(
    tickers: list[str],
    price_data: dict[str, pd.DataFrame],
    company_facts: dict[str, dict],
) -> dict[str, float]:
    """Returns {ticker: market_cap} (shares outstanding x latest close). Tickers without
    confirmable shares-outstanding or price data are omitted rather than assumed to be zero.
    """
    market_caps = {}
    for ticker in tickers:
        facts = company_facts.get(ticker)
        df = price_data.get(ticker)
        if facts is None or df is None or df.empty:
            continue
        shares = extract_shares_outstanding(facts)
        if shares is None:
            continue
        market_caps[ticker] = shares * df["Close"].iloc[-1]
    return market_caps


def _latest_and_yoy(gaap: dict, tags: list[str]) -> tuple[float, float] | None:
    """Returns (latest_quarter_value, year_ago_quarter_value), or None if there's no discrete
    latest quarter or no quarter ~1 year earlier to compare it against."""
    series = _quarterly_series(gaap, tags)
    if not series:
        return None

    latest = series[-1]
    latest_end = date.fromisoformat(latest["end"])
    target = latest_end - timedelta(days=365)

    prior, best_diff = None, None
    for entry in series[:-1]:
        diff = abs((date.fromisoformat(entry["end"]) - target).days)
        if diff <= YOY_TOLERANCE_DAYS and (best_diff is None or diff < best_diff):
            prior, best_diff = entry, diff

    if prior is None:
        return None
    return latest["val"], prior["val"]


def _margin_improved(gaap: dict, tags: list[str], revenue: tuple[float, float]) -> bool | None:
    latest_revenue, prior_revenue = revenue
    profit = _latest_and_yoy(gaap, tags)
    if profit is None:
        return None
    latest_profit, prior_profit = profit
    return (latest_profit / latest_revenue) > (prior_profit / prior_revenue)


def compute_fundamentals(facts: dict) -> dict | None:
    """Returns {revenue_growth, eps_growth, margin_improved} computed from a ticker's SEC XBRL
    facts, or None if the required tags/history aren't available."""
    gaap = facts.get("facts", {}).get("us-gaap", {})

    revenue = _latest_and_yoy(gaap, REVENUE_TAGS)
    eps = _latest_and_yoy(gaap, EPS_TAGS)
    if revenue is None or eps is None:
        return None

    latest_revenue, prior_revenue = revenue
    latest_eps, prior_eps = eps
    if prior_revenue <= 0 or latest_eps <= 0 or prior_eps <= 0:
        return None  # growth % is meaningless/undefined off a loss or non-positive base

    margin_improved = _margin_improved(gaap, GROSS_PROFIT_TAGS, revenue)
    if margin_improved is None:
        margin_improved = _margin_improved(gaap, OPERATING_INCOME_TAGS, revenue)
    if margin_improved is None:
        return None

    return {
        "revenue_growth": (latest_revenue - prior_revenue) / prior_revenue,
        "eps_growth": (latest_eps - prior_eps) / prior_eps,
        "margin_improved": margin_improved,
    }


def fetch_company_facts(tickers: list[str]) -> dict[str, dict]:
    """Fetch raw SEC EDGAR companyfacts JSON for each ticker, with retries and rate limiting.

    Returns dict of ticker -> facts JSON. Tickers with no SEC filer match are omitted. Callers
    derive both market cap (extract_shares_outstanding) and growth/margin metrics
    (compute_fundamentals) from the same fetched payload — no second round of requests needed.
    """
    ticker_to_cik = load_ticker_to_cik()

    def _fetch_one(ticker: str) -> tuple[str, dict | None]:
        cik = ticker_to_cik.get(ticker)
        if cik is None:
            return ticker, None

        url = FACTS_URL.format(cik=cik)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _rate_limiter.wait()
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 404:
                    return ticker, None
                resp.raise_for_status()
                return ticker, resp.json()
            except Exception as exc:
                logger.warning("Company facts fetch failed for %s (attempt %d/%d): %s", ticker, attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    logger.error("Giving up on company facts for %s", ticker)
                    return ticker, None
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        return ticker, None

    result: dict[str, dict] = {}
    total = len(tickers)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_fetch_one, ticker) for ticker in tickers]
        for processed, future in enumerate(as_completed(futures), start=1):
            ticker, facts = future.result()
            if facts is not None:
                result[ticker] = facts
            if processed % PROGRESS_LOG_INTERVAL == 0 or processed == total:
                logger.info("SEC EDGAR fetch progress: %d/%d tickers processed (%d with data)", processed, total, len(result))

    return result
