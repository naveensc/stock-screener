"""Entry point: fetch data, screen the universe, email the results.

Run: python src/main.py
"""

import logging
from datetime import date

import yaml
from dotenv import load_dotenv

from data import fetch_history, load_universe
from email_digest import send_digest
from fundamentals import compute_fundamentals, fetch_company_facts
from quality_value import screen_quality_value
from ranking import build_top_ideas, market_regime
from screen import screen_universe

load_dotenv()  # no-op in GitHub Actions (no .env file there); loads local .env for dev runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    with open("config/rules.yaml") as f:
        rules = yaml.safe_load(f)

    universe = load_universe()
    total_universe_size = len(universe)

    # Market cap is pre-computed in universe.csv (see build_universe.py) specifically so this
    # filter can run before any network call — no point fetching price history + SEC filings
    # for thousands of sub-$10B tickers just to immediately discard them.
    min_market_cap = rules["universe_filters"]["min_market_cap"]
    universe = universe[universe["market_cap"] >= min_market_cap]
    logger.info(
        "%d/%d tickers meet the $%.0fB market cap floor (from universe.csv)",
        len(universe),
        total_universe_size,
        min_market_cap / 1e9,
    )

    tickers = universe["ticker"].tolist()
    ticker_info = universe.set_index("ticker")[["name", "sector"]].to_dict(orient="index")

    price_data = fetch_history(tickers)
    logger.info("Fetched price history for %d/%d tickers", len(price_data), len(tickers))

    company_facts = fetch_company_facts(tickers)
    logger.info("Fetched SEC filings for %d/%d tickers", len(company_facts), len(tickers))

    matches = screen_universe(price_data, rules, ticker_info)
    logger.info("%d ticker(s) matched at least one criterion", len(matches))

    fundamentals = {}
    for ticker in tickers:
        facts = company_facts.get(ticker)
        if facts is None:
            continue
        computed = compute_fundamentals(facts)
        if computed is not None:
            fundamentals[ticker] = computed
    logger.info("Computed fundamentals for %d/%d tickers", len(fundamentals), len(tickers))

    quality_value_matches = screen_quality_value(price_data, fundamentals, rules, ticker_info)
    logger.info("%d ticker(s) matched the quality-at-a-discount screen", len(quality_value_matches))

    spy_price_data = fetch_history(["SPY"])
    regime = market_regime(spy_price_data["SPY"]["Close"], rules["ranking"]["regime_ma_window"])
    logger.info("Market regime: %s (SPY vs. its %d-day MA)", regime, rules["ranking"]["regime_ma_window"])

    top_ideas = build_top_ideas(matches, quality_value_matches, regime, rules["ranking"])
    logger.info("Top %d idea(s): %s", len(top_ideas), [m["ticker"] for m in top_ideas])

    send_digest(
        matches,
        date.today(),
        universe_size=total_universe_size,
        fetched_count=len(price_data),
        quality_value_matches=quality_value_matches,
        top_ideas=top_ideas,
        regime=regime,
    )


if __name__ == "__main__":
    main()
