"""Fetch daily OHLCV history for the ticker universe from Yahoo Finance."""

import logging
import time

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# yfinance logs a delisted/no-data ticker as an ERROR by default, which looks alarming in CI
# output even though it's expected and already handled below (the ticker is just omitted from
# the result). Quiet it down to avoid that scroll of red text being mistaken for a real failure.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

CHUNK_SIZE = 75
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
# When Yahoo is rate limiting, yf.download often returns empty columns for some tickers instead
# of raising, so the retries above never fire. Missing tickers get one more pass after a pause.
MISSING_RETRY_PAUSE_SECONDS = 60


def load_universe(path: str = "config/universe.csv") -> pd.DataFrame:
    """Returns a DataFrame with columns: ticker, name, sector, market_cap."""
    return pd.read_csv(path)


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def fetch_history(tickers: list[str], period: str = "2y", retry_missing: bool = True) -> dict[str, pd.DataFrame]:
    """Download daily OHLCV history for each ticker, chunked with retries.

    Returns a dict of ticker -> DataFrame (columns: Open, High, Low, Close, Volume).
    Tickers that fail on every retry (and on the missing-ticker pass) are omitted from the result.
    """
    result = _fetch_chunks(tickers, period)
    missing = [t for t in tickers if t not in result]
    if missing and retry_missing:
        logger.warning("%d ticker(s) came back empty; retrying them in %ds", len(missing), MISSING_RETRY_PAUSE_SECONDS)
        time.sleep(MISSING_RETRY_PAUSE_SECONDS)
        result.update(_fetch_chunks(missing, period))
    return result


def _fetch_chunks(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}

    for chunk in _chunk(tickers, CHUNK_SIZE):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                data = yf.download(
                    chunk,
                    period=period,
                    group_by="ticker",
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
                break
            except Exception as exc:  # yfinance/urllib can raise a variety of errors
                logger.warning("Chunk download failed (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
                if attempt == MAX_RETRIES:
                    logger.error("Giving up on chunk starting with %s", chunk[0])
                    data = None
                else:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)

        if data is None:
            continue

        for ticker in chunk:
            try:
                df = data[ticker]
            except KeyError:
                continue
            df = df.dropna(how="all")
            if not df.empty:
                result[ticker] = df

    return result
