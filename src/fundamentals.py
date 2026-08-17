"""Fetch fundamentals (YoY revenue/EPS growth, margin trend) from SEC EDGAR's free XBRL API.

No API key, no cost — SEC EDGAR is the official source and covers virtually every
US-listed filer. The tradeoff: it only has reported actuals (no analyst consensus
estimates), so there's no earnings-beat/miss detection here, only growth and margin
trend off the two most recent comparable quarters.

Companies tag the same concept inconsistently (e.g. some use `Revenues`, others
`RevenueFromContractWithCustomerExcludingAssessedTax`), so each metric tries a
fallback chain of tags. Tickers without a clean quarter-over-quarter-a-year-ago
match for every required tag are omitted entirely — the downstream hard filter
treats missing fundamentals as "can't confirm quality" rather than a pass.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
HEADERS = {"User-Agent": "stock-screener research naveensc@gmail.com"}

MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY_SECONDS = 0.15  # stay well within SEC's fair-use rate limit

REVENUE_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
GROSS_PROFIT_TAGS = ["GrossProfit"]
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]

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
                days = (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days
                if not (MIN_QUARTER_DAYS <= days <= MAX_QUARTER_DAYS):
                    continue
                existing = by_end.get(entry["end"])
                if existing is None or entry["filed"] > existing["filed"]:
                    by_end[entry["end"]] = entry
        if by_end:
            return sorted(by_end.values(), key=lambda e: e["end"])
    return []


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


def fetch_fundamentals(tickers: list[str]) -> dict[str, dict]:
    """Fetch and compute fundamentals for each ticker via SEC EDGAR, with retries.

    Returns dict of ticker -> {revenue_growth, eps_growth, margin_improved}. Tickers with no
    SEC filer match, or without enough tagged/discrete-quarter history, are omitted.
    """
    ticker_to_cik = load_ticker_to_cik()

    def _fetch_one(ticker: str) -> tuple[str, dict | None]:
        cik = ticker_to_cik.get(ticker)
        if cik is None:
            return ticker, None

        url = FACTS_URL.format(cik=cik)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                time.sleep(REQUEST_DELAY_SECONDS)
                resp = requests.get(url, headers=HEADERS, timeout=30)
                if resp.status_code == 404:
                    return ticker, None
                resp.raise_for_status()
                return ticker, compute_fundamentals(resp.json())
            except Exception as exc:
                logger.warning("Fundamentals fetch failed for %s (attempt %d/%d): %s", ticker, attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    logger.error("Giving up on fundamentals for %s", ticker)
                    return ticker, None
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        return ticker, None

    result: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(_fetch_one, ticker) for ticker in tickers]
        for future in as_completed(futures):
            ticker, metrics = future.result()
            if metrics is not None:
                result[ticker] = metrics

    return result
