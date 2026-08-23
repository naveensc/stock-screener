"""Rebuild config/universe.csv from S&P 500 + NASDAQ-100 + Russell 2000 constituents.

S&P 500 is scraped live from Wikipedia (reliable, no auth needed) — its table
already includes company name and GICS sector for free. NASDAQ-100 comes from
a manually maintained snapshot (see nasdaq100_snapshot.py) since no free,
scrape-friendly live source was available at the time this was written.
Russell 2000 has no free official constituent list either, so it's sourced
live from IWM (iShares Russell 2000 ETF) — a public, unauthenticated JSON
endpoint that BlackRock's own fund page calls to render its holdings table.

Also computes and stores each ticker's market cap (shares outstanding from SEC EDGAR x a
recent close from yfinance), so the daily run (main.py) can filter out sub-$10B tickers by
reading this column before making a single network call, instead of fetching full price
history + SEC filings for the whole universe just to find out who gets excluded. This does
mean market cap is only as fresh as the last time this script was run, not computed daily —
acceptable for a coarse floor that a company doesn't cross and re-cross day to day.

Run this occasionally to refresh the ticker list as index membership changes:
    python scripts/build_universe.py
"""

import sys
from pathlib import Path

import pandas as pd
import requests
from nasdaq100_snapshot import NASDAQ_100

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data import fetch_history  # noqa: E402
from fundamentals import compute_market_caps, fetch_company_facts  # noqa: E402

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
IWM_HOLDINGS_URL = "https://www.ishares.com/varnish-api/blk-one01-product-data/product-data/api/v2/get-product-data"
IWM_PORTFOLIO_ID = 239710  # iShares Russell 2000 ETF (IWM)
HEADERS = {"User-Agent": "Mozilla/5.0"}

# iShares' sector labels mostly match GICS but differ from the Wikipedia S&P 500 table in a
# couple of spots; normalize so sectors are consistent across the merged universe.
SECTOR_ALIASES = {"Communication": "Communication Services"}

# IWM's feed lists these share classes without the dash Yahoo Finance/SEC use (verified against
# SEC's own ticker list, which has no filer under the un-dashed form but does under this one) —
# without this, yfinance can't find price data for these tickers at all.
TICKER_CORRECTIONS = {
    "MOGA": "MOG-A",  # Moog Inc Class A
    "BHA": "BH-A",  # Biglari Holdings Class A
    "CRDA": "CRD-A",  # Crawford & Company Class A
    "GEFB": "GEF-B",  # Greif Inc Class B
}

# Not real tradeable common equity, so yfinance always fails on them — merger-related
# contingent value rights (CVR) and an escrow claim from IWM's feed, plus a couple of tickers
# for companies no longer actively trading (verified: none resolve to an active SEC filer).
EXCLUDED_TICKERS = {"ADRO", "AKE", "GTXI", "INH", "P5N994", "PDLI", "SBT", "THRD"}


def fetch_sp500() -> dict[str, tuple[str, str]]:
    """Returns {ticker: (name, sector)}."""
    tables = pd.read_html(SP500_URL, storage_options=HEADERS)
    df = tables[0]
    result = {}
    for _, row in df.iterrows():
        ticker = str(row["Symbol"]).strip().replace(".", "-")  # yfinance uses '-' not '.' (e.g. BRK-B)
        result[ticker] = (str(row["Security"]).strip(), str(row["GICS Sector"]).strip())
    return result


def fetch_russell2000() -> dict[str, tuple[str, str]]:
    """Returns {ticker: (name, sector)}, sourced from IWM's holdings as a proxy for the Russell
    2000 index (the ETF tracks it directly; a handful of cash/futures positions are filtered out
    below via assetClass). Company names come through as ALL CAPS from the feed and are
    title-cased here — treat casing as approximate.
    """
    params = {
        "appSubType": "ISHARES",
        "appType": "PRODUCT_PAGE",
        "component": "holdings.all",
        "locale": "en_US",
        "portfolioId": IWM_PORTFOLIO_ID,
        "targetSite": "us-ishares",
        "userType": "individual",
        "excludeContent": "true",
        "includeConfig": "true",
    }
    resp = requests.get(IWM_HOLDINGS_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    points = resp.json()["componentsByNameMap"]["holdings"]["containersByNameMap"]["all"]["dataPointsByNameMap"]

    result = {}
    for ticker, name, sector, asset_class in zip(
        points["ticker"]["formattedValue"],
        points["issueName"]["formattedValue"],
        points["sectorName"]["formattedValue"],
        points["assetClass"]["formattedValue"],
    ):
        if asset_class != "Equity" or ticker == "-":
            continue
        ticker = TICKER_CORRECTIONS.get(ticker, ticker)
        if ticker in EXCLUDED_TICKERS:
            continue
        result[ticker] = (name.title(), SECTOR_ALIASES.get(sector, sector))
    return result


def main() -> None:
    sp500 = fetch_sp500()
    nasdaq100 = {ticker: (name, sector) for ticker, name, sector in NASDAQ_100}
    russell2000 = fetch_russell2000()

    # Merge with S&P 500 taking precedence for overlapping tickers (freshest, scraped live),
    # then NASDAQ-100, then Russell 2000 (small-cap; no expected overlap with the other two).
    combined = {**russell2000, **nasdaq100, **sp500}
    tickers = list(combined.keys())

    print(f"Computing market cap for {len(tickers)} tickers (SEC EDGAR is rate-limited — this takes a while)...")
    price_data = fetch_history(tickers, period="5d")  # just need a recent close, not full history
    company_facts = fetch_company_facts(tickers)
    market_caps = compute_market_caps(tickers, price_data, company_facts)

    out_path = "config/universe.csv"
    rows = [
        {"ticker": ticker, "name": name, "sector": sector, "market_cap": market_caps.get(ticker)}
        for ticker, (name, sector) in combined.items()
    ]
    pd.DataFrame(rows).sort_values("ticker").to_csv(out_path, index=False)

    print(f"S&P 500: {len(sp500)} tickers")
    print(f"NASDAQ-100: {len(nasdaq100)} tickers")
    print(f"Russell 2000: {len(russell2000)} tickers")
    print(f"Combined unique: {len(combined)} tickers -> {out_path}")
    print(f"Market cap confirmed for {len(market_caps)}/{len(tickers)} tickers")


if __name__ == "__main__":
    main()
