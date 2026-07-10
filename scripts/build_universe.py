"""Rebuild config/universe.csv from S&P 500 + NASDAQ-100 constituents.

S&P 500 is scraped live from Wikipedia (reliable, no auth needed). NASDAQ-100
comes from a manually maintained snapshot (see nasdaq100_snapshot.py) since no
free, scrape-friendly live source was available at the time this was written.

Run this occasionally to refresh the ticker list as index membership changes:
    python scripts/build_universe.py
"""

import pandas as pd
from nasdaq100_snapshot import NASDAQ_100

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_sp500() -> set[str]:
    tables = pd.read_html(SP500_URL, storage_options=HEADERS)
    df = tables[0]
    return set(df["Symbol"].astype(str).str.strip())


def main() -> None:
    sp500 = fetch_sp500()
    nasdaq100 = set(NASDAQ_100)
    combined = sorted(t.replace(".", "-") for t in (sp500 | nasdaq100))  # yfinance uses '-' not '.' (e.g. BRK-B)

    out_path = "config/universe.csv"
    pd.DataFrame({"ticker": combined}).to_csv(out_path, index=False)
    print(f"S&P 500: {len(sp500)} tickers")
    print(f"NASDAQ-100: {len(nasdaq100)} tickers")
    print(f"Combined unique: {len(combined)} tickers -> {out_path}")


if __name__ == "__main__":
    main()
