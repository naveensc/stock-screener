"""Entry point: fetch data, screen the universe, email the results.

Run: python src/main.py
"""

import logging
from datetime import date

import yaml
from dotenv import load_dotenv

from data import fetch_history, load_universe
from email_digest import send_digest
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

    matches = screen_universe(price_data, rules, ticker_info)
    logger.info("%d ticker(s) matched at least one criterion", len(matches))

    send_digest(matches, date.today(), universe_size=len(tickers), fetched_count=len(price_data))


if __name__ == "__main__":
    main()
