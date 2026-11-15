"""Daily run: score the universe on Quality, Valuation and Technicals, publish the stock cards,
and write the email.

    python src/main.py                    # site/, data/score_history.csv, email.html
    python src/main.py --tickers FILE     # a smaller universe (local testing)
    python src/main.py --backfill 5       # first compute the previous 5 trading days' snapshots

Sending is a separate step (python src/email_digest.py email.html) so the workflow can publish
the cards to GitHub Pages before the email links to them.
"""

import argparse
import logging
import os
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

import email_digest
import lifecycle
import scores
import valuation
from data import fetch_history, load_universe
from screen import screen_ticker

load_dotenv()  # no-op in GitHub Actions (no .env file there); loads local .env for dev runs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CARD_HISTORY_RUNS = 60  # snapshots shown on each card's score chart


def _max_age_hours(ticker: str, today: date, cfg: dict) -> float:
    """Each ticker belongs to one of `buckets` weekday groups and is refreshed when its day
    comes round (cache older than ~a day), and in any case once it's `max_age_days` old."""
    due = zlib.crc32(ticker.encode()) % cfg["buckets"] == today.weekday() % cfg["buckets"]
    return 20 if due else cfg["max_age_days"] * 24


def _load_raws(tickers: list[str], refresh: bool, workers: int, cfg: dict) -> tuple[dict, list[dict]]:
    lifecycle.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    raws, errors = {}, []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(lifecycle.load_raw, t, lifecycle.CACHE_DIR, refresh, _max_age_hours(t, today, cfg)): t
                   for t in tickers}
        for done, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                raws[ticker] = future.result()
            except Exception as exc:
                errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            if done % 50 == 0 or done == len(tickers):
                logger.info("Fundamentals: %d/%d tickers loaded (%d errors)", done, len(tickers), len(errors))
    return raws, errors


def _at_price(raw: dict, price: float) -> dict:
    """The cached fundamentals with today's price applied: market cap and the price-based
    figures Yahoo computed at its own (older) price are rebuilt from the new one."""
    market = dict(raw.get("market") or {})
    old_price, shares = market.get("price"), market.get("shares")
    market["price"] = price
    if market.get("forward_eps") and market["forward_eps"] > 0:
        market["forward_pe"] = price / market["forward_eps"]
    if market.get("peg") and old_price:
        market["peg"] = market["peg"] * price / old_price
    market["price_to_book"] = None  # valuation.py falls back to market cap / equity
    info = dict(raw["info"])
    if shares:
        info["market_cap"] = price * shares
    return {**raw, "market": market, "info": info}


def score_day(raws: dict, price_data: dict, rules: dict, risk_free: float, offset: int = 0) -> tuple[list[dict], list[dict], str]:
    """Everything as of `offset` trading days ago: (results, errors, run_date). Fundamentals
    are the cached ones either way — a backfilled day reuses today's filings."""
    prices = {t: df.iloc[: len(df) - offset] for t, df in price_data.items() if len(df) > offset}
    run_date = prices["SPY"].index[-1].date().isoformat()
    results, errors = [], []
    for ticker, raw in raws.items():
        df = prices.get(ticker)
        if df is None or df.empty:
            errors.append({"ticker": ticker, "error": "no price history"})
            continue
        try:
            results.append(lifecycle.analyze_full(_at_price(raw, float(df["Close"].iloc[-1])), risk_free))
        except Exception as exc:
            errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
    valuation.apply_relative(results)
    sectors = {r["ticker"]: r["sector"] for r in results}
    stock_prices = {t: df for t, df in prices.items() if t in sectors}
    scores.apply_scores(results, scores.technical_scores(stock_prices, sectors, rules), rules["composite"])
    for r in results:
        r["daily"]["as_of"] = run_date
        r["daily"]["universe_size"] = len(results)
    return results, errors, run_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily Quality / Valuation / Technical scoring run.")
    parser.add_argument("--tickers", help="ticker file to use instead of the $10B+ universe")
    parser.add_argument("--out", default="site", help="where the published site is written")
    parser.add_argument("--history", default="data/score_history.csv")
    parser.add_argument("--email-out", default="email.html")
    parser.add_argument("--backfill", type=int, default=0, help="also score the previous N trading days (bootstrap)")
    parser.add_argument("--refresh", action="store_true", help="refetch every ticker's fundamentals")
    parser.add_argument("--workers", type=int, default=lifecycle.DEFAULT_WORKERS)
    args = parser.parse_args()

    with open("config/rules.yaml") as f:
        rules = yaml.safe_load(f)

    if args.tickers:
        tickers = lifecycle.load_universe(args.tickers)
        universe_label = Path(args.tickers).name
    else:
        # Market cap is pre-computed in universe.csv (build_universe.py) so this filter runs before
        # any network call.
        universe = load_universe()
        universe = universe[universe["market_cap"] >= rules["universe_filters"]["min_market_cap"]]
        tickers = universe["ticker"].tolist()
        universe_label = f"${rules['universe_filters']['min_market_cap'] / 1e9:.0f}B+ (S&P 500 + NASDAQ-100 + Russell 2000)"
    logger.info("Scoring %d tickers", len(tickers))

    price_data = fetch_history([*tickers, "SPY"], period="2y")
    logger.info("Fetched price history for %d/%d tickers", len(price_data) - ("SPY" in price_data), len(tickers))
    risk_free = valuation.fetch_risk_free()
    raws, errors = _load_raws(tickers, args.refresh, args.workers, rules["fundamentals_refresh"])

    history_path = Path(args.history)
    history = scores.load_history(history_path)
    for offset in range(args.backfill, 0, -1):
        results, _, day = score_day(raws, price_data, rules, risk_free, offset)
        if history.empty or day not in set(history["date"]):  # never overwrite a real snapshot
            history = scores.save_history(history, scores.snapshot(results, day), history_path)
            logger.info("Backfilled %s", day)

    results, day_errors, run_date = score_day(raws, price_data, rules, risk_free)
    errors += day_errors
    today = scores.snapshot(results, run_date)
    changed = scores.changes(history, today, rules["composite"], rules["changes"])
    history = scores.save_history(history, today, history_path)

    for r in results:
        r["daily"]["history"] = scores.ticker_history(history, r["ticker"], CARD_HISTORY_RUNS)
        alert = screen_ticker(r["ticker"], price_data[r["ticker"]], rules, {})
        r["daily"]["alerts"] = alert["signals"] if alert else []

    results.sort(key=lambda r: (r["daily"]["rank"] is None, r["daily"]["rank"] or 0))
    out_dir = Path(args.out)
    lifecycle.write_outputs(results, sorted(errors, key=lambda e: e["ticker"]), out_dir, f"Daily scores · {run_date}")

    regime = scores.market_regime(price_data["SPY"]["Close"], rules["email"]["regime_ma_window"])
    site_url = os.environ.get("SITE_URL", "").rstrip("/")
    if not site_url:
        site_url = "http://localhost:8765"
        logger.warning("SITE_URL not set — email links point at %s (local preview)", site_url)
    html, subject = email_digest.build_html(
        results, changed, run_date=run_date, regime=regime, universe_label=universe_label,
        error_count=len(errors), risk_free=risk_free, site_url=site_url, top_n=rules["email"]["top_ideas"],
        weights=rules["composite"], min_change=rules["changes"]["min_change"],
    )
    Path(args.email_out).write_text(html, encoding="utf-8")
    logger.info("Wrote %s (%s) and the site to %s/", args.email_out, subject, out_dir)


if __name__ == "__main__":
    main()
