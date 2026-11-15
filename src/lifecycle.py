"""Classify each company's business life-cycle phase from yfinance financials.

Three questions decide the phase:
  1. Is revenue growing?        latest fiscal year vs the one before (annual statements)
  2. Is it profitable?          operating cash flow if asset-light, net income otherwise (LTM)
  3. Is it returning capital?   buybacks + dividends over the last twelve months (LTM)

  1 Startup             unprofitable, not growing fast, not returning capital (or no revenue base yet)
  2 Hyper Growth        unprofitable, revenue growing >= HYPER_GROWTH_MIN
  3 Operating Leverage  growing and profitable, not yet returning capital
  4 Capital Return      growing, profitable and returning capital
  5 Decline             revenue not growing — or unprofitable, not growing fast, yet still paying
                        out capital (a mature company in a loss year, e.g. Ford, isn't a startup)

"LTM" is the sum of the last four reported quarters when they're consecutive; otherwise
(Yahoo sometimes skips a quarter) the latest fiscal year stands in. Revenue growth always uses
fiscal years — Yahoo only serves 5-7 quarters, not the 8 an LTM-vs-prior-LTM comparison needs.

Each ticker is also re-analyzed as of its previous fiscal year (latest annual column dropped,
quarterlies ignored) to flag phase transitions.

Usage:
    python src/lifecycle.py --universe tickers.txt
    python src/lifecycle.py --universe sp500 --phase 3
    python src/lifecycle.py --demo
"""

import argparse
import csv
import html
import json
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

import valuation
from fundamentals import _RateLimiter  # the same global pacer the SEC fetch uses
from statements import (
    BUYBACK_ROWS,
    CAPEX_ROWS,
    DEPRECIATION_ROWS,
    DIVIDEND_ROWS,
    EBIT_ROWS,
    INVESTED_CAPITAL_ROWS,
    NET_INCOME_ROWS,
    OCF_ROWS,
    OPERATING_INCOME_ROWS,
    PPE_ROWS,
    REVENUE_ROWS,
    TAX_RATE_ROWS,
    TOTAL_ASSETS_ROWS,
    fiscal_years,
)
from statements import metric as _metric
from statements import value as _value

logger = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # see data.py — failures are handled below

# ---- Config ---------------------------------------------------------------------------------
HYPER_GROWTH_MIN = 0.20  # YoY revenue growth at/above this is "hyper growth"
REVENUE_GROWING_MIN = 0.0  # YoY revenue growth must exceed this to count as "growing"
# Asset-light vs asset-heavy is a majority vote of these signals (among those Yahoo reports).
# Asset-light -> profit judged by operating cash flow; asset-heavy -> net income. Depreciation
# stands in for maintenance cost, which Yahoo doesn't report separately.
ASSET_SIGNALS = {  # key: (label, threshold, asset-light when the value is at/below the threshold?)
    "ppe_pct": ("PP&E / total assets", 0.20, True),  # physical assets
    "capex_pct": ("Capex / revenue", 0.05, True),  # capital expenditure
    "dna_pct": ("Depreciation / revenue", 0.04, True),  # depreciation, maintenance proxy
    "op_margin": ("Operating margin", 0.15, False),  # margins
    "roic": ("ROIC", 0.15, False),  # return on invested capital
}
DEFAULT_TAX_RATE = 0.21  # for ROIC when Yahoo has no effective tax rate
BUYBACK_MIN_PCT = 0.02  # "returning capital" = any dividend, or buybacks >= this share of revenue.
# Small buybacks often just offset stock-comp dilution (PLTR: ~1% of revenue in 2026); dividends
# are a deliberate commitment, and a revenue-based bar on them would miss low-margin payers (Ford).
HYBRID_LIGHT_SHARE = (0.2, 0.8)  # share of signals pointing asset-light strictly between these = Hybrid
# (with all five reported: 4-5 light = Asset-light, 0-1 = Asset-heavy, 2-3 = Hybrid). Hybrid is
# for display and valuation; the profit metric above still follows the simple majority.
CACHE_TTL_HOURS = 24
DEFAULT_WORKERS = 8
MAX_RETRIES = 4
RETRY_BASE_SECONDS = 2  # backoff: 2s, 4s, 8s (+ jitter)
PROGRESS_LOG_INTERVAL = 50
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "lifecycle"
FETCH_MIN_INTERVAL_SECONDS = 0.4  # global pace between ticker fetches (each is ~7 Yahoo requests)
CACHE_VERSION = 5  # bump when fetch_raw starts storing new statements; older cache files are refetched
# ---------------------------------------------------------------------------------------------

PHASES = {1: "Startup", 2: "Hyper Growth", 3: "Operating Leverage", 4: "Capital Return", 5: "Decline"}
KEY_TRANSITIONS = {(2, 3): "Just turned profitable", (3, 4): "Started returning capital"}

class InsufficientData(Exception):
    """Ticker lacks the statements needed to classify it — skipped, not retried."""


_fetch_pacer = _RateLimiter(FETCH_MIN_INTERVAL_SECONDS)  # shared by all fetch threads


# ---- Phase logic ----------------------------------------------------------------------------


def classify_phase(revenue_growth: float | None, profitable: bool, returning_capital: bool) -> int:
    if revenue_growth is None:  # no positive revenue base to measure growth from (pre-revenue)
        return 1
    if revenue_growth <= REVENUE_GROWING_MIN:
        return 5
    if not profitable:
        if revenue_growth >= HYPER_GROWTH_MIN:
            return 2
        return 5 if returning_capital else 1
    return 4 if returning_capital else 3


def transition_label(from_phase: int, to_phase: int) -> str | None:
    """Returns a label for the transitions worth highlighting, None for the rest."""
    if to_phase == 5:
        return "Entered decline"
    return KEY_TRANSITIONS.get((from_phase, to_phase))


def _ratio(num: float | None, den: float | None) -> float | None:
    return num / den if num is not None and den is not None and den > 0 else None


def asset_signals(raw: dict, latest_year: str, revenue: float) -> dict[str, float | None]:
    """The ASSET_SIGNALS values: flows are LTM where available, balance-sheet items are as of
    the latest fiscal year-end. None where Yahoo doesn't report the inputs."""
    bs = raw["annual"][latest_year]
    capex, _ = _metric(raw, CAPEX_ROWS)
    dna, _ = _metric(raw, DEPRECIATION_ROWS)
    operating_income, _ = _metric(raw, OPERATING_INCOME_ROWS)
    ebit, _ = _metric(raw, EBIT_ROWS)
    tax_rate = _value(bs, TAX_RATE_ROWS)
    tax_rate = DEFAULT_TAX_RATE if tax_rate is None else tax_rate
    return {
        "ppe_pct": _ratio(_value(bs, PPE_ROWS), _value(bs, TOTAL_ASSETS_ROWS)),
        "capex_pct": _ratio(abs(capex) if capex is not None else None, revenue),
        "dna_pct": _ratio(abs(dna) if dna is not None else None, revenue),
        "op_margin": _ratio(operating_income, revenue),
        "roic": _ratio(ebit * (1 - tax_rate) if ebit is not None else None, _value(bs, INVESTED_CAPITAL_ROWS)),
    }


def light_votes(signals: dict[str, float | None]) -> tuple[int, int]:
    """Returns (signals pointing asset-light, signals available)."""
    light = available = 0
    for key, (_, threshold, light_if_below) in ASSET_SIGNALS.items():
        value = signals.get(key)
        if value is None:
            continue
        available += 1
        light += value <= threshold if light_if_below else value >= threshold
    return light, available


def analyze(raw: dict, asset_light: bool | None = None) -> dict:
    """Classify one ticker from its raw statements (see fetch_raw for the shape). asset_light
    overrides the vote — analyze_prior passes the current answer so both years are judged on
    the same profit metric."""
    annual = raw["annual"]
    years = fiscal_years(raw)
    revenues = [_value(annual[d], REVENUE_ROWS) for d in years]
    if len(years) < 2:
        raise InsufficientData("needs two fiscal years of revenue")

    latest_rev, prior_rev = revenues[0], revenues[1]
    growth = (latest_rev - prior_rev) / prior_rev if prior_rev > 0 else None

    revenue, revenue_basis = _metric(raw, REVENUE_ROWS)
    if revenue is None:
        revenue, revenue_basis = latest_rev, f"FY{years[0][:4]}"
    signals = asset_signals(raw, years[0], revenue)
    votes_light, votes_available = light_votes(signals)
    # Missing capex (banks, insurers) -> asset-heavy: the balance sheet is the asset base, and
    # their operating cash flow swings with deposits/loans, so net income is the honest signal.
    # A tie counts as heavy (the more conservative net-income test).
    if asset_light is None:
        asset_light = signals["capex_pct"] is not None and votes_light * 2 > votes_available
    light_share = votes_light / votes_available if votes_available else 0
    if signals["capex_pct"] is None or light_share <= HYBRID_LIGHT_SHARE[0]:
        asset_model = "Asset-heavy"
    elif light_share >= HYBRID_LIGHT_SHARE[1]:
        asset_model = "Asset-light"
    else:
        asset_model = "Hybrid"

    profit_metric = "Operating cash flow" if asset_light else "Net income"
    profit_value, profit_basis = _metric(raw, OCF_ROWS if asset_light else NET_INCOME_ROWS)
    if profit_value is None:
        raise InsufficientData(f"no {profit_metric.lower()}")

    buybacks, capital_basis = _metric(raw, BUYBACK_ROWS)
    dividends, dividends_basis = _metric(raw, DIVIDEND_ROWS)
    buybacks, dividends = abs(buybacks or 0), abs(dividends or 0)
    capital_returned = buybacks + dividends

    profitable = profit_value > 0
    returning = dividends > 0 or (revenue > 0 and buybacks / revenue >= BUYBACK_MIN_PCT)
    phase = classify_phase(growth, profitable, returning)

    history = []
    for i, d in enumerate(years):
        p = annual[d]
        prev = revenues[i + 1] if i + 1 < len(revenues) else None
        rev = revenues[i]
        history.append({
            "fy": f"FY{d[:4]}",
            "revenue": rev,
            "growth": (rev - prev) / prev if rev is not None and prev else None,
            "net_income": _value(p, NET_INCOME_ROWS),
            "ocf": _value(p, OCF_ROWS),
            "buybacks": abs(_value(p, BUYBACK_ROWS) or 0),
            "dividends": abs(_value(p, DIVIDEND_ROWS) or 0),
        })

    info = raw.get("info", {})
    quarterly = raw.get("quarterly", {})
    return {
        "ticker": raw["ticker"],
        "name": info.get("name") or raw["ticker"],
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "country": info.get("country") or "",
        "market_cap": info.get("market_cap"),
        "fiscal_year": f"FY{years[0][:4]}",
        "latest_quarter": max(quarterly) if quarterly else None,
        "phase": phase,
        "phase_name": PHASES[phase],
        "revenue": revenue,
        "revenue_basis": revenue_basis,
        "revenue_growth": growth,
        "asset_light": asset_light,
        "asset_model": asset_model,
        **signals,
        "light_votes": votes_light,
        "signals_available": votes_available,
        "profit_metric": profit_metric,
        "profit_value": profit_value,
        "profit_basis": profit_basis,
        "buybacks": buybacks,
        "dividends": dividends,
        "capital_returned": capital_returned,
        "capital_basis": capital_basis,
        "dividends_basis": dividends_basis,
        "revenue_growing": growth is not None and growth > REVENUE_GROWING_MIN,
        "profitable": profitable,
        "returning_capital": returning,
        "history": history,
    }


def analyze_prior(raw: dict, asset_light: bool | None = None) -> dict | None:
    """Analysis as of the previous fiscal year, or None if there isn't enough history. Pass the
    current asset_light so a company drifting across the asset-light line doesn't "change phase"
    just because its profit metric switched (Ford's OCF vs net income differ by ~$20B)."""
    latest = fiscal_years(raw)[0]
    prior ={**raw, "annual": {d: p for d, p in raw["annual"].items() if d < latest}, "quarterly": {}}
    try:
        return analyze(prior, asset_light)
    except InsufficientData:
        return None


def analyze_with_transition(raw: dict) -> dict:
    result = analyze(raw)
    prior = analyze_prior(raw, result["asset_light"])
    result["prior_phase"] = prior["phase"] if prior else None
    result["prior_phase_name"] = prior["phase_name"] if prior else ""
    changed = prior is not None and prior["phase"] != result["phase"]
    result["phase_changed"] = changed
    result["transition"] = (transition_label(prior["phase"], result["phase"]) or "") if changed else ""
    return result


# ---- Fetching + cache -----------------------------------------------------------------------


def _frames_to_periods(*frames: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Merge statement DataFrames (rows = line items, columns = period ends) into
    {"YYYY-MM-DD": {line item: value}}, dropping NaNs. Line-item names don't collide across
    the income, cash flow and balance sheet statements, so one dict per period is enough."""
    periods: dict[str, dict[str, float]] = {}
    for df in frames:
        if df is None or df.empty:
            continue
        for col in df.columns:
            values = {str(row): float(v) for row, v in df[col].items() if pd.notna(v)}
            if values:
                periods.setdefault(pd.Timestamp(col).date().isoformat(), {}).update(values)
    return periods


class IncompleteStatements(Exception):
    """Yahoo returned an empty statement. Under rate limiting yfinance often returns empty tables
    rather than raising, so this is retried — and never cached, which would freeze a partial
    record for the cache lifetime."""


def fetch_raw(ticker: str) -> dict:
    _fetch_pacer.wait()
    tk = yf.Ticker(ticker)
    statements = {"income statement": tk.income_stmt, "cash flow": tk.cashflow, "balance sheet": tk.balance_sheet}
    empty = [name for name, df in statements.items() if df is None or df.empty]
    if empty:
        raise IncompleteStatements(f"empty {', '.join(empty)} from Yahoo")
    annual = _frames_to_periods(statements["income statement"], statements["cash flow"])
    for period, values in _frames_to_periods(statements["balance sheet"]).items():
        annual.setdefault(period, {}).update(values)
    quarterly = _frames_to_periods(tk.quarterly_income_stmt, tk.quarterly_cashflow)
    info = tk.info or {}
    if not info.get("marketCap"):
        raise IncompleteStatements("empty quote info (no market cap) from Yahoo")

    def number(key: str) -> float | None:
        v = info.get(key)
        return float(v) if isinstance(v, (int, float)) and v == v else None

    estimates = {}
    try:  # analyst coverage is optional — valuation falls back to historical growth without it
        est = tk.revenue_estimate
        if est is not None and "growth" in est:
            estimates = {p: float(g) for p, g in est["growth"].items() if p in ("0y", "+1y") and pd.notna(g)}
    except Exception as exc:
        logger.debug("%s: no revenue estimates (%s)", ticker, exc)
    prices = {}
    try:  # month-end closes (split-adjusted, not dividend-adjusted) for the historical valuation range
        hist = tk.history(period="6y", interval="1mo", auto_adjust=False)["Close"].dropna()
        prices = {pd.Timestamp(d).date().isoformat(): float(c) for d, c in hist.items()}
    except Exception as exc:
        logger.debug("%s: no price history (%s)", ticker, exc)
    return {
        "ticker": ticker,
        "version": CACHE_VERSION,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "info": {
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "market_cap": info.get("marketCap"),
        },
        "market": {
            "price": number("currentPrice") or number("regularMarketPrice"),
            "shares": number("sharesOutstanding"),
            "beta": number("beta"),
            "total_debt": number("totalDebt"),
            "total_cash": number("totalCash"),
            "trailing_eps": number("trailingEps"),
            "forward_eps": number("forwardEps"),
            "forward_pe": number("forwardPE"),
            "peg": number("trailingPegRatio"),
            "price_to_book": number("priceToBook"),
            "insider_pct": number("heldPercentInsiders"),
            "currency": info.get("financialCurrency") or info.get("currency"),
            "summary": info.get("longBusinessSummary"),
        },
        "estimates": estimates,
        "prices": prices,
        "annual": annual,
        "quarterly": quarterly,
    }


def fetch_with_retries(ticker: str) -> dict:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fetch_raw(ticker)
        except Exception as exc:  # yfinance/requests raise a variety of errors, incl. rate limits
            if attempt == MAX_RETRIES:
                if isinstance(exc, IncompleteStatements):
                    raise InsufficientData(f"{exc} after {MAX_RETRIES} tries (not covered, or rate-limited)") from exc
                raise
            delay = RETRY_BASE_SECONDS * 2 ** (attempt - 1) + random.uniform(0, 1)
            logger.warning("%s fetch failed (attempt %d/%d): %s — retrying in %.1fs", ticker, attempt, MAX_RETRIES, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def load_raw(ticker: str, cache_dir: Path, refresh: bool, max_age_hours: float = CACHE_TTL_HOURS) -> dict:
    path = cache_dir / f"{ticker}.json"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") == CACHE_VERSION:
            return raw
    raw = fetch_with_retries(ticker)
    path.write_text(json.dumps(raw), encoding="utf-8")
    return raw


def analyze_full(raw: dict, risk_free: float) -> dict:
    """Phase + transition + the per-ticker half of the valuation (valuation.apply_relative
    finishes it once every ticker is in)."""
    result = analyze_with_transition(raw)
    result["valuation"] = valuation.evaluate(raw, result, risk_free)
    return result


def run(tickers: list[str], workers: int, cache_dir: Path, refresh: bool,
        risk_free: float) -> tuple[list[dict], list[dict]]:
    cache_dir.mkdir(parents=True, exist_ok=True)

    def one(ticker: str) -> dict:
        return analyze_full(load_raw(ticker, cache_dir, refresh), risk_free)

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(one, t): t for t in tickers}
        for done, future in enumerate(as_completed(futures), start=1):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"})
            if done % PROGRESS_LOG_INTERVAL == 0 or done == len(tickers):
                logger.info("Processed %d/%d tickers (%d errors)", done, len(tickers), len(errors))
    return results, errors


# ---- Universe -------------------------------------------------------------------------------


def load_universe(spec: str) -> list[str]:
    scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    if spec == "sp500":
        from build_universe import fetch_sp500  # Wikipedia via pandas.read_html

        tickers = list(fetch_sp500())
    elif spec == "nasdaq100":
        from nasdaq100_snapshot import NASDAQ_100  # no free live source — see that file

        tickers = [t for t, _, _ in NASDAQ_100]
    else:
        tickers = []
        for line in Path(spec).read_text(encoding="utf-8").splitlines():
            ticker = line.split(",")[0].strip().strip('"').upper().replace(".", "-")
            if ticker and not ticker.startswith("#") and ticker not in ("TICKER", "SYMBOL"):
                tickers.append(ticker)
    return list(dict.fromkeys(tickers))


# ---- Demo data ------------------------------------------------------------------------------


def make_raw(ticker: str, annual: list[dict], quarterly: list[dict] | None = None, name: str = "",
             sector: str = "", market_cap: float | None = None, last_fy: int = 2025) -> dict:
    """Build a raw record from plain numbers (oldest period first). Keys per period: revenue,
    net_income, ocf, capex, buybacks, dividends, and optionally dna, operating_income, ebit, ppe,
    total_assets, invested_capital — positive amounts, signed like Yahoo here."""

    def period(p: dict) -> dict:
        rows = {
            "Total Revenue": p.get("revenue"),
            "Net Income": p.get("net_income"),
            "Operating Cash Flow": p.get("ocf"),
            "Capital Expenditure": -p["capex"] if p.get("capex") is not None else None,
            "Repurchase Of Capital Stock": -p["buybacks"] if p.get("buybacks") else None,
            "Cash Dividends Paid": -p["dividends"] if p.get("dividends") else None,
            "Reconciled Depreciation": p.get("dna"),
            "Operating Income": p.get("operating_income"),
            "EBIT": p.get("ebit"),
            "Net PPE": p.get("ppe"),
            "Total Assets": p.get("total_assets"),
            "Invested Capital": p.get("invested_capital"),
        }
        return {k: float(v) for k, v in rows.items() if v is not None}

    first_fy = last_fy - len(annual) + 1
    annual_periods = {f"{first_fy + i}-12-31": period(p) for i, p in enumerate(annual)}
    quarter_ends = pd.date_range(end=f"{last_fy + 1}-06-30", periods=len(quarterly or []), freq="QE")
    quarterly_periods = {d.date().isoformat(): period(p) for d, p in zip(quarter_ends, quarterly or [])}
    return {
        "ticker": ticker,
        "info": {"name": name or ticker, "sector": sector, "market_cap": market_cap},
        # A $100 share price is arbitrary; only market cap matters for the demo's multiples.
        "market": {"price": 100.0, "shares": market_cap / 100 if market_cap else None, "beta": 1.1},
        "annual": annual_periods,
        "quarterly": quarterly_periods,
    }


def _fy(revenue, net_income, ocf, capex=0, buybacks=0, dividends=0, scale=1e6, **asset_rows) -> dict:
    """One period in $M. asset_rows: optional dna, operating_income, ebit, ppe, total_assets,
    invested_capital (the asset-model inputs)."""
    return {k: (v * scale if v is not None else None) for k, v in dict(
        revenue=revenue, net_income=net_income, ocf=ocf, capex=capex, buybacks=buybacks, dividends=dividends,
        **asset_rows).items()}


def _assets(ppe, total_assets, invested_capital, dna, operating_income, ebit=None) -> dict:
    return dict(ppe=ppe, total_assets=total_assets, invested_capital=invested_capital, dna=dna,
                operating_income=operating_income, ebit=operating_income if ebit is None else ebit)


def demo_universe() -> dict[str, dict]:
    """Synthetic companies ($M), one or more per phase, including each key transition. Asset-model
    inputs are given for the latest year only — the prior-year rerun reuses the current label."""
    return {r["ticker"]: r for r in [
        make_raw("STRT", [_fy(0, -20, -18, 1), _fy(0, -35, -30, 2), _fy(0, -60, -55, 3),
                          _fy(0, -80, -70, 4, **_assets(30, 200, 150, 4, -80))],
                 name="Seedling Robotics", sector="Industrials", market_cap=0.4e9),
        make_raw("HYPR", [_fy(100, -90, -60, 3), _fy(180, -120, -70, 5), _fy(300, -150, -60, 8),
                          _fy(480, -140, -30, 12, **_assets(40, 900, 700, 10, -150, -140))],
                 name="Hyperion Cloud", sector="Technology", market_cap=12e9),
        make_raw("ASTL", [_fy(200, -10, 5, 4), _fy(270, 5, 8, 5), _fy(360, 20, 12, 7),
                          _fy(490, 30, -15, 9, **_assets(20, 800, 500, 8, 40))],
                 name="Astral Payments (+NI, -OCF)", sector="Financial Services", market_cap=9e9),
        make_raw("LEVR", [_fy(200, -80, -50, 4), _fy(320, -60, -20, 6), _fy(500, -40, -10, 10),
                          _fy(700, 30, 90, 14, **_assets(50, 1500, 1000, 15, 40))],
                 name="Leverage Software", sector="Technology", market_cap=25e9),
        make_raw("GROW", [_fy(1000, 80, 150, 100), _fy(1250, 110, 190, 125), _fy(1500, 150, 240, 150),
                          _fy(1800, 200, 300, 180, **_assets(2500, 4000, 3000, 150, 300))],
                 name="Grower Logistics", sector="Industrials", market_cap=30e9),
        make_raw("RTRN", [_fy(5000, 600, 900, 200), _fy(5500, 700, 1000, 220), _fy(6000, 800, 1100, 240),
                          _fy(6400, 850, 1200, 250, buybacks=500, **_assets(1500, 9000, 7000, 200, 1100))],
                 name="Return Health", sector="Health Care", market_cap=60e9),
        make_raw("CASH", [_fy(80000, 20000, 25000, 2000, 15000, 4000), _fy(85000, 22000, 27000, 2100, 16000, 4200),
                          _fy(90000, 24000, 29000, 2200, 18000, 4400),
                          _fy(95000, 26000, 31000, 2300, 20000, 4600, **_assets(40000, 350000, 150000, 11000, 30000, 31000))],
                 name="Cashflow Consumer Co", sector="Consumer Staples", market_cap=900e9),
        make_raw("FADE", [_fy(9000, 900, 1100, 600, 0, 400), _fy(9200, 920, 1150, 600, 0, 420),
                          _fy(9300, 930, 1160, 610, 0, 440),
                          _fy(8800, 700, 1000, 600, 0, 450, **_assets(5000, 12000, 9000, 500, 900))],
                 name="Fading Media", sector="Communication Services", market_cap=15e9),
        make_raw("SINK", [_fy(4000, 100, 300, 200), _fy(3600, -50, 150, 180), _fy(3200, -200, 50, 160),
                          _fy(2900, -300, -20, 150, **_assets(1800, 3500, 2500, 160, -250))],
                 name="Sinking Retail", sector="Consumer Discretionary", market_cap=1.2e9),
    ]}


# ---- Reports --------------------------------------------------------------------------------


STYLE = """
:root{--bg:#f6f6f3;--surface:#fff;--text:#1c1c1f;--muted:#6a6a72;--border:#e2e2dd;--link:#2b55c9;
--p1:#6e44b3;--p1bg:#efe8fa;--p2:#1f5fc4;--p2bg:#e4eefc;--p3:#1b7d45;--p3bg:#e1f3e8;--p4:#9a5b00;--p4bg:#fbeed6;
--p5:#b8352a;--p5bg:#fbe5e2;--yes:#1b7d45;--no:#b8352a;--hl:#fff7e0;--light:#0b6e78;--lightbg:#ddf2f3;
--heavy:#4b4b57;--heavybg:#e6e6ec}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--surface:#1b1b1f;--text:#ebebef;--muted:#9898a2;--border:#2d2d33;
--link:#8fb0ff;--p1:#c4a6f5;--p1bg:#2c2340;--p2:#8fb8ff;--p2bg:#1b2b45;--p3:#7fd6a2;--p3bg:#173226;--p4:#f0bd6a;
--p4bg:#3a2c14;--p5:#f39a90;--p5bg:#3d1d1a;--yes:#7fd6a2;--no:#f39a90;--hl:#2e2a1a;--light:#7fd3db;--lightbg:#15343a;
--heavy:#c4c4ce;--heavybg:#2e2e36}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1200px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px}.muted{color:var(--muted)}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin:16px 0}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ph1{color:var(--p1);background:var(--p1bg)}.ph2{color:var(--p2);background:var(--p2bg)}.ph3{color:var(--p3);background:var(--p3bg)}
.ph4{color:var(--p4);background:var(--p4bg)}.ph5{color:var(--p5);background:var(--p5bg)}
.yes{color:var(--yes);font-weight:600}.no{color:var(--no);font-weight:600}
.am{display:inline-block;padding:1px 8px;border-radius:4px;font-size:12px;font-weight:600;white-space:nowrap}
.am-light{color:var(--light);background:var(--lightbg)}.am-heavy{color:var(--heavy);background:var(--heavybg)}
.am-hybrid{color:var(--p4);background:var(--p4bg)}
table{width:100%;border-collapse:collapse}th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto}
"""


def _badge(phase: int | None) -> str:
    if phase is None:
        return '<span class="muted">—</span>'
    return f'<span class="badge ph{phase}">{phase} · {PHASES[phase]}</span>'


ASSET_BADGE_CLASS = {"Asset-light": "am-light", "Hybrid": "am-hybrid", "Asset-heavy": "am-heavy"}


def _asset_badge(model: str) -> str:
    return f'<span class="am {ASSET_BADGE_CLASS[model]}">{model}</span>'


CARD_STYLE = """
:root{--paper:#f3f2ec;--tile:#e8eadf;--tile-border:#d6d9cb;--ink:#1d2116;--ink-muted:#676b5c;--olive:#4f5d2b;
--olive-ink:#f6f8ea;--olive-sub:#cfdc95;--pill-yes:#4f5d2b;--pill-no:#a8392c;--rev:#2b2f22;--profit:#5d7a1f;
--loss:#c0503f;--buy:#a8392c;--div:#4f5d2b;--here:#e3ecb8}
@media (prefers-color-scheme:dark){:root{--paper:#15160f;--tile:#20231a;--tile-border:#353a2b;--ink:#eef0e4;
--ink-muted:#a3a794;--olive:#56672a;--olive-ink:#f6f8ea;--olive-sub:#d6e59a;--pill-yes:#6c8034;--pill-no:#c4544a;
--rev:#e6e8dc;--profit:#a9cf5a;--loss:#f0877c;--buy:#f0877c;--div:#a9cf5a;--here:#323a19}}
body{background:var(--paper);color:var(--ink);font-family:"Archivo",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:960px}.panel{background:var(--tile);border-color:var(--tile-border)}
.top{display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--ink-muted);letter-spacing:.04em}
.top b{color:var(--ink);font-weight:700;letter-spacing:.08em}
.title{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:flex-start;margin:18px 0 16px}
.id{display:flex;align-items:flex-start;gap:12px}
.mono{width:38px;height:38px;border-radius:6px;background:var(--olive);color:var(--olive-ink);display:grid;
place-items:center;font-weight:800;font-size:18px;flex:none}
.ticker{font-size:44px;font-weight:800;line-height:1;letter-spacing:-.02em}
.ticker small{font-size:22px;font-weight:600;margin-left:6px;letter-spacing:0}
.sub{color:var(--ink-muted);margin-top:6px}
.tags{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:4px;background:var(--tile);border:1px solid var(--tile-border)}
.tile{background:var(--tile);border:1px solid var(--tile-border);border-radius:8px;padding:14px 16px}
.label{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--ink-muted);text-transform:uppercase}
.hero{display:grid;grid-template-columns:150px 1fr 220px;gap:10px}
.mcap{font-size:30px;font-weight:800;margin-top:6px}
.qrow{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:3px 0}
.qrow b{font-size:16px;font-weight:700;display:block;line-height:1.3}.qrow .d{font-size:12px;color:var(--ink-muted)}
.pill{font-size:12px;font-weight:700;color:#fff;border-radius:999px;padding:3px 11px;white-space:nowrap}
.pill.yes{background:var(--pill-yes)}.pill.no{background:var(--pill-no)}
.metric{background:var(--olive);color:var(--olive-ink);border-radius:8px;padding:14px 16px}
.metric .label{color:var(--olive-sub)}.metric .big{font-size:22px;font-weight:800;margin:8px 0 4px;line-height:1.2}
.metric .note{font-size:12px;color:var(--olive-sub)}
.block{margin-top:10px}.head{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center}
.flag{font-size:11px;font-weight:800;letter-spacing:.06em}
.plot{position:relative;margin:26px 0 22px}
.plot svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.pt{position:absolute;transform:translate(-50%,-130%);font-size:12px;font-weight:700;white-space:nowrap}
.pt.below{transform:translate(-50%,30%)}
.xs{display:grid;font-size:11px;color:var(--ink-muted);text-align:center}
.legend{display:flex;gap:14px;font-size:12px;color:var(--ink-muted)}
.legend i{display:inline-block;width:16px;height:3px;border-radius:2px;vertical-align:middle;margin-right:5px}
.foot{font-size:12px;color:var(--ink-muted);margin-top:6px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cols{display:grid;grid-template-columns:repeat(5,1fr);text-align:center;margin-top:10px}
.cols div{font-weight:800;font-size:15px;padding:6px 4px;border-right:1px solid var(--tile-border)}
.cols div:last-child{border-right:0}.cols .now{background:var(--here);border-radius:6px 6px 0 0}
.cycle svg{display:block;width:100%;height:auto}
.cy-ann{fill:var(--ink-muted);font:600 19px "Archivo",system-ui,sans-serif}
.cy-here{fill:var(--ink);font:800 19px "Archivo",system-ui,sans-serif}
@media (max-width:720px){.hero,.pair{grid-template-columns:1fr}.ticker{font-size:36px}.cols div{font-size:11px}}
"""


def money_short(v: float | None) -> str:
    """Chart-label money: $168B, $3.6T, $27B, $8.5M."""
    if v is None:
        return "—"
    sign, a = ("-" if v < 0 else ""), abs(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            n = a / div
            return f"{sign}${n:.0f}{suffix}" if n >= 10 else f"{sign}${n:.1f}{suffix}"
    return f"{sign}${a:.0f}"


def _line_chart(labels: list[str], series: list[tuple[list, str, bool]], height: int = 110) -> str:
    """Line chart with a value printed on every point. series: (values, CSS color, labels below
    the line?). The SVG stretches to the box; labels are HTML so they never distort."""
    values = [v for vals, _, _ in series for v in vals if v is not None]
    lo, hi = min([0, *values]), max([0, *values])
    span = (hi - lo) or 1
    n = len(labels)

    def x(i: int) -> float:
        return (i + 0.5) / n * 100

    def y(v: float) -> float:
        return 4 + (hi - v) / span * 92

    zero = y(0)
    svg = [f'<line x1="0" x2="100" y1="{zero:.2f}" y2="{zero:.2f}" style="stroke:var(--tile-border)" '
           f'stroke-width="1.5" vector-effect="non-scaling-stroke"/>']
    points = []
    for vals, color, below in series:
        coords = " ".join(f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(vals) if v is not None)
        svg.append(f'<polyline points="{coords}" fill="none" style="stroke:{color}" stroke-width="2.5" '
                   f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>')
        for i, v in enumerate(vals):
            if v is not None:
                cls = "pt below" if below else "pt"
                label_color = "var(--loss)" if v < 0 else color
                points.append(f'<div class="{cls}" style="left:{x(i):.2f}%;top:{y(v):.2f}%;color:{label_color}">{money_short(v)}</div>')
    xs = "".join(f"<div>{html.escape(label)}</div>" for label in labels)
    return (f'<div class="plot" style="height:{height}px"><svg viewBox="0 0 100 100" preserveAspectRatio="none">'
            f'{"".join(svg)}</svg>{"".join(points)}</div>'
            f'<div class="xs" style="grid-template-columns:repeat({n},1fr)">{xs}</div>')


def _small_chart(labels: list[str], values: list[float], color: str, what: str) -> str:
    if not any(values):
        return f'<p class="foot" style="margin:14px 0 4px">No {what} in {labels[0]}–{labels[-1]}.</p>' if labels else ""
    return _line_chart(labels, [(values, color, False)], 60)


def _cycle_diagram(phase: int, ticker: str) -> str:
    """The five-phase life-cycle illustration with the company's phase highlighted."""
    cols = "".join(f'<div class="{"now" if p == phase else ""}">{p}: {name}</div>' for p, name in PHASES.items())
    x0 = (phase - 1) * 200
    revenue = "M0,165 C120,163 230,152 330,128 C440,100 560,62 700,46 C780,38 840,36 880,42 C930,52 970,90 1000,135"
    profit = "M0,172 C70,180 130,205 185,212 C260,206 340,185 400,170 C480,150 560,125 650,108 C730,95 800,90 860,92 C920,98 960,135 1000,185"
    dashes = 'fill="none" stroke-width="7" stroke-dasharray="9 7"'
    return f"""<div class="cols">{cols}</div>
<svg viewBox="0 0 1000 250" role="img" aria-label="{html.escape(ticker)} is in phase {phase}, {PHASES[phase]}">
<defs><clipPath id="above"><rect x="0" y="0" width="1000" height="170"/></clipPath>
<clipPath id="below"><rect x="0" y="170" width="1000" height="80"/></clipPath></defs>
<rect x="{x0}" y="0" width="200" height="250" style="fill:var(--here)"/>
{"".join(f'<line x1="{c}" x2="{c}" y1="0" y2="250" style="stroke:var(--tile-border)" stroke-width="2"/>' for c in (200, 400, 600, 800))}
<line x1="0" x2="1000" y1="170" y2="170" style="stroke:var(--tile-border)" stroke-width="2"/>
<path d="{revenue}" {dashes} style="stroke:var(--rev)"/>
<path d="{profit}" {dashes} style="stroke:var(--profit)" clip-path="url(#above)"/>
<path d="{profit}" {dashes} style="stroke:var(--loss)" clip-path="url(#below)"/>
<text x="{x0 + 100}" y="26" text-anchor="middle" class="cy-here">{html.escape(ticker)} is here</text>
<path d="M226,232 L194,216" style="stroke:var(--ink-muted)" stroke-width="2.5"/><text x="232" y="240" class="cy-ann">Losses peak</text>
<path d="M430,206 L406,178" style="stroke:var(--ink-muted)" stroke-width="2.5"/><text x="436" y="216" class="cy-ann">Breakeven</text>
<path d="M630,150 L608,126" style="stroke:var(--ink-muted)" stroke-width="2.5"/><text x="618" y="172" class="cy-ann">Dividends /</text>
<text x="618" y="194" class="cy-ann">Buybacks</text>
</svg>
<div class="legend" style="margin-top:6px"><span><i style="background:var(--rev)"></i>Revenue</span>
<span><i style="background:var(--profit)"></i>Profit</span><span><i style="background:var(--loss)"></i>Losses</span></div>"""


DAILY_SCORE_LABELS = {"composite": "Composite", "quality": "Quality", "valuation": "Valuation", "technical": "Technical"}
DAILY_SCORE_COLORS = {"composite": "var(--ink)", "quality": "var(--profit)", "valuation": "var(--p4)", "technical": "var(--p2)"}
TECHNICAL_FACTORS = {  # key: (label, how the raw value reads)
    "trend": ("Trend", "avg. distance from 50/200-day averages and 50-day slope"),
    "momentum": ("Momentum", "12-1 month return"),
    "relative_strength": ("Relative strength", "6-month return minus sector median"),
    "rsi": ("RSI pullback", "100 − RSI percentile vs its own year"),
    "volume": ("Volume", "up-day ÷ down-day volume, 50 days"),
}


def _sparkline(history: list[dict]) -> str:
    """Score history (0-100) for the composite and its three parts, one line each."""
    if len(history) < 2:
        return '<p class="foot">The score chart fills in as daily snapshots accumulate.</p>'
    n = len(history)
    lines = ""
    for key, color in DAILY_SCORE_COLORS.items():
        coords = " ".join(f"{i / (n - 1) * 100:.2f},{100 - h[key]:.2f}" for i, h in enumerate(history) if h.get(key) is not None)
        width = 3 if key == "composite" else 1.5
        lines += (f'<polyline points="{coords}" fill="none" style="stroke:{color}" stroke-width="{width}" '
                  f'vector-effect="non-scaling-stroke" stroke-linejoin="round"/>')
    legend = "".join(f'<span><i style="background:{c}"></i>{DAILY_SCORE_LABELS[k]}</span>' for k, c in DAILY_SCORE_COLORS.items())
    return (f'<div class="legend" style="margin-top:12px">{legend}</div>'
            f'<div style="position:relative;height:90px;margin-top:6px"><svg viewBox="0 0 100 100" preserveAspectRatio="none" '
            f'style="position:absolute;inset:0;width:100%;height:100%;overflow:visible">'
            f'<line x1="0" x2="100" y1="50" y2="50" style="stroke:var(--tile-border)" vector-effect="non-scaling-stroke"/>'
            f'{lines}</svg></div><div class="xs" style="grid-template-columns:1fr 1fr"><div style="text-align:left">'
            f'{history[0]["date"]}</div><div style="text-align:right">{history[-1]["date"]}</div></div>')


def _daily_panel(r: dict) -> str:
    """Composite and its three percentile scores, the technical factors behind the Technical
    score, today's alert tags and the score history. Only present on daily-run cards."""
    d = r.get("daily")
    if not d:
        return ""
    e = html.escape
    rank = f"#{d['rank']} of {d['universe_size']}" if d.get("rank") else "not ranked"
    bars = ""
    for key in ("quality", "valuation", "technical"):
        v = d[key]
        width = 0 if v is None else v
        bars += (f'<div class="qrow"><div style="flex:1"><b style="font-size:14px">{DAILY_SCORE_LABELS[key]}</b>'
                 f'<div style="height:8px;background:var(--paper);border-radius:4px;margin-top:4px">'
                 f'<div style="height:8px;width:{width:.0f}%;background:{DAILY_SCORE_COLORS[key]};border-radius:4px"></div></div></div>'
                 f'<b style="font-size:18px;min-width:34px;text-align:right">{"–" if v is None else f"{v:.0f}"}</b></div>')
    raw, comps = d.get("technical_raw") or {}, d.get("technical_components") or {}
    factor_rows = ""
    for key, (label, meaning) in TECHNICAL_FACTORS.items():
        value = raw.get(key)
        if value is None:
            shown = "n/a"
        elif key == "volume":
            shown = f"{value:.2f}x"
        elif key == "rsi":
            shown = f"{value:.0f}"
        else:
            shown = f"{value * 100:+.1f}%"
        pct_rank = comps.get(key)
        factor_rows += (f'<tr><td>{label} <span class="muted" style="font-size:11px">· {meaning}</span></td>'
                        f'<td class="num">{shown}</td><td class="num">{"n/a" if pct_rank is None else f"{pct_rank:.0f}"}</td></tr>')
    alerts = "".join(f'<span class="tag" style="margin-right:4px">{e(a)}</span>' for a in d.get("alerts") or [])
    composite = "–" if d["composite"] is None else f"{d['composite']:.0f}"
    return f"""<div class="tile block"><div class="head"><span class="label">Daily scores · as of {d["as_of"]}</span>
<span class="flag">{rank}</span></div>
<div class="hero" style="grid-template-columns:150px 1fr;margin-top:10px">
<div><div class="mcap" style="font-size:44px">{composite}</div><div class="foot">Composite, 0–100 percentile<br>Trend: <b>{e(d["trend_state"])}</b></div></div>
<div>{bars}</div></div>
{f'<div style="margin-top:8px">{alerts}</div>' if alerts else ""}
<div class="scroll" style="margin-top:10px"><table><tr><th>Technical factor</th><th class="num">Value</th><th class="num">Percentile</th></tr>{factor_rows}</table></div>
<p class="foot">Each score ranks this stock against today's universe (100 = best). Quality and Valuation come from the analysis below;
Technical averages the last 5 days so one session can't swing it.</p>
{_sparkline(d.get("history") or [])}</div>"""


def _as_of(r: dict) -> str:
    if r["profit_basis"] == "LTM" and r["latest_quarter"]:
        d = date.fromisoformat(r["latest_quarter"])
        return f"as of Q{(d.month - 1) // 3 + 1} {d.year}"
    return f"as of {r['fiscal_year']}"


def render(r: dict) -> str:
    """One-page HTML card for a single analyzed ticker."""
    e = html.escape
    years = list(reversed(r["history"][:5]))  # oldest -> newest
    labels = [h["fy"] for h in years]
    profit_key = "ocf" if r["asset_light"] else "net_income"
    revenue_vals = [h["revenue"] for h in years]
    profit_vals = [h[profit_key] for h in years]
    buyback_vals = [h["buybacks"] for h in years]
    dividend_vals = [h["dividends"] for h in years]
    if r["revenue_basis"] == "LTM" and r["profit_basis"] == "LTM":
        main_labels = labels + ["LTM"]
        revenue_vals, profit_vals = revenue_vals + [r["revenue"]], profit_vals + [r["profit_value"]]
    else:
        main_labels = labels
    buyback_labels = labels + ["LTM"] if r["capital_basis"] == "LTM" else labels
    buyback_vals = buyback_vals + [r["buybacks"]] if r["capital_basis"] == "LTM" else buyback_vals
    dividend_labels = labels + ["LTM"] if r["dividends_basis"] == "LTM" else labels
    dividend_vals = dividend_vals + [r["dividends"]] if r["dividends_basis"] == "LTM" else dividend_vals

    # --- the three questions
    growth = r["revenue_growth"]
    revenue_note = "No revenue base to measure growth" if growth is None else f"{growth * 100:+.0f}% in the last year"
    prev_profit = next((v for v in reversed(profit_vals[:-1]) if v is not None), None)
    if prev_profit is None:
        trend = ""
    elif r["profit_value"] < 0 and prev_profit < 0:
        trend = ", losses narrowing" if r["profit_value"] > prev_profit else ", losses widening"
    else:
        trend = ", rising" if r["profit_value"] > prev_profit else ", falling"
    profit_note = f"{money_short(r['profit_value'])} {r['profit_metric'].lower()}{trend}"
    big_buybacks = r["revenue"] > 0 and r["buybacks"] / r["revenue"] >= BUYBACK_MIN_PCT
    token_buybacks = r["buybacks"] > 0 and not big_buybacks
    if big_buybacks and r["dividends"]:
        capital_note = "Buybacks and dividends"
    elif r["dividends"]:
        capital_note = "Dividends" + (f" (buybacks under {BUYBACK_MIN_PCT:.0%} of revenue)" if token_buybacks else "")
    elif big_buybacks:
        capital_note = "Buybacks"
    elif token_buybacks:
        capital_note = f"Only token buybacks (under {BUYBACK_MIN_PCT:.0%} of revenue)"
    else:
        capital_note = "No buybacks or dividends"
    questions = [("Is revenue growing?", r["revenue_growing"], revenue_note),
                 ("Is it profitable?", r["profitable"], profit_note),
                 ("Is it returning capital?", r["returning_capital"], capital_note)]
    q_html = "".join(f'<div class="qrow"><div><b>{q}</b><span class="d">{e(note)}</span></div>'
                     f'<span class="pill {"yes" if ok else "no"}">{"✓ Yes" if ok else "✕ No"}</span></div>'
                     for q, ok, note in questions)

    if r["capex_pct"] is None and not r["asset_light"]:
        metric_note = "No capex reported (bank/insurer), so net income"
    elif r["asset_model"] == "Hybrid":
        metric_note = f"Hybrid — the signal majority leans asset-{'light' if r['asset_light'] else 'heavy'}"
    else:
        metric_note = f"best signal for {r['asset_model'].lower()}"

    # --- buyback / dividend panels
    last_bb = buyback_vals[-1] if buyback_vals else 0
    bb_flag = "YES" if big_buybacks else ("SMALL" if r["buybacks"] else "NO")
    bb_foot = (f"{money_short(last_bb)} returned in buybacks ({buyback_labels[-1]})" if last_bb else "No buybacks")
    last_div = dividend_vals[-1] if dividend_vals else 0
    div_growing = len(dividend_vals) > 1 and last_div > 0 and last_div > dividend_vals[-2]
    div_flag = "YES &amp; GROWING" if div_growing else ("YES" if last_div else "NO")
    div_foot = (f"{money_short(last_div)} paid in dividends{', and rising' if div_growing else ''} ({dividend_labels[-1]})"
                if last_div else "No dividend")

    # --- extras: phase change + asset model
    if r.get("phase_changed"):
        label = f' — <b>{e(r["transition"])}</b>' if r["transition"] else ""
        change = (f'<div class="panel" style="background:var(--here)">Phase change since last fiscal year: '
                  f'{_badge(r["prior_phase"])} → {_badge(r["phase"])}{label}</div>')
    else:
        change = ""
    votes = f"{r['light_votes']} of {r['signals_available']} signals point asset-light"
    signal_rows = ""
    for key, (label, threshold, light_if_below) in ASSET_SIGNALS.items():
        v = r[key]
        if v is None:
            verdict = '<span class="muted">not reported</span>'
        else:
            light = v <= threshold if light_if_below else v >= threshold
            verdict = f'<span class="am {"am-light" if light else "am-heavy"}">{"Light" if light else "Heavy"}</span>'
        signal_rows += (f'<tr><td>{label}</td><td class="num">{"—" if v is None else f"{v * 100:.1f}%"}</td>'
                        f'<td class="num">{"≤" if light_if_below else "≥"} {threshold:.0%}</td><td>{verdict}</td></tr>')
    asset_note = ("No capex reported (typical of banks/insurers), so the company is treated as asset-heavy."
                  if r["capex_pct"] is None else
                  f"{votes}. 4–5 of 5 = Asset-light, 0–1 = Asset-heavy, in between = Hybrid. The profit metric "
                  f"follows the simple majority (a tie counts as heavy).")

    sub = " · ".join(e(x) for x in (r["sector"], r["industry"]) if x)
    tags = "".join(f'<span class="tag">{e(t)}</span>' for t in [r["country"]] if t)
    profit_title = "Operating cash flow" if r["asset_light"] else "Net income"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(r["ticker"])} Phase Check</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>{STYLE}{CARD_STYLE}{valuation.STYLE}</style></head><body><div class="wrap">
<div class="top"><span><a href="../screener.html">← Life-cycle Screener</a> &nbsp;/ <b>PHASE CHECK</b></span><span>{_as_of(r)}</span></div>
<div class="title"><div class="id"><div class="mono">{e(r["ticker"][:1])}</div>
<div><div class="ticker">{e(r["ticker"])}<small>{e(r["name"])}</small></div><div class="sub">{sub}</div></div></div>
<div class="tags">{tags}{_asset_badge(r["asset_model"])}{_badge(r["phase"])}</div></div>

<div class="hero">
<div class="tile"><div class="label">Market cap</div><div class="mcap">{money_short(r["market_cap"])}</div></div>
<div class="tile">{q_html}</div>
<div class="metric"><div class="label">Profit metric</div><div class="big">{profit_title}</div><div class="note">{e(metric_note)}</div></div>
</div>
{_daily_panel(r)}
{change}
<div class="tile block"><div class="head"><span class="label">Revenue &amp; {profit_title}</span>
<span class="legend"><span><i style="background:var(--rev)"></i>Revenue</span><span><i style="background:var(--profit)"></i>{profit_title}</span></span></div>
{_line_chart(main_labels, [(revenue_vals, "var(--rev)", False), (profit_vals, "var(--profit)", True)], 130)}</div>

<div class="pair block">
<div class="tile"><div class="head"><span class="label">Stock buybacks by year</span><span class="flag">{bb_flag}</span></div>
{_small_chart(buyback_labels, buyback_vals, "var(--buy)", "buybacks")}<div class="foot">{bb_foot}</div></div>
<div class="tile"><div class="head"><span class="label">Dividends by year</span><span class="flag">{div_flag}</span></div>
{_small_chart(dividend_labels, dividend_vals, "var(--div)", "dividends")}<div class="foot">{div_foot}</div></div>
</div>

<div class="tile block cycle"><span class="label">Where it sits in the business life cycle</span>
{_cycle_diagram(r["phase"], r["ticker"])}</div>

<div class="panel"><h2>Asset model {_asset_badge(r["asset_model"])}</h2><div class="scroll"><table>
<tr><th>Signal</th><th class="num">Value</th><th class="num">Asset-light if</th><th>Points to</th></tr>{signal_rows}</table></div>
<p class="muted" style="margin:10px 0 0">{asset_note} Flows are {r["profit_basis"]}; balance-sheet items as of {r["fiscal_year"]} year-end.
Depreciation stands in for maintenance cost.</p></div>

<h2 style="font-size:20px;margin:28px 0 4px">Valuation &amp; quality analysis</h2>
<p class="muted" style="margin:0">Computed from Yahoo Finance data with generated reasoning. Trailing figures are {r["valuation"]["ltm_basis"]}; forward figures are analyst estimates.</p>
{valuation.render_html(r)}
<p class="muted">Data: Yahoo Finance via yfinance. Generated {date.today().isoformat()}. Not investment advice.</p>
</div></body></html>"""


def render_screener(results: list[dict], title: str) -> str:
    e = html.escape
    changes = sorted((r for r in results if r["phase_changed"]), key=lambda r: (not r["transition"], r["ticker"]))
    if changes:
        items = ""
        for r in changes:
            label = f' <b>{e(r["transition"])}</b>' if r["transition"] else ""
            items += (f'<li class="{"key" if label else ""}"><a href="cards/{e(r["ticker"])}.html"><b>{e(r["ticker"])}</b></a> '
                      f'{e(r["name"])}: {_badge(r["prior_phase"])} → {_badge(r["phase"])}{label}</li>')
        changes_html = f'<ul class="changes">{items}</ul>'
    else:
        changes_html = '<p class="muted">No phase changes vs the prior fiscal year.</p>'

    keys = ["ticker", "name", "sector", "market_cap", "phase", "asset_model", "revenue_growth", "profit_metric",
            "profit_value", "capital_returned", "revenue_growing", "profitable", "returning_capital",
            "score", "valuation_verdict", "base_upside", "composite"]
    default_sort = "composite" if any(r.get("composite") is not None for r in results) else "market_cap"
    data = json.dumps([{k: r.get(k) for k in keys} for r in results]).replace("</", "<\\/")  # composite: daily runs only
    phases = json.dumps(PHASES)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Life-cycle Screener</title>
<style>{STYLE}
.changes{{margin:0;padding:0;list-style:none}}.changes li{{padding:6px 8px;border-radius:6px}}
.changes li.key{{background:var(--hl)}}
.bar{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}}
.tab{{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:999px;padding:5px 12px;
cursor:pointer;font:inherit;font-size:13px}}.tab.on{{background:var(--text);color:var(--bg);border-color:var(--text)}}
input[type=search]{{flex:1;min-width:180px;padding:6px 10px;border:1px solid var(--border);border-radius:8px;
background:var(--surface);color:var(--text);font:inherit}}
th[data-key]{{cursor:pointer;user-select:none}}th[data-key]:hover{{color:var(--text)}}
th.sorted::after{{content:" ▾"}}th.sorted.asc::after{{content:" ▴"}}
</style></head><body><div class="wrap">
<h1>Life-cycle Screener</h1><div class="muted">{e(title)} · {len(results)} companies · generated {date.today().isoformat()}</div>
<div class="panel"><h2>Phase changes vs prior fiscal year ({len(changes)})</h2>{changes_html}</div>
<div class="panel"><div class="bar" id="tabs"></div><div class="bar" id="assets"></div>
<div class="bar"><input type="search" id="q" placeholder="Filter by ticker, name or sector"><span class="muted" id="count"></span></div>
<div class="scroll"><table><thead><tr>
<th data-key="ticker">Ticker</th><th data-key="name">Name</th><th data-key="sector">Sector</th>
<th data-key="market_cap" class="num">Mkt cap</th><th data-key="phase">Phase</th><th data-key="asset_model">Asset model</th>
<th data-key="revenue_growth" class="num">Rev growth</th><th data-key="profit_value" class="num">Profit</th>
<th data-key="capital_returned" class="num">Capital returned</th>
<th data-key="revenue_growing">Growing</th><th data-key="profitable">Profitable</th><th data-key="returning_capital">Returning</th>
<th data-key="composite" class="num">Composite</th><th data-key="score" class="num">Model score</th>
<th data-key="valuation_verdict">Valuation</th><th data-key="base_upside" class="num">Base DCF</th>
</tr></thead><tbody id="rows"></tbody></table></div></div>
<p class="muted">Data: Yahoo Finance via yfinance. Click a ticker for its one-page card; click a column to sort.</p>
</div>
<script>
const DATA = {data};
const PHASES = {phases};
const state = {{phase: 0, asset: "", q: "", key: "{default_sort}", dir: -1}};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);
function money(v) {{
  if (v == null) return "—";
  const a = Math.abs(v), s = v < 0 ? "-" : "";
  for (const [d, u] of [[1e12, "T"], [1e9, "B"], [1e6, "M"]]) if (a >= d) return s + "$" + (a / d).toFixed(1) + u;
  return s + "$" + Math.round(a).toLocaleString();
}}
const pct = v => v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const yn = b => b ? '<span class="yes">Yes</span>' : '<span class="no">No</span>';
const badge = p => `<span class="badge ph${{p}}">${{p}} · ${{PHASES[p]}}</span>`;
const ASSET_CLASS = {json.dumps(ASSET_BADGE_CLASS)};
const assetBadge = m => `<span class="am ${{ASSET_CLASS[m]}}">${{m}}</span>`;
const VERDICT_CLASS = {{"Undervalued": "yes", "Overvalued": "no"}};
const verdict = v => `<span class="${{VERDICT_CLASS[v] || "muted"}}">${{esc(v)}}</span>`;

// Each filter row counts within the other row's current selection.
function drawTabs() {{
  const byAsset = DATA.filter(r => !state.asset || r.asset_model === state.asset);
  const byPhase = DATA.filter(r => !state.phase || r.phase === state.phase);
  const tabs = [[0, "All"], ...Object.entries(PHASES).map(([p, n]) => [+p, p + " " + n])];
  document.getElementById("tabs").innerHTML = tabs.map(([p, n]) =>
    `<button class="tab${{p === state.phase ? " on" : ""}}" data-phase="${{p}}">${{esc(n)}} (${{byAsset.filter(r => !p || r.phase === p).length}})</button>`).join("");
  document.getElementById("assets").innerHTML = [["", "All asset models"], ["Asset-light", "Asset-light"], ["Hybrid", "Hybrid"], ["Asset-heavy", "Asset-heavy"]].map(([m, n]) =>
    `<button class="tab${{m === state.asset ? " on" : ""}}" data-asset="${{m}}">${{n}} (${{byPhase.filter(r => !m || r.asset_model === m).length}})</button>`).join("");
}}
function draw() {{
  const q = state.q.toLowerCase();
  const rows = DATA.filter(r => (!state.phase || r.phase === state.phase) && (!state.asset || r.asset_model === state.asset) &&
    (!q || [r.ticker, r.name, r.sector].some(s => String(s).toLowerCase().includes(q))));
  rows.sort((a, b) => {{
    const x = a[state.key], y = b[state.key];
    if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.dir;
  }});
  document.getElementById("rows").innerHTML = rows.length ? rows.map(r => `<tr>
    <td><a href="cards/${{encodeURIComponent(r.ticker)}}.html"><b>${{esc(r.ticker)}}</b></a></td>
    <td>${{esc(r.name)}}</td><td>${{esc(r.sector)}}</td><td class="num">${{money(r.market_cap)}}</td>
    <td>${{badge(r.phase)}}</td><td>${{assetBadge(r.asset_model)}}</td><td class="num">${{pct(r.revenue_growth)}}</td>
    <td class="num" title="${{esc(r.profit_metric)}}">${{money(r.profit_value)}} <span class="muted">${{r.profit_metric === "Net income" ? "NI" : "OCF"}}</span></td>
    <td class="num">${{money(r.capital_returned)}}</td>
    <td>${{yn(r.revenue_growing)}}</td><td>${{yn(r.profitable)}}</td><td>${{yn(r.returning_capital)}}</td>
    <td class="num"><b>${{r.composite == null ? "—" : Math.round(r.composite)}}</b></td>
    <td class="num">${{r.score == null ? "—" : Math.round(r.score)}}</td><td>${{verdict(r.valuation_verdict)}}</td>
    <td class="num">${{pct(r.base_upside)}}</td></tr>`).join("")
    : '<tr><td colspan="16" class="muted">No companies match.</td></tr>';
  document.getElementById("count").textContent = rows.length + " shown";
  document.querySelectorAll("th[data-key]").forEach(th => {{
    th.classList.toggle("sorted", th.dataset.key === state.key);
    th.classList.toggle("asc", th.dataset.key === state.key && state.dir === 1);
  }});
}}
document.getElementById("tabs").addEventListener("click", ev => {{
  const b = ev.target.closest("button"); if (!b) return;
  state.phase = +b.dataset.phase; drawTabs(); draw();
}});
document.getElementById("assets").addEventListener("click", ev => {{
  const b = ev.target.closest("button"); if (!b) return;
  state.asset = b.dataset.asset; drawTabs(); draw();
}});
document.getElementById("q").addEventListener("input", ev => {{ state.q = ev.target.value; draw(); }});
document.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {{
  const k = th.dataset.key;
  state.dir = state.key === k ? -state.dir : (["ticker", "name", "sector", "phase", "asset_model", "valuation_verdict"].includes(k) ? 1 : -1);
  state.key = k; draw();
}}));
drawTabs(); draw();
</script></body></html>"""


CSV_COLUMNS = ["ticker", "name", "sector", "market_cap", "phase", "phase_name", "revenue_growth", "profit_metric",
               "profit_value", "capital_returned", "revenue_growing", "profitable", "returning_capital",
               "asset_model", *ASSET_SIGNALS, "light_votes", "signals_available",
               "prior_phase", "prior_phase_name", "transition",
               "score", "quality_score", "valuation_score", "valuation_verdict", "price", "intrinsic_bear",
               "intrinsic_base", "intrinsic_bull", "base_upside", "forward_pe", "ev_ebitda", "fcf_yield", "peers"]
ROUNDED_COLUMNS = ("revenue_growth", *ASSET_SIGNALS, "base_upside", "forward_pe", "ev_ebitda", "fcf_yield",
                   "intrinsic_bear", "intrinsic_base", "intrinsic_bull")


def write_outputs(results: list[dict], errors: list[dict], out_dir: Path, title: str) -> None:
    (out_dir / "cards").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            rounded = {k: round(r[k], 4) for k in ROUNDED_COLUMNS if r.get(k) is not None}
            writer.writerow({**r, **rounded})
    with open(out_dir / "errors.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "error"])
        writer.writeheader()
        writer.writerows(errors)
    for r in results:
        (out_dir / "cards" / f"{r['ticker']}.html").write_text(render(r), encoding="utf-8")
    (out_dir / "screener.html").write_text(render_screener(results, title), encoding="utf-8")


def print_summary(results: list[dict], errors: list[dict], out_dir: Path) -> None:
    print(f"\nLife-cycle phases ({len(results)} companies, {len(errors)} skipped -> errors.csv)")
    print(f"  {'':<22}{'total':>5}{'light':>7}{'hybrid':>7}{'heavy':>7}")
    for p, name in PHASES.items():
        in_phase = [r for r in results if r["phase"] == p]
        counts = [sum(r["asset_model"] == m for r in in_phase) for m in ("Asset-light", "Hybrid", "Asset-heavy")]
        print(f"  {p} {name:<20}{len(in_phase):>5}" + "".join(f"{c:>7}" for c in counts))
    verdicts = {}
    for r in results:
        verdicts[r["valuation_verdict"]] = verdicts.get(r["valuation_verdict"], 0) + 1
    print("\nValuation (model): " + ", ".join(f"{k} {n}" for k, n in sorted(verdicts.items())))
    ranked = sorted((r for r in results if r["score"] is not None), key=lambda r: -r["score"])[:5]
    print("Top scores: " + ", ".join(f"{r['ticker']} {r['score']:.0f}" for r in ranked))
    changes = sorted((r for r in results if r["phase_changed"]), key=lambda r: (not r["transition"], r["ticker"]))
    print(f"\nPhase changes vs prior fiscal year ({len(changes)}):")
    for r in changes:
        mark = "*" if r["transition"] else " "
        label = f"  [{r['transition']}]" if r["transition"] else ""
        print(f" {mark} {r['ticker']:<7}{r['prior_phase_name']} -> {r['phase_name']}{label}")
    if not changes:
        print("  (none)")
    print(f"\nReport: {(out_dir / 'screener.html').resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify companies by business life-cycle phase.")
    parser.add_argument("--universe", help="ticker file (one per line), 'sp500' or 'nasdaq100'")
    parser.add_argument("--phase", type=int, choices=sorted(PHASES), help="only report companies currently in this phase")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--out", default="lifecycle_report", help="output directory")
    parser.add_argument("--refresh", action="store_true", help="ignore the on-disk cache")
    parser.add_argument("--demo", action="store_true", help="use synthetic data, no network")
    args = parser.parse_args()
    if not args.demo and not args.universe:
        parser.error("--universe is required unless --demo is set")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.demo:
        title = "Demo (synthetic data)"
        results, errors = [], []
        for ticker, raw in demo_universe().items():
            try:
                results.append(analyze_full(raw, valuation.RISK_FREE_FALLBACK))
            except InsufficientData as exc:
                errors.append({"ticker": ticker, "error": f"InsufficientData: {exc}"})
    else:
        title = f"Universe: {Path(args.universe).name}"
        tickers = load_universe(args.universe)
        risk_free = valuation.fetch_risk_free()
        logger.info("Analyzing %d tickers with %d workers (risk-free rate %.2f%%)", len(tickers), args.workers, risk_free * 100)
        results, errors = run(tickers, args.workers, CACHE_DIR, args.refresh, risk_free)

    valuation.apply_relative(results)  # before --phase filtering, so peers come from the whole universe
    if args.phase:
        results = [r for r in results if r["phase"] == args.phase]
        title += f" · phase {args.phase} only"
    results.sort(key=lambda r: (r["phase"], -(r["market_cap"] or 0)))
    errors.sort(key=lambda e: e["ticker"])

    out_dir = Path(args.out)
    write_outputs(results, errors, out_dir, title)
    print_summary(results, errors, out_dir)


if __name__ == "__main__":
    main()
