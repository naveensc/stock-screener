"""Entry point: fetch data, screen the universe, email the results.

Run: python src/main.py
"""

import logging
from datetime import date

import yaml
from dotenv import load_dotenv

from data import fetch_history, load_universe
from email_digest import send_digest
from fundamentals import compute_fundamentals, fetch_company_facts, filter_by_market_cap
from quality_value import screen_quality_value
from screen import screen_universe

load_dotenv()  # no-op in GitHub Actions (no .env file there); loads local .env for dev runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    with open("config/rules.yaml") as f:
        rules = yaml.safe_load(f)

    universe = load_universe()
    tickers = universe["ticker"].tolist()
    ticker_info = universe.set_index("ticker")[["name", "sector"]].to_dict(orient="index")
    logger.info("Universe: %d tickers", len(tickers))

    price_data = fetch_history(tickers)
    logger.info("Fetched price history for %d/%d tickers", len(price_data), len(tickers))

    company_facts = fetch_company_facts(tickers)
    logger.info("Fetched SEC filings for %d/%d tickers", len(company_facts), len(tickers))

    min_market_cap = rules["universe_filters"]["min_market_cap"]
    qualifying_tickers = filter_by_market_cap(tickers, price_data, company_facts, min_market_cap)
    logger.info(
        "%d/%d tickers meet the $%.0fB market cap floor",
        len(qualifying_tickers),
        len(tickers),
        min_market_cap / 1e9,
    )
    price_data = {t: price_data[t] for t in qualifying_tickers}

    matches = screen_universe(price_data, rules, ticker_info)
    logger.info("%d ticker(s) matched at least one criterion", len(matches))

    fundamentals = {}
    for ticker in qualifying_tickers:
        computed = compute_fundamentals(company_facts[ticker])
        if computed is not None:
            fundamentals[ticker] = computed
    logger.info("Computed fundamentals for %d/%d qualifying tickers", len(fundamentals), len(qualifying_tickers))

    quality_value_matches = screen_quality_value(price_data, fundamentals, rules, ticker_info)
    logger.info("%d ticker(s) matched the quality-at-a-discount screen", len(quality_value_matches))

    send_digest(
        matches,
        date.today(),
        universe_size=len(tickers),
        fetched_count=len(price_data),
        quality_value_matches=quality_value_matches,
    )


if __name__ == "__main__":
    main()
