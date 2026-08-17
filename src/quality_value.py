"""Hard-filter screen for fundamentally-improving companies whose price is depressed by
market/sector-wide weakness rather than a company-specific problem, ranked by the existing
technical signals among survivors.

A ticker must pass every gate below to appear at all:
  1. Fundamentals confirm the company itself is doing well (revenue/EPS growth, margin trend).
  2. Its price is meaningfully off its highs (priced low).
  3. Its decline isn't meaningfully worse than its own sector's (rules out a company-specific
     reason for the drop — if it fell more than its peers, the market may know something the
     trailing financials don't show yet).
Survivors are then ranked by the same RSI/MA-cross/volume/Bollinger score used by the main
technical screen, so the top of the list is both fundamentally strong and technically timely.
"""

import statistics

import pandas as pd

from indicators import below_long_ma, pct_below_52w_high, trailing_return
from screen import screen_ticker


def _sector_median_returns(price_data: dict[str, pd.DataFrame], ticker_info: dict, window_days: int) -> dict[str, float]:
    """Returns {sector: median trailing return} across all tickers with enough history."""
    by_sector: dict[str, list[float]] = {}
    for ticker, df in price_data.items():
        ret = trailing_return(df["Close"], window_days)
        if ret is None:
            continue
        sector = ticker_info.get(ticker, {}).get("sector", "Unknown")
        by_sector.setdefault(sector, []).append(ret)
    return {sector: statistics.median(rets) for sector, rets in by_sector.items()}


def screen_quality_value(
    price_data: dict[str, pd.DataFrame],
    fundamentals: dict[str, dict],
    rules: dict,
    ticker_info: dict,
) -> list[dict]:
    """Returns matches sorted by technical score descending (ties broken by how far below the
    52-week high, deepest first)."""
    cfg = rules["quality_value"]
    window_days = cfg["trailing_return_window_days"]
    sector_medians = _sector_median_returns(price_data, ticker_info, window_days)

    matches = []
    for ticker, fund in fundamentals.items():
        df = price_data.get(ticker)
        if df is None:
            continue

        if fund["revenue_growth"] < cfg["min_revenue_growth"]:
            continue
        if fund["eps_growth"] < cfg["min_eps_growth"]:
            continue
        if cfg["require_margin_improved"] and not fund["margin_improved"]:
            continue

        close = df["Close"]
        pct_below_high = pct_below_52w_high(close)
        below_ma = below_long_ma(close, rules["ma_cross"]["slow"])
        price_depressed = (pct_below_high is not None and pct_below_high >= cfg["min_pct_below_52w_high"]) or below_ma
        if not price_depressed:
            continue

        stock_return = trailing_return(close, window_days)
        sector = ticker_info.get(ticker, {}).get("sector", "Unknown")
        sector_median = sector_medians.get(sector)
        if stock_return is None or sector_median is None:
            continue
        if stock_return < sector_median - cfg["max_sector_relative_underperformance"]:
            continue  # fell meaningfully worse than its sector — likely company-specific, not macro noise

        technical = screen_ticker(ticker, df, rules, ticker_info) or {"score": 0, "signals": [], "rsi": None}

        info = ticker_info.get(ticker, {})
        matches.append(
            {
                "ticker": ticker,
                "name": info.get("name", ticker),
                "sector": sector,
                "price": close.iloc[-1],
                "pct_below_high": pct_below_high,
                "revenue_growth": fund["revenue_growth"],
                "eps_growth": fund["eps_growth"],
                "score": technical["score"],
                "signals": technical["signals"],
                "rsi": technical["rsi"],
            }
        )

    matches.sort(key=lambda m: (m["score"], m["pct_below_high"] or 0), reverse=True)
    return matches
