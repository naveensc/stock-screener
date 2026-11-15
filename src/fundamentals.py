"""Fetch company facts from SEC EDGAR's free XBRL API, for market caps.

No API key, no cost — SEC EDGAR is the official source and covers virtually every US-listed
filer. `fetch_company_facts` does the one (rate-limited) network round-trip per ticker, and
`extract_shares_outstanding` / `compute_market_caps` derive market cap from it.
build_universe.py uses this to store each ticker's market cap in universe.csv, so the daily run
can apply its market-cap floor before any network call. (Fundamentals scoring moved to
yfinance — see lifecycle.py and valuation.py.)
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

SHARES_OUTSTANDING_DEI_TAGS = ["EntityCommonStockSharesOutstanding"]
SHARES_OUTSTANDING_GAAP_TAGS = ["CommonStockSharesOutstanding"]

def load_ticker_to_cik() -> dict[str, int]:
    """Returns {ticker: cik} for every SEC filer."""
    resp = requests.get(TICKERS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return {row["ticker"]: row["cik_str"] for row in resp.json().values()}


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


def fetch_company_facts(tickers: list[str]) -> dict[str, dict]:
    """Fetch raw SEC EDGAR companyfacts JSON for each ticker, with retries and rate limiting.

    Returns dict of ticker -> facts JSON. Tickers with no SEC filer match are omitted.
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
