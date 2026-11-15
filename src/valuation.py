"""Valuation and quality analysis for the life-cycle screener, computed from the numbers alone.

Implements the quantitative parts of the equity-research template: five-year fundamentals and
trends, valuation multiples chosen by asset model (light / heavy / hybrid / financial),
relative valuation against same-industry peers and the company's own history, a three-scenario
DCF with cross-checks, capital allocation, rule-based red flags, a 100-point score, and a model
summary. Every section carries reasoning generated from the figures it shows.

What it deliberately does not do: buy/sell calls, entry-price advice or "would you buy" answers
(those are the reader's decisions), and qualitative judgments — moat, management quality,
competitive dynamics — that financial statements can't support. Missing data is reported as
missing, never estimated. Everything here is model output from the stated assumptions.

Flow: evaluate() runs per ticker (needs only that ticker's data); apply_relative() runs once
over all results (needs peers), then fills in relative valuation, risks, scores and summary.
"""

import html
import logging
import statistics
from datetime import date, timedelta

import yfinance as yf

from statements import (
    ACQUISITION_ROWS,
    BUYBACK_ROWS,
    CAPEX_ROWS,
    CASH_ROWS,
    CURRENT_ASSETS_ROWS,
    CURRENT_LIABILITIES_ROWS,
    DEBT_REPAYMENT_ROWS,
    DEBT_ROWS,
    DEPRECIATION_ROWS,
    DIVIDEND_ROWS,
    EBIT_ROWS,
    EBITDA_ROWS,
    EPS_ROWS,
    EQUITY_ROWS,
    FCF_ROWS,
    GROSS_PROFIT_ROWS,
    INTEREST_ROWS,
    INVESTED_CAPITAL_ROWS,
    NET_INCOME_ROWS,
    OCF_ROWS,
    OPERATING_INCOME_ROWS,
    REVENUE_ROWS,
    SBC_ROWS,
    SHARES_ROWS,
    TAX_RATE_ROWS,
    TOTAL_ASSETS_ROWS,
    WORKING_CAPITAL_ROWS,
    fiscal_years,
    metric,
    value,
)

logger = logging.getLogger(__name__)

# ---- Config ---------------------------------------------------------------------------------
HISTORY_YEARS = 5
EQUITY_RISK_PREMIUM = 0.05  # with a ~5% risk-free rate this makes the DCF conservative vs today's
# market multiples — most large caps come out "Overvalued". Lower it (e.g. 0.04) for a less strict model.
RISK_FREE_FALLBACK = 0.043  # when the 10-year Treasury yield (^TNX) can't be fetched, and in --demo
BETA_RANGE = (0.6, 2.0)  # Yahoo betas outside this are usually noise (thin trading, recent IPO)
COST_OF_DEBT_RANGE = (0.03, 0.10)  # interest expense / total debt, clamped
DEFAULT_TAX_RATE = 0.21
TAX_RATE_RANGE = (0.0, 0.35)
DCF_YEARS = 10
GROWTH_RANGE = (-0.20, 0.40)  # clamp on any single year's revenue growth input
MIN_WACC_SPREAD = 0.02  # WACC must exceed terminal growth by at least this
TERMINAL_RONIC_CAP = 0.30  # return on new capital assumed in the terminal value, capped
# Scenarios: growth_scale stretches the growth path's excess over terminal growth; margin_shift
# is relative to the base operating margin; wacc_shift is added to the base WACC.
SCENARIOS = {
    "bear": {"growth_scale": 0.5, "margin_shift": -0.20, "wacc_shift": 0.01, "terminal_growth": 0.020},
    "base": {"growth_scale": 1.0, "margin_shift": 0.0, "wacc_shift": 0.0, "terminal_growth": 0.025},
    "bull": {"growth_scale": 1.4, "margin_shift": 0.10, "wacc_shift": -0.01, "terminal_growth": 0.030},
}
VALUATION_BAND = 0.15  # base-case DCF within +/- this of the price = "Fairly valued"
MARGIN_OF_SAFETY_TARGET = 0.25  # the summary shows the price that would give this margin of safety
CROSS_CHECK_TOLERANCE = 0.30  # a cross-check this far from the base DCF is flagged as diverging
MIN_PEERS = 3  # fewer same-industry peers in the universe -> fall back to same sector
CATEGORY_WEIGHTS = {"relative_valuation": 0.30, "cash_flow_quality": 0.25,
                    "capital_efficiency": 0.25, "financial_strength": 0.20}
# Score bars: (bad, good) — linear 0-100 in between. Per asset model where it matters; "hybrid"
# uses the midpoint of light and heavy, and "financial" uses heavy unless it has its own entry.
SCORE_BARS = {
    "fcf_margin": {"light": (0.0, 0.25), "heavy": (0.0, 0.10)},
    "fcf_conversion": {"all": (0.3, 1.0)},
    "fcf_consistency": {"all": (0.4, 1.0)},  # share of fiscal years with positive FCF
    "sbc_pct": {"all": (0.15, 0.02)},
    "roic": {"light": (0.05, 0.30), "heavy": (0.03, 0.15)},
    "roe": {"all": (0.0, 0.25), "financial": (0.05, 0.18)},  # banks: ~10% ROE is par, 15%+ is strong
    "roa": {"light": (0.0, 0.15), "heavy": (0.0, 0.08), "financial": (0.0, 0.015)},  # banks: ~1% ROA is good
    "asset_turnover": {"light": (0.3, 1.0), "heavy": (0.2, 1.0)},
    "debt_to_ebitda": {"all": (5.0, 1.0)},
    "interest_coverage": {"all": (1.5, 15.0)},
    "current_ratio": {"all": (0.8, 1.5)},
    "debt_to_equity": {"light": (3.0, 0.5), "heavy": (4.0, 1.0)},
    "premium": {"all": (0.6, -0.6)},  # vs peers / own history: +60% premium -> 0, at par -> 50, -60% -> 100
    "dcf_upside": {"all": (-0.5, 0.5)},
}
SCORE_BANDS = [(85, "Exceptional investment candidate"), (75, "Attractive investment candidate"),
               (65, "Worth monitoring"), (50, "Weak investment proposition"), (0, "Unattractive investment proposition")]
# ---------------------------------------------------------------------------------------------

ASSET_CLASSES = {"Asset-light": "light", "Asset-heavy": "heavy", "Hybrid": "hybrid"}
CLASS_LABELS = {"light": "Asset-light", "heavy": "Asset-heavy", "hybrid": "Hybrid", "financial": "Financial (bank/insurer)"}
MULTIPLES = {  # key: (label, kind) — kind "x" = multiple (lower is cheaper), "%" = yield/return (higher is better)
    "forward_pe": ("Forward P/E", "x"),
    "trailing_pe": ("Trailing P/E", "x"),
    "ev_ebitda": ("EV/EBITDA", "x"),
    "ev_ebit": ("EV/EBIT", "x"),
    "ev_fcf": ("EV/FCF", "x"),
    "price_to_sales": ("Price/Sales", "x"),
    "ev_revenue": ("EV/Revenue", "x"),
    "peg": ("PEG", "x"),
    "fcf_yield": ("FCF yield", "%"),
    "price_to_book": ("Price/Book", "x"),
    "price_normalized": ("Price/normalized earnings", "x"),
    "roic": ("ROIC", "%"),
}
FRAMEWORK = {
    "light": ["forward_pe", "ev_ebitda", "ev_fcf", "fcf_yield", "ev_revenue", "peg"],
    "heavy": ["ev_ebitda", "ev_ebit", "price_to_book", "fcf_yield", "price_normalized"],
    "hybrid": ["forward_pe", "ev_ebitda", "ev_ebit", "ev_fcf", "fcf_yield", "price_to_book", "peg"],
    "financial": ["trailing_pe", "forward_pe", "price_to_book"],
}
FRAMEWORK_FOCUS = {
    "light": "revenue scalability, cash conversion, capex needs and how durable the margins are",
    "heavy": "asset utilization, maintenance vs growth capex, depreciation, debt load and cyclicality",
    "hybrid": "both lenses — cash conversion and margins, plus capex intensity and debt load",
    "financial": "earnings and book value; EV-based multiples and FCF don't apply because debt is operating capital",
}
RELATIVE_KEYS = ["forward_pe", "trailing_pe", "ev_ebitda", "ev_ebit", "ev_fcf", "price_to_sales", "peg", "fcf_yield", "roic"]
HISTORY_KEYS = ["trailing_pe", "ev_ebitda", "price_to_sales"]
EV_KEYS = {"ev_ebitda", "ev_ebit", "ev_fcf", "ev_revenue"}


# ---- Small helpers --------------------------------------------------------------------------


def _ratio(num, den):
    return num / den if num is not None and den is not None and den > 0 else None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _positive(v):
    return v if v is not None and v > 0 else None


def lin(v, bad, good):
    """0-100 linear score: `bad` -> 0, `good` -> 100, clamped. Works for either direction."""
    if v is None:
        return None
    return round(_clamp((v - bad) / (good - bad), 0.0, 1.0) * 100, 1)


def _bar(name, asset_class):
    bars = SCORE_BARS[name]
    if asset_class in bars:
        return bars[asset_class]
    if "all" in bars:
        return bars["all"]
    if asset_class == "hybrid":
        return tuple((a + b) / 2 for a, b in zip(bars["light"], bars["heavy"]))
    return bars["heavy" if asset_class == "financial" else asset_class]


def money(v):
    if v is None:
        return "n/a"
    sign, a = ("-" if v < 0 else ""), abs(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            n = a / div
            return f"{sign}${n:.0f}{suffix}" if n >= 10 else f"{sign}${n:.1f}{suffix}"
    return f"{sign}${a:,.0f}"


def pct(v, signed=False):
    if v is None:
        return "n/a"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


def fmt(key, v):
    if v is None:
        return "n/a"
    if MULTIPLES.get(key, ("", "x"))[1] == "%":
        return pct(v)
    return f"{v:.1f}x"


def per_share(v):
    """Per-share values to the dollar — more precision would be false precision. A negative DCF
    value means net debt exceeds the value of the operations."""
    if v is None:
        return "n/a"
    return "below $0" if v < 0 else f"${v:,.0f}"


def mos_text(v):
    """Margin of safety; negative means the price is above the value, i.e. there is none."""
    if v is None:
        return "n/a"
    return pct(v) if v >= 0 else "none (price above value)"


def fetch_risk_free() -> float:
    """10-year Treasury yield from ^TNX (quoted in percent), or the config fallback."""
    try:
        close = yf.Ticker("^TNX").history(period="5d")["Close"].dropna()
        if not close.empty:
            return float(close.iloc[-1]) / 100
    except Exception as exc:
        logger.warning("Couldn't fetch the 10-year Treasury yield (%s); using %.1f%%", exc, RISK_FREE_FALLBACK * 100)
    return RISK_FREE_FALLBACK


# ---- Per-ticker: fundamentals ----------------------------------------------------------------


def _fiscal_year(raw, d):
    p = raw["annual"][d]
    revenue = value(p, REVENUE_ROWS)
    operating_income = value(p, OPERATING_INCOME_ROWS)
    ebit = value(p, EBIT_ROWS)
    ebit = operating_income if ebit is None else ebit
    dna = value(p, DEPRECIATION_ROWS)
    ebitda = value(p, EBITDA_ROWS)
    if ebitda is None and ebit is not None and dna is not None:
        ebitda = ebit + abs(dna)
    ocf, capex = value(p, OCF_ROWS), value(p, CAPEX_ROWS)
    fcf = value(p, FCF_ROWS)
    if fcf is None and ocf is not None and capex is not None:
        fcf = ocf - abs(capex)
    acquisitions = value(p, ACQUISITION_ROWS)
    tax_rate = value(p, TAX_RATE_ROWS)
    row = {
        "fy": f"FY{d[:4]}", "date": d, "revenue": revenue, "gross_profit": value(p, GROSS_PROFIT_ROWS),
        "operating_income": operating_income, "ebit": ebit, "ebitda": ebitda, "net_income": value(p, NET_INCOME_ROWS),
        "eps": value(p, EPS_ROWS), "shares": value(p, SHARES_ROWS), "ocf": ocf,
        "capex": abs(capex) if capex is not None else None, "dna": abs(dna) if dna is not None else None, "fcf": fcf,
        "sbc": value(p, SBC_ROWS), "dividends": abs(value(p, DIVIDEND_ROWS) or 0), "buybacks": abs(value(p, BUYBACK_ROWS) or 0),
        "debt_repaid": abs(value(p, DEBT_REPAYMENT_ROWS) or 0),
        "acquisitions": -acquisitions if acquisitions is not None and acquisitions < 0 else 0,
        "interest": abs(value(p, INTEREST_ROWS)) if value(p, INTEREST_ROWS) is not None else None,
        "tax_rate": _clamp(tax_rate, *TAX_RATE_RANGE) if tax_rate is not None else DEFAULT_TAX_RATE,
        "debt": value(p, DEBT_ROWS), "cash": value(p, CASH_ROWS), "equity": value(p, EQUITY_ROWS),
        "total_assets": value(p, TOTAL_ASSETS_ROWS), "invested_capital": value(p, INVESTED_CAPITAL_ROWS),
        "current_assets": value(p, CURRENT_ASSETS_ROWS), "current_liabilities": value(p, CURRENT_LIABILITIES_ROWS),
        "working_capital": value(p, WORKING_CAPITAL_ROWS),
    }
    rev, ni = revenue, row["net_income"]
    row.update({
        "gross_margin": _ratio(row["gross_profit"], rev), "op_margin": _ratio(operating_income, rev),
        "net_margin": _ratio(ni, rev), "ebitda_margin": _ratio(ebitda, rev), "fcf_margin": _ratio(fcf, rev),
        "roic": _ratio(ebit * (1 - row["tax_rate"]) if ebit is not None else None, row["invested_capital"]),
        "roe": _ratio(ni, row["equity"]), "roa": _ratio(ni, row["total_assets"]),
        "asset_turnover": _ratio(rev, row["total_assets"]), "capex_pct": _ratio(row["capex"], rev),
        "ic_to_revenue": _ratio(row["invested_capital"], rev),
        "net_debt": row["debt"] - (row["cash"] or 0) if row["debt"] is not None else None,
        "debt_to_ebitda": _ratio(row["debt"], _positive(ebitda)),
        "debt_to_equity": _ratio(row["debt"], row["equity"]),
        "interest_coverage": _ratio(ebit, row["interest"]) if row["interest"] else None,
        "current_ratio": _ratio(row["current_assets"], row["current_liabilities"]),
    })
    return row


def _cagr(newest, oldest, years):
    if newest is None or oldest is None or oldest <= 0 or newest <= 0 or years <= 0:
        return None
    return (newest / oldest) ** (1 / years) - 1


def _yoy(newer, older):
    return (newer - older) / abs(older) if newer is not None and older else None


def _growth(hist):
    """YoY (latest fiscal year) and CAGR (over the available years) for the template's growth set."""
    out = {}
    n = len(hist) - 1
    for key, label in (("revenue", "Revenue"), ("eps", "EPS"), ("operating_income", "Operating income"), ("fcf", "Free cash flow")):
        series = [h[key] for h in hist]
        out[key] = {
            "label": label,
            "yoy": _yoy(series[0], series[1]) if n >= 1 else None,
            "cagr": _cagr(series[0], series[-1], n) if n >= 1 else None,
            "years": n,
        }
    return out


TREND_METRICS = [  # key, label, higher is better?, unit
    ("gross_margin", "Gross margin", True, "pp"), ("op_margin", "Operating margin", True, "pp"),
    ("net_margin", "Net margin", True, "pp"), ("fcf_margin", "FCF margin", True, "pp"),
    ("roic", "ROIC", True, "pp"), ("roe", "ROE", True, "pp"), ("asset_turnover", "Asset turnover", True, "x"),
    ("debt_to_ebitda", "Debt/EBITDA", False, "x"), ("interest_coverage", "Interest coverage", True, "x"),
    ("current_ratio", "Current ratio", True, "x"),
]


def _trends(hist):
    """Latest fiscal year vs the average of the up-to-three before it."""
    improving, deteriorating = [], []
    for key, label, higher_better, unit in TREND_METRICS:
        latest = hist[0][key]
        base = _mean([h[key] for h in hist[1:4]])
        if latest is None or base is None:
            continue
        if unit == "pp":
            delta = latest - base
            moved = abs(delta) >= 0.01
            text = f"{label} {'up' if delta > 0 else 'down'} {abs(delta) * 100:.1f} pp ({pct(latest)} vs {pct(base)} prior avg.)"
        else:
            delta = _yoy(latest, base) or 0
            moved = abs(delta) >= 0.10
            text = f"{label} {'up' if delta > 0 else 'down'} {abs(delta) * 100:.0f}% ({latest:.1f}x vs {base:.1f}x prior avg.)"
        if moved:
            (improving if (delta > 0) == higher_better else deteriorating).append(text)
    return improving, deteriorating


def _ltm_flows(raw, hist0):
    flows, bases = {}, {}
    for key, rows in (("revenue", REVENUE_ROWS), ("operating_income", OPERATING_INCOME_ROWS), ("ebit", EBIT_ROWS),
                      ("ebitda", EBITDA_ROWS), ("net_income", NET_INCOME_ROWS), ("ocf", OCF_ROWS),
                      ("capex", CAPEX_ROWS), ("sbc", SBC_ROWS), ("dividends", DIVIDEND_ROWS),
                      ("buybacks", BUYBACK_ROWS), ("interest", INTEREST_ROWS), ("dna", DEPRECIATION_ROWS)):
        flows[key], bases[key] = metric(raw, rows)
    for key in ("capex", "dividends", "buybacks", "interest", "dna"):
        if flows[key] is not None:
            flows[key] = abs(flows[key])
    if flows["ebit"] is None:
        flows["ebit"] = flows["operating_income"]
    if flows["ebitda"] is None and flows["ebit"] is not None and flows["dna"] is not None:
        flows["ebitda"] = flows["ebit"] + flows["dna"]
    flows["fcf"] = (flows["ocf"] - flows["capex"]) if flows["ocf"] is not None and flows["capex"] is not None else hist0["fcf"]
    return flows, bases["revenue"]


def _market(raw, result, hist0):
    m = raw.get("market", {})
    price, shares = m.get("price"), m.get("shares")
    market_cap = result.get("market_cap") or (price * shares if price and shares else None)
    debt = m.get("total_debt") if m.get("total_debt") is not None else (hist0["debt"] or 0)
    cash = m.get("total_cash") if m.get("total_cash") is not None else (hist0["cash"] or 0)
    return {
        "price": price, "shares": shares, "market_cap": market_cap, "debt": debt, "cash": cash,
        "ev": market_cap + debt - cash if market_cap else None, "beta": m.get("beta"),
        "trailing_eps": m.get("trailing_eps"), "forward_eps": m.get("forward_eps"), "forward_pe": m.get("forward_pe"),
        "peg": m.get("peg"), "price_to_book": m.get("price_to_book"), "insider_pct": m.get("insider_pct"),
        "currency": m.get("currency") or "USD",
    }


def _multiples(mk, ltm, hist, asset_class):
    mcap, ev, price = mk["market_cap"], mk["ev"], mk["price"]
    eps = _positive(mk["trailing_eps"])
    normalized_margin = _median([h["net_margin"] for h in hist])
    normalized_income = normalized_margin * ltm["revenue"] if normalized_margin is not None and ltm["revenue"] else None
    out = {
        "trailing_pe": (price / eps if price and eps else _ratio(mcap, _positive(ltm["net_income"]))),
        "forward_pe": _positive(mk["forward_pe"]) or (_ratio(price, _positive(mk["forward_eps"]))),
        "ev_ebitda": _ratio(ev, _positive(ltm["ebitda"])),
        "ev_ebit": _ratio(ev, _positive(ltm["ebit"])),
        "ev_fcf": _ratio(ev, _positive(ltm["fcf"])),
        "price_to_sales": _ratio(mcap, ltm["revenue"]),
        "ev_revenue": _ratio(ev, ltm["revenue"]),
        "peg": _positive(mk["peg"]),
        "fcf_yield": ltm["fcf"] / mcap if mcap and ltm["fcf"] is not None else None,
        "price_to_book": _positive(mk["price_to_book"]) or _ratio(mcap, _positive(hist[0]["equity"])),
        "price_normalized": _ratio(mcap, _positive(normalized_income)),
        "roic": hist[0]["roic"],
    }
    if asset_class == "financial":  # EV and FCF lose meaning when debt/deposits are the raw material
        for key in (*EV_KEYS, "fcf_yield", "roic"):
            out[key] = None
    return out


def _own_history(raw, hist, asset_class):
    """P/E, EV/EBITDA and P/S at each fiscal year-end, from the month-end close and that year's
    statements. Needs at least three years to call it a range."""
    prices = raw.get("prices", {})  # monthly bars keyed by month start; the close is the month-end close
    points = {key: [] for key in HISTORY_KEYS}
    for h in hist:
        fy_end = date.fromisoformat(h["date"])
        bars = [d for d in prices if fy_end - timedelta(days=35) <= date.fromisoformat(d) <= fy_end]
        if not bars:
            continue
        close = prices[max(bars)]
        mcap = close * h["shares"] if h["shares"] else None
        points["trailing_pe"].append(_ratio(close, _positive(h["eps"])))
        points["price_to_sales"].append(_ratio(mcap, h["revenue"]))
        if asset_class != "financial" and mcap is not None and h["debt"] is not None:
            ev = mcap + h["debt"] - (h["cash"] or 0)
            points["ev_ebitda"].append(_ratio(ev, _positive(h["ebitda"])))
    out = {}
    for key, values in points.items():
        values = [v for v in values if v is not None]
        if len(values) >= 3:
            out[key] = {"min": min(values), "median": statistics.median(values), "max": max(values), "n": len(values)}
    return out


def _wacc(mk, hist0, ltm, risk_free, asset_class):
    beta = _clamp(mk["beta"], *BETA_RANGE) if mk["beta"] is not None else 1.0
    cost_of_equity = risk_free + beta * EQUITY_RISK_PREMIUM
    if asset_class == "financial" or not mk["market_cap"]:
        return {"wacc": cost_of_equity, "cost_of_equity": cost_of_equity, "beta": beta, "beta_reported": mk["beta"],
                "cost_of_debt": None, "debt_weight": 0.0, "risk_free": risk_free}
    debt = mk["debt"] or 0
    cost_of_debt = _clamp(ltm["interest"] / debt, *COST_OF_DEBT_RANGE) if debt > 0 and ltm["interest"] else risk_free + 0.015
    cost_of_debt = _clamp(cost_of_debt, *COST_OF_DEBT_RANGE)
    debt_weight = debt / (mk["market_cap"] + debt)
    wacc = (1 - debt_weight) * cost_of_equity + debt_weight * cost_of_debt * (1 - hist0["tax_rate"])
    return {"wacc": wacc, "cost_of_equity": cost_of_equity, "beta": beta, "beta_reported": mk["beta"],
            "cost_of_debt": cost_of_debt, "debt_weight": debt_weight, "risk_free": risk_free}


def _growth_inputs(raw, growth):
    est = raw.get("estimates", {})
    g1, g2 = est.get("0y"), est.get("+1y")
    if g1 is not None:
        return g1, (g2 if g2 is not None else g1), "analyst revenue estimates for the current and next fiscal year"
    cagr = growth["revenue"]["cagr"]
    if cagr is not None:
        return cagr, cagr, f"the historical {growth['revenue']['years']}-year revenue CAGR, as there are no analyst estimates"
    return None, None, ""


def dcf_value(revenue0, margin, tax_rate, capex_pct, dna_pct, nwc_pct, growth_path, terminal_growth, wacc,
              terminal_ronic, net_debt, shares):
    """Free cash flow to the firm, discounted at WACC. Explicit years reinvest net capex
    (capex above depreciation, floored at 0) plus working capital on incremental revenue; the
    terminal year reinvests g / RONIC of NOPAT so growth isn't free. Returns a dict of results."""
    revenue, pv = revenue0, 0.0
    for t, g in enumerate(growth_path, start=1):
        prev, revenue = revenue, revenue * (1 + g)
        nopat = revenue * margin * (1 - tax_rate)
        reinvestment = max(capex_pct - dna_pct, 0) * revenue + nwc_pct * (revenue - prev)
        pv += (nopat - reinvestment) / (1 + wacc) ** t
    n = len(growth_path)
    nopat_next = revenue * (1 + terminal_growth) * margin * (1 - tax_rate)
    terminal_fcff = nopat_next * (1 - terminal_growth / terminal_ronic)
    pv_terminal = terminal_fcff / (wacc - terminal_growth) / (1 + wacc) ** n
    ev = pv + pv_terminal
    equity = ev - net_debt
    return {"ev": ev, "equity": equity, "per_share": equity / shares if shares else None,
            "terminal_share": pv_terminal / ev if ev > 0 else None}


def _growth_path(g1, g2, terminal_growth, scale):
    def scaled(g):
        return _clamp(terminal_growth + (g - terminal_growth) * scale, *GROWTH_RANGE)
    s1, s2 = scaled(g1), scaled(g2)
    path = [s1, s2]
    for t in range(3, DCF_YEARS + 1):
        path.append(s2 + (terminal_growth - s2) * (t - 2) / (DCF_YEARS - 2))
    return path


def _dcf(raw, hist, ltm, mk, wacc_info, growth, asset_class):
    if asset_class == "financial":
        return {"available": False, "reason": "A free-cash-flow DCF isn't meaningful for banks and insurers — debt and "
                "deposits are their raw material, not financing. Valued on P/E and P/B instead."}
    if not mk["price"] or not mk["shares"] or not ltm["revenue"]:
        return {"available": False, "reason": "Price, share count or revenue is missing from the data."}
    margin_ltm = _ratio(ltm["operating_income"], ltm["revenue"])
    margin_hist = _median([h["op_margin"] for h in hist])
    base_margin = _mean([margin_ltm, margin_hist])
    if base_margin is None:
        # Deliberately no EBIT fallback: companies without an operating-income line are mostly
        # insurers and conglomerates whose EBIT includes investment gains (Berkshire), which
        # would be projected forward as if it were operating profit.
        return {"available": False, "reason": "Operating income isn't reported (common for insurers and conglomerates, "
                "whose EBIT includes investment gains), so there's no operating margin to project."}
    if base_margin <= 0:
        return {"available": False, "reason": "Operating margin is negative, so a DCF would rest entirely on an assumed "
                "path to profitability that the data can't support."}
    g1, g2, growth_source = _growth_inputs(raw, growth)
    if g1 is None:
        return {"available": False, "reason": "No analyst estimates and not enough revenue history to set a growth path."}
    recent = hist[:3]
    capex_pct = _mean([h["capex_pct"] for h in recent]) or 0
    dna_pct = _mean([_ratio(h["dna"], h["revenue"]) for h in recent]) or 0
    h0 = hist[0]
    nwc = (h0["working_capital"] - (h0["cash"] or 0)) if h0["working_capital"] is not None else None
    nwc_pct = _clamp(nwc / h0["revenue"], -0.2, 0.3) if nwc is not None and h0["revenue"] else 0.0
    roic = h0["roic"]
    net_debt = (mk["debt"] or 0) - (mk["cash"] or 0)
    scenarios = {}
    for name, s in SCENARIOS.items():
        tg = s["terminal_growth"]
        wacc = max(wacc_info["wacc"] + s["wacc_shift"], tg + MIN_WACC_SPREAD)
        margin = base_margin * (1 + s["margin_shift"])
        path = _growth_path(g1, g2, tg, s["growth_scale"])
        ronic = _clamp(roic, wacc, TERMINAL_RONIC_CAP) if roic is not None else wacc
        res = dcf_value(ltm["revenue"], margin, h0["tax_rate"], capex_pct, dna_pct, nwc_pct, path, tg, wacc, ronic,
                        net_debt, mk["shares"])
        ps = res["per_share"]
        res.update({
            "growth_y1": path[0], "growth_y2": path[1], "terminal_growth": tg, "margin": margin, "wacc": wacc,
            "upside": ps / mk["price"] - 1 if ps is not None else None,
            "margin_of_safety": (ps - mk["price"]) / ps if ps and ps > 0 else None,
        })
        scenarios[name] = res
    return {
        "available": True, "scenarios": scenarios, "growth_source": growth_source, "g1": g1, "g2": g2,
        "base_margin": base_margin, "margin_ltm": margin_ltm, "margin_hist": margin_hist, "tax_rate": h0["tax_rate"],
        "capex_pct": capex_pct, "dna_pct": dna_pct, "nwc_pct": nwc_pct, "net_debt": net_debt,
        "revenue0": ltm["revenue"], "terminal_ronic": roic,
    }


def _capital_allocation(hist, ltm, mk, wacc):
    fcf, ni = ltm["fcf"], ltm["net_income"]
    shares = [h["shares"] for h in hist if h["shares"]]
    dilution = _cagr(shares[0], shares[-1], len(shares) - 1) if len(shares) >= 2 else None
    out = {
        "fcf": fcf, "fcf_yield": fcf / mk["market_cap"] if fcf is not None and mk["market_cap"] else None,
        "fcf_conversion": _ratio(fcf, _positive(ni)),
        "dividends": ltm["dividends"] or 0, "buybacks": ltm["buybacks"] or 0,
        "dividend_payout_fcf": _ratio(ltm["dividends"] or 0, _positive(fcf)) if ltm["dividends"] else None,
        "dividend_yield": (ltm["dividends"] or 0) / mk["market_cap"] if mk["market_cap"] else None,
        "sbc_pct": _ratio(ltm["sbc"], ltm["revenue"]), "sbc_of_fcf": _ratio(ltm["sbc"], _positive(fcf)),
        "dilution": dilution, "debt_repaid": hist[0]["debt_repaid"],
        "acquisitions": sum(h["acquisitions"] for h in hist), "acquisition_years": len(hist),
        "reinvestment_rate": _ratio(ltm["capex"], _positive(ltm["ocf"])),
        "roic_minus_wacc": hist[0]["roic"] - wacc if hist[0]["roic"] is not None else None,
    }
    good, bad = [], []
    if out["roic_minus_wacc"] is not None:
        (good if out["roic_minus_wacc"] > 0 else bad).append(
            f"ROIC {pct(hist[0]['roic'])} vs WACC {pct(wacc)}: reinvestment {'creates' if out['roic_minus_wacc'] > 0 else 'destroys'} value")
    if dilution is not None:
        if dilution < -0.005:
            good.append(f"Share count shrinking {pct(-dilution)} a year")
        elif dilution > 0.02:
            bad.append(f"Share count growing {pct(dilution)} a year (dilution)")
    if out["dividend_payout_fcf"] is not None:
        payout = out["dividend_payout_fcf"]
        (good if payout <= 0.7 else bad).append(f"Dividends take {pct(payout)} of FCF ({'covered' if payout <= 0.7 else 'stretched'})")
    elif out["dividends"] and (fcf is None or fcf <= 0):
        bad.append("Dividends paid while free cash flow is negative")
    if out["sbc_pct"] is not None and out["sbc_pct"] > 0.08:
        bad.append(f"Stock-based compensation is {pct(out['sbc_pct'])} of revenue")
    if out["acquisitions"] and ltm["revenue"] and out["acquisitions"] / len(hist) > 0.1 * ltm["revenue"] and \
            out["roic_minus_wacc"] is not None and out["roic_minus_wacc"] < 0:
        bad.append("Heavy acquisition spending while ROIC trails WACC")
    out["good"], out["bad"] = good, bad
    value_destroying = out["roic_minus_wacc"] is not None and out["roic_minus_wacc"] < 0
    if len(good) > len(bad) and not value_destroying:
        out["verdict"] = "Effective"
    elif len(bad) > len(good):
        out["verdict"] = "Weak"
    else:
        out["verdict"] = "Mixed"
    return out


def _business(raw, hist, growth):
    m = raw.get("market", {})
    summary = (m.get("summary") or "").strip()
    if len(summary) > 700:
        summary = summary[:700].rsplit(" ", 1)[0] + "…"
    yoys = [_yoy(a["revenue"], b["revenue"]) for a, b in zip(hist, hist[1:])]
    yoys = [g for g in yoys if g is not None]
    declines = sum(g < 0 for g in yoys)
    if len(yoys) >= 3 and (statistics.pstdev(yoys) > 0.15 or declines >= 2):
        model = "Cyclical or volatile — revenue growth swings a lot year to year; judge on normalized earnings"
    elif yoys and min(yoys) > 0.10:
        model = "Consistent grower — double-digit revenue growth every year shown"
    else:
        model = "Steady — revenue growth has been relatively stable"
    return {"summary": summary, "revenue_path": [(h["fy"], h["revenue"]) for h in reversed(hist)],
            "model": model, "cagr": growth["revenue"]["cagr"], "years": growth["revenue"]["years"]}


def _competitive(hist, wacc, mk):
    roics = [h["roic"] for h in hist if h["roic"] is not None]
    above = sum(r > wacc for r in roics)
    margins = [h["gross_margin"] for h in hist if h["gross_margin"] is not None]
    gm_std = statistics.pstdev(margins) if len(margins) >= 3 else None
    notes = []
    if roics:
        if above == len(roics):
            notes.append(f"ROIC above the cost of capital in all {len(roics)} years shown — consistent with a durable "
                         f"advantage (confirm qualitatively).")
        elif above == 0:
            notes.append(f"ROIC below the cost of capital in all {len(roics)} years shown — no sign of a moat in the returns.")
        else:
            notes.append(f"ROIC above the cost of capital in {above} of {len(roics)} years — an inconsistent advantage.")
    if gm_std is not None:
        notes.append(f"Gross margin {'stable' if gm_std < 0.02 else 'variable'} (±{gm_std * 100:.1f} pp across "
                     f"{len(margins)} years) — {'a sign of pricing power' if gm_std < 0.02 else 'pricing power not evident'}.")
    return {"roic_above_wacc": above, "roic_years": len(roics), "gross_margin_std": gm_std,
            "insider_pct": mk["insider_pct"], "notes": notes}


def evaluate(raw, result, risk_free):
    """Everything that needs only this ticker's data. apply_relative() completes it."""
    years = fiscal_years(raw)[:HISTORY_YEARS]
    hist = [_fiscal_year(raw, d) for d in years]
    asset_class = "financial" if result.get("capex_pct") is None else ASSET_CLASSES[result["asset_model"]]
    ltm, ltm_basis = _ltm_flows(raw, hist[0])
    mk = _market(raw, result, hist[0])
    growth = _growth(hist)
    improving, deteriorating = _trends(hist)
    wacc_info = _wacc(mk, hist[0], ltm, risk_free, asset_class)
    return {
        "asset_class": asset_class, "hist": hist, "ltm": ltm, "ltm_basis": ltm_basis, "market": mk, "growth": growth,
        "improving": improving, "deteriorating": deteriorating, "wacc": wacc_info,
        "multiples": _multiples(mk, ltm, hist, asset_class), "own_history": _own_history(raw, hist, asset_class),
        "dcf": _dcf(raw, hist, ltm, mk, wacc_info, growth, asset_class),
        "capital": _capital_allocation(hist, ltm, mk, wacc_info["wacc"]),
        "business": _business(raw, hist, growth),
        "competitive": _competitive(hist, wacc_info["wacc"], mk),
    }


# ---- Cross-ticker: peers, risks, scores, summary --------------------------------------------


def _peers(r, results):
    for field in ("industry", "sector"):
        group = [o for o in results if o is not r and r.get(field) and o.get(field) == r.get(field)]
        if len(group) >= MIN_PEERS:
            return group, f"same {field} ({r[field]})"
    group = [o for o in results if o is not r and r.get("sector") and o.get("sector") == r.get("sector")]
    if group:
        return group, f"same sector ({r['sector']}) — only {len(group)} in the universe, too few for a reliable comparison"
    return [], "no same-industry or same-sector companies in the screened universe"


def _premium(key, company, peer):
    """Positive = more expensive than the benchmark. Yields invert: a lower yield is pricier."""
    if company is None or peer is None or peer <= 0:
        return None
    if MULTIPLES[key][1] == "%":
        return peer / company - 1 if company > 0 else None
    return company / peer - 1


def _relative(r, results):
    v = r["valuation"]
    peers, basis = _peers(r, results)
    rows = []
    for key in RELATIVE_KEYS:
        company = v["multiples"][key]
        peer_median = _median([p["valuation"]["multiples"][key] for p in peers])
        own = v["own_history"].get(key)
        rows.append({"key": key, "company": company, "peer_median": peer_median,
                     "premium": None if key == "roic" else _premium(key, company, peer_median),
                     "own": own, "own_premium": _premium(key, company, own["median"]) if own else None})
    framework = FRAMEWORK[v["asset_class"]]
    in_framework = [row for row in rows if row["key"] in framework and row["premium"] is not None]
    # Median, not mean: a barely-positive denominator (EBIT just above zero) makes one multiple
    # explode, and it shouldn't drown out the others.
    avg_premium = _median([row["premium"] for row in in_framework])
    peer_quality = {k: _median([p["valuation"]["hist"][0][k] for p in peers]) for k in ("roic", "fcf_margin")}
    peer_growth = _median([p["valuation"]["growth"]["revenue"]["yoy"] for p in peers])
    better = sum(1 for mine, theirs in ((v["hist"][0]["roic"], peer_quality["roic"]),
                                        (v["hist"][0]["fcf_margin"], peer_quality["fcf_margin"]),
                                        (v["growth"]["revenue"]["yoy"], peer_growth))
                 if mine is not None and theirs is not None and mine > theirs)
    notes = []
    if avg_premium is None:
        notes.append("Not enough comparable multiples to judge premium or discount.")
    else:
        stance = f"a {pct(abs(avg_premium))} {'premium' if avg_premium > 0 else 'discount'}"
        notes.append(f"On its framework multiples (median across them) it trades at {stance} to the peer median ({basis}).")
        if avg_premium > 1.0:
            notes.append(f"Better than peers on {better} of 3 (ROIC, FCF margin, revenue growth), but a premium this "
                         "large prices in growth far beyond any peer — no current fundamental edge accounts for it.")
        elif avg_premium > 0.10:
            notes.append(f"Better than peers on {better} of 3 (ROIC, FCF margin, revenue growth) — premium "
                         + ("looks supported." if better >= 2 else "is not clearly supported by fundamentals."))
        elif avg_premium < -0.10:
            notes.append(f"Better than peers on {better} of 3 (ROIC, FCF margin, revenue growth) — "
                         + ("a discount despite stronger fundamentals: a possible mispricing worth researching."
                            if better >= 2 else "the discount looks earned."))
    own_premiums = [row["own_premium"] for row in rows if row["own_premium"] is not None]
    if own_premiums:
        avg_own = _median(own_premiums)
        notes.append(f"Versus its own fiscal-year-end history it's {pct(abs(avg_own))} "
                     f"{'above' if avg_own > 0 else 'below'} its median multiple.")
    return {"rows": rows, "peers": [p["ticker"] for p in peers], "basis": basis, "avg_premium": avg_premium,
            "better_than_peers": better, "notes": notes, "peer_count": len(peers)}


def _cross_checks(r, relative):
    v = r["valuation"]
    mk, ltm = v["market"], v["ltm"]
    shares = mk["shares"]
    out = []
    rows = {row["key"]: row for row in relative["rows"]}
    if v["asset_class"] != "financial" and shares and rows["ev_ebitda"]["peer_median"] and _positive(ltm["ebitda"]):
        equity = rows["ev_ebitda"]["peer_median"] * ltm["ebitda"] - ((mk["debt"] or 0) - (mk["cash"] or 0))
        out.append(("Peer median EV/EBITDA × LTM EBITDA", equity / shares))
    if rows["forward_pe"]["peer_median"] and _positive(mk["forward_eps"]):
        out.append(("Peer median forward P/E × forward EPS", rows["forward_pe"]["peer_median"] * mk["forward_eps"]))
    margin = _median([h["net_margin"] for h in v["hist"]])
    pe = (v["own_history"].get("trailing_pe") or {}).get("median") or rows["trailing_pe"]["peer_median"]
    if margin and margin > 0 and pe and ltm["revenue"] and shares:
        source = "own median" if v["own_history"].get("trailing_pe") else "peer median"
        out.append((f"Normalized earnings (median net margin {pct(margin)}) × {source} P/E {pe:.1f}x",
                    margin * ltm["revenue"] * pe / shares))
    return out


def _risks(r, relative):
    v = r["valuation"]
    h0, cap, ltm = v["hist"][0], v["capital"], v["ltm"]
    flags = []  # (severity 1-3, title, detail)

    def add(severity, title, detail):
        flags.append((severity, title, detail))

    if h0["debt_to_ebitda"] is not None and h0["debt_to_ebitda"] > 3 and v["asset_class"] != "financial":
        add(3 if h0["debt_to_ebitda"] > 4 else 2, "Leverage", f"Debt is {h0['debt_to_ebitda']:.1f}x EBITDA — limited room if earnings fall.")
    coverage = h0["interest_coverage"]
    if coverage is not None and coverage < 0:
        add(3, "Interest burden", "EBIT is negative, so operating profit doesn't cover interest at all.")
    elif coverage is not None and coverage < 4:
        add(3 if coverage < 2 else 2, "Interest burden", f"EBIT covers interest only {coverage:.1f}x.")
    if h0["current_ratio"] is not None and h0["current_ratio"] < 1:
        add(1, "Liquidity", f"Current ratio {h0['current_ratio']:.2f} — current liabilities exceed current assets.")
    if ltm["fcf"] is not None and ltm["fcf"] < 0 and v["asset_class"] != "financial":
        add(3, "Cash burn", f"Free cash flow is negative ({money(ltm['fcf'])}, {v['ltm_basis']}).")
    margin_trend = [t for t in v["deteriorating"] if t.startswith("Operating margin")]
    if margin_trend:
        add(2, "Declining margins", margin_trend[0] + ".")
    g = v["growth"]["revenue"]["yoy"]
    if g is not None and g < 0:
        add(2, "Shrinking revenue", f"Revenue fell {pct(abs(g))} in the latest fiscal year.")
    if cap["dilution"] is not None and cap["dilution"] > 0.02:
        add(2, "Dilution", f"Share count growing {pct(cap['dilution'])} a year.")
    if cap["sbc_pct"] is not None and cap["sbc_pct"] > 0.10:
        add(2, "Stock-based compensation", f"SBC is {pct(cap['sbc_pct'])} of revenue — a real cost the FCF figure leaves out.")
    if cap["fcf_conversion"] is not None and cap["fcf_conversion"] < 0.5:
        add(2, "Earnings quality", f"Only {pct(cap['fcf_conversion'])} of net income converts to free cash flow — "
            f"check accruals and capex needs.")
    if "Cyclical" in v["business"]["model"]:
        add(2, "Cyclicality", "Revenue swings year to year; current earnings may not be representative.")
    if relative["avg_premium"] is not None and relative["avg_premium"] > 0.5:
        add(2, "Valuation", f"Trades {pct(relative['avg_premium'])} above peers on its framework multiples — "
            f"vulnerable to a de-rating.")
    if v["dcf"]["available"]:
        base, bear = v["dcf"]["scenarios"]["base"], v["dcf"]["scenarios"]["bear"]
        if bear["upside"] is not None and bear["upside"] < -0.30:
            add(2, "Downside case", f"The bear-case DCF ({per_share(bear['per_share'])}) is {pct(abs(bear['upside']))} below the price.")
        if base["terminal_share"] is not None and base["terminal_share"] > 0.80:
            add(1, "Long-dated value", f"{pct(base['terminal_share'])} of the base-case value is in the terminal "
                f"value — highly sensitive to long-run assumptions.")
    if r.get("phase") == 5:
        add(2, "Life-cycle decline", "Classified in the Decline phase (revenue not growing).")
    flags.sort(key=lambda f: -f[0])
    return flags


def _category(components):
    scored = [(label, s, detail) for label, s, detail in components if s is not None]
    return {"score": round(_mean([s for _, s, _ in scored]), 1) if scored else None, "components": components}


def _scores(r, relative):
    v = r["valuation"]
    ac, h0, cap = v["asset_class"], v["hist"][0], v["capital"]
    fin = ac == "financial"
    fcf_years = [h["fcf"] for h in v["hist"] if h["fcf"] is not None]

    relative_components = []
    for row in relative["rows"]:
        if row["key"] in FRAMEWORK[ac]:
            label = MULTIPLES[row["key"]][0]
            relative_components.append((f"{label} vs peers", lin(row["premium"], *_bar("premium", ac)),
                                        pct(row["premium"], True) if row["premium"] is not None else "n/a"))
            if row["own_premium"] is not None:
                relative_components.append((f"{label} vs own history", lin(row["own_premium"], *_bar("premium", ac)),
                                            pct(row["own_premium"], True)))
    if v["dcf"]["available"]:
        up = v["dcf"]["scenarios"]["base"]["upside"]
        relative_components.append(("Base-case DCF vs price", lin(up, *_bar("dcf_upside", ac)), pct(up, True)))

    cash_components = [] if fin else [
        ("FCF margin", lin(h0["fcf_margin"], *_bar("fcf_margin", ac)), pct(h0["fcf_margin"])),
        ("FCF conversion", lin(cap["fcf_conversion"], *_bar("fcf_conversion", ac)), pct(cap["fcf_conversion"])),
        ("Years with positive FCF", lin(sum(f > 0 for f in fcf_years) / len(fcf_years) if fcf_years else None,
                                        *_bar("fcf_consistency", ac)),
         f"{sum(f > 0 for f in fcf_years)} of {len(fcf_years)}" if fcf_years else "n/a"),
        ("SBC / revenue", lin(cap["sbc_pct"], *_bar("sbc_pct", ac)), pct(cap["sbc_pct"])),
    ]
    efficiency_components = [
        ("ROIC", lin(h0["roic"], *_bar("roic", ac)), pct(h0["roic"])),
        ("ROE", lin(h0["roe"], *_bar("roe", ac)), pct(h0["roe"])),
        ("ROA", lin(h0["roa"], *_bar("roa", ac)), pct(h0["roa"])),
    ]
    if not fin:
        efficiency_components.append(("Asset turnover", lin(h0["asset_turnover"], *_bar("asset_turnover", ac)),
                                      f"{h0['asset_turnover']:.2f}x" if h0["asset_turnover"] is not None else "n/a"))
    if fin:
        strength_components = []
    else:
        net_cash = h0["net_debt"] is not None and h0["net_debt"] <= 0
        strength_components = [
            ("Debt / EBITDA", 100.0 if net_cash else lin(h0["debt_to_ebitda"], *_bar("debt_to_ebitda", ac)),
             "net cash" if net_cash else (f"{h0['debt_to_ebitda']:.1f}x" if h0["debt_to_ebitda"] is not None else "n/a")),
            ("Interest coverage", 100.0 if not h0["interest"] and (h0["ebit"] or 0) > 0 else
             lin(h0["interest_coverage"], *_bar("interest_coverage", ac)),
             "no interest" if not h0["interest"] else (f"{h0['interest_coverage']:.1f}x" if h0["interest_coverage"] is not None else "n/a")),
            ("Current ratio", lin(h0["current_ratio"], *_bar("current_ratio", ac)),
             f"{h0['current_ratio']:.2f}" if h0["current_ratio"] is not None else "n/a"),
            ("Debt / equity", lin(h0["debt_to_equity"], *_bar("debt_to_equity", ac)),
             f"{h0['debt_to_equity']:.2f}" if h0["debt_to_equity"] is not None else "n/a"),
        ]
    categories = {
        "relative_valuation": _category(relative_components),
        "cash_flow_quality": _category(cash_components),
        "capital_efficiency": _category(efficiency_components),
        "financial_strength": _category(strength_components),
    }

    def weighted(keys):
        pairs = [(CATEGORY_WEIGHTS[k], categories[k]["score"]) for k in keys if categories[k]["score"] is not None]
        total = sum(w for w, _ in pairs)
        return round(sum(w * s for w, s in pairs) / total, 1) if total else None

    return {
        "categories": categories,
        "overall": weighted(CATEGORY_WEIGHTS),
        "quality": weighted(["cash_flow_quality", "capital_efficiency", "financial_strength"]),
        "valuation": categories["relative_valuation"]["score"],
        "renormalized": any(c["score"] is None for c in categories.values()),
    }


def _band(score):
    if score is None:
        return "Not scored"
    return next(label for floor, label in SCORE_BANDS if score >= floor)


def _summary(r, relative, scores, risks, cross_checks):
    v = r["valuation"]
    mk, dcf = v["market"], v["dcf"]
    q, fs = scores["quality"], scores["categories"]["financial_strength"]["score"]
    quality = "n/a" if q is None else ("Excellent" if q >= 80 else "Good" if q >= 65 else "Average" if q >= 50 else "Poor")
    health = "Not assessed (no bank capital ratios in the data)" if fs is None else (
        "Strong" if fs >= 70 else "Moderate" if fs >= 45 else "Weak")
    if dcf["available"]:
        up = dcf["scenarios"]["base"]["upside"]
        verdict = "Undervalued" if up > VALUATION_BAND else "Overvalued" if up < -VALUATION_BAND else "Fairly valued"
        verdict_basis = f"base-case DCF {per_share(dcf['scenarios']['base']['per_share'])} vs price {per_share(mk['price'])} ({pct(up, True)})"
    elif scores["valuation"] is not None:
        s = scores["valuation"]
        verdict = "Undervalued" if s >= 60 else "Overvalued" if s <= 40 else "Fairly valued"
        verdict_basis = f"relative valuation only (score {s:.0f}/100) — {dcf['reason']}"
    else:
        verdict, verdict_basis = "Not determinable", dcf["reason"]
    est = r["valuation"]["dcf"].get("g2") if dcf["available"] else None
    growth_rate = est if est is not None else v["growth"]["revenue"]["cagr"]
    potential = "n/a" if growth_rate is None else ("High" if growth_rate >= 0.15 else "Moderate" if growth_rate >= 0.05 else "Low")
    out = {
        "business_quality": quality, "classification": CLASS_LABELS[v["asset_class"]], "financial_health": health,
        "verdict": verdict, "verdict_basis": verdict_basis, "score": scores["overall"], "band": _band(scores["overall"]),
        "price": mk["price"], "growth_potential": potential, "growth_rate": growth_rate,
        "key_risks": [title for _, title, _ in risks[:3]],
    }
    if dcf["available"]:
        s = dcf["scenarios"]
        out.update({
            "intrinsic": {name: s[name]["per_share"] for name in ("bear", "base", "bull")},
            "margin_of_safety": s["base"]["margin_of_safety"],
            "mos_price": s["base"]["per_share"] * (1 - MARGIN_OF_SAFETY_TARGET)
            if s["base"]["per_share"] and s["base"]["per_share"] > 0 else None,
        })
        base_ps, price = s["base"]["per_share"], mk["price"]
        if base_ps and base_ps > 0 and price:
            out["model_return"] = (base_ps / price) ** (1 / 5) - 1 + (v["capital"]["dividend_yield"] or 0)
        out["assumptions"] = [
            f"Revenue grows {pct(dcf['g1'])} then {pct(dcf['g2'])}, fading to {pct(s['base']['terminal_growth'])} "
            f"by year {DCF_YEARS} — from {dcf['growth_source']}",
            f"Operating margin holds near {pct(dcf['base_margin'])}",
            f"Discount rate (WACC) of {pct(s['base']['wacc'])} reflects the risk",
            f"Net capex stays near {pct(max(dcf['capex_pct'] - dcf['dna_pct'], 0))} of revenue",
        ]
    triggers = []
    if dcf["available"]:
        bear = dcf["scenarios"]["bear"]
        triggers.append(f"The bear case — slower growth, {pct(abs(SCENARIOS['bear']['margin_shift']))} lower margins, "
                        f"+{SCENARIOS['bear']['wacc_shift'] * 100:.0f} pt discount rate — implies {per_share(bear['per_share'])} "
                        f"({pct(bear['upside'], True)}).")
    if relative["avg_premium"] is not None and relative["avg_premium"] > 0.2:
        triggers.append(f"A re-rating to the peer median would remove a {pct(relative['avg_premium'])} premium "
                        f"(about {pct(relative['avg_premium'] / (1 + relative['avg_premium']))} off the price).")
    triggers += [detail for _, _, detail in risks[:2]]
    out["decline_triggers"] = triggers
    divergent = [label for label, ps in cross_checks
                 if dcf["available"] and dcf["scenarios"]["base"]["per_share"] and ps
                 and abs(ps / dcf["scenarios"]["base"]["per_share"] - 1) > CROSS_CHECK_TOLERANCE]
    out["cross_check_note"] = (
        "" if not dcf["available"] or not cross_checks else
        "Cross-checks broadly agree with the base-case DCF." if not divergent else
        f"{len(divergent)} of {len(cross_checks)} cross-checks differ from the base-case DCF by more than "
        f"{CROSS_CHECK_TOLERANCE:.0%} — treat the DCF with caution.")
    return out


def apply_relative(results):
    """Second pass, once all tickers are evaluated: peers, relative valuation, cross-checks,
    risks, scores and the summary. Also copies headline numbers onto each result for the table/CSV."""
    for r in results:
        v = r["valuation"]
        relative = _relative(r, results)
        cross = _cross_checks(r, relative)
        risks = _risks(r, relative)
        scores = _scores(r, relative)
        v.update({"relative": relative, "cross_checks": cross, "risks": risks, "scores": scores})
        v["summary"] = _summary(r, relative, scores, risks, cross)
        base = v["dcf"]["scenarios"]["base"] if v["dcf"]["available"] else {}
        r.update({
            "score": scores["overall"], "quality_score": scores["quality"], "valuation_score": scores["valuation"],
            "valuation_verdict": v["summary"]["verdict"], "base_upside": base.get("upside"),
            "intrinsic_bear": v["summary"].get("intrinsic", {}).get("bear"),
            "intrinsic_base": v["summary"].get("intrinsic", {}).get("base"),
            "intrinsic_bull": v["summary"].get("intrinsic", {}).get("bull"),
            "price": v["market"]["price"], "forward_pe": v["multiples"]["forward_pe"],
            "ev_ebitda": v["multiples"]["ev_ebitda"], "fcf_yield": v["multiples"]["fcf_yield"],
            "peers": " ".join(relative["peers"]),
        })


# ---- HTML -----------------------------------------------------------------------------------

STYLE = """
.vsec{margin-top:10px}.vsec h3{font-size:16px;margin:0 0 10px;font-weight:800}
.vsec h3 small{font-weight:600;color:var(--ink-muted);font-size:12px;margin-left:6px}
.vsec table{font-size:13px}.vsec td,.vsec th{padding:5px 8px}
.vsec .grp td{font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-muted);
padding-top:12px;border-bottom:1px solid var(--tile-border)}
.notes{margin:10px 0 0;padding-left:18px}.notes li{margin:3px 0}
.why{font-size:13px;color:var(--ink-muted);margin:8px 0 0}
.scoregrid{display:grid;grid-template-columns:170px 1fr;gap:16px;align-items:center}
.bigscore{font-size:52px;font-weight:800;line-height:1}.bigscore small{font-size:18px;color:var(--ink-muted)}
.chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px}
.chip{background:var(--paper);border:1px solid var(--tile-border);border-radius:6px;padding:8px 10px}
.chip b{display:block;font-size:15px}.chip span{font-size:11px;color:var(--ink-muted);text-transform:uppercase;letter-spacing:.06em}
.range{position:relative;height:46px;margin:18px 6px 6px}
.range .track{position:absolute;left:0;right:0;top:20px;height:6px;border-radius:3px;background:var(--tile-border)}
.range .band{position:absolute;top:20px;height:6px;border-radius:3px;background:var(--profit);opacity:.45}
.range .mk{position:absolute;top:0;transform:translateX(-50%);font-size:11px;font-weight:700;text-align:center;white-space:nowrap}
.range .mk i{display:block;width:2px;height:14px;margin:2px auto 0;background:currentColor}
.range .mk.price{top:26px}.range .mk.price i{margin:0 auto 2px}
.pos{color:var(--profit)}.neg{color:var(--loss)}
.disclaimer{font-size:12px;color:var(--ink-muted);border-left:3px solid var(--olive);padding:4px 10px;margin-top:12px}
.sev3{color:var(--loss);font-weight:800}.sev2{color:var(--p4)}.sev1{color:var(--ink-muted)}
.range-legend{display:none}
@media (max-width:720px){.scoregrid{grid-template-columns:1fr}.chips{grid-template-columns:repeat(2,minmax(0,1fr))}
.range .mk span{display:none}.range{height:28px}.range-legend{display:block}}
"""


def _signed_class(v):
    return "" if v is None else ("pos" if v > 0 else "neg")


def _list(items):
    return "<ul class='notes'>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>" if items else ""


def _range_bar(price, intrinsic):
    """Bear-to-bull band with base and price markers on one scale."""
    values = [x for x in (price, *intrinsic.values()) if x is not None]
    if not values or not price:
        return ""
    lo, hi = min(0, min(values)), max(values) * 1.08
    span = hi - lo or 1

    def x(v):
        return (v - lo) / span * 100

    bear, base, bull = intrinsic["bear"], intrinsic["base"], intrinsic["bull"]
    band = (f'<div class="band" style="left:{x(min(bear, bull)):.1f}%;width:{abs(x(bull) - x(bear)):.1f}%"></div>'
            if bear is not None and bull is not None else "")
    points = [(label, val) for label, val in (("Bear", bear), ("Base", base), ("Bull", bull)) if val is not None]
    markers = "".join(f'<div class="mk" style="left:{x(val):.1f}%"><span>{label} {per_share(val)}</span><i></i></div>'
                      for label, val in points)
    legend = " · ".join(f"{label} {per_share(val)}" for label, val in [*points, ("Price", price)])
    return (f'<div class="range"><div class="track"></div>{band}{markers}'
            f'<div class="mk price" style="left:{x(price):.1f}%"><i></i><span>Price {per_share(price)}</span></div></div>'
            f'<p class="why range-legend">{legend}</p>')


def render_html(r):
    """The valuation sections of a stock card (inserted after the life-cycle diagram)."""
    e = html.escape
    v = r["valuation"]
    s, sc, dcf, rel, cap = v["summary"], v["scores"], v["dcf"], v["relative"], v["capital"]
    mk, hist, ac = v["market"], v["hist"], v["asset_class"]
    parts = []

    # Summary (template sections 9 + 10)
    chips = [("Business quality", s["business_quality"]), ("Classification", s["classification"]),
             ("Financial health", s["financial_health"]), ("Valuation (model)", s["verdict"]),
             ("Growth potential", s["growth_potential"] + (f" ({pct(s['growth_rate'])})" if s["growth_rate"] is not None else "")),
             ("Price", per_share(s["price"]))]
    if "intrinsic" in s:
        chips += [("Margin of safety (base)", mos_text(s["margin_of_safety"])),
                  (f"Price for a {MARGIN_OF_SAFETY_TARGET:.0%} margin", per_share(s["mos_price"]))]
    if s.get("model_return") is not None:
        chips.append(("Model 5-yr return / yr", pct(s["model_return"], True)))
    chip_html = "".join(f'<div class="chip"><span>{e(k)}</span><b>{e(str(val))}</b></div>' for k, val in chips)
    q, val_score = sc["quality"], sc["valuation"]
    parts.append(f"""<div class="tile vsec"><h3>Model summary<small>sections 9–10</small></h3>
<div class="scoregrid"><div><div class="bigscore">{"n/a" if s["score"] is None else f"{s['score']:.0f}"}<small>/100</small></div>
<div class="why"><b>{e(s["band"])}</b><br>Quality {"n/a" if q is None else f"{q:.0f}"} · Valuation {"n/a" if val_score is None else f"{val_score:.0f}"}</div></div>
<div class="chips">{chip_html}</div></div>
{_range_bar(s["price"], s["intrinsic"]) if "intrinsic" in s else ""}
<p class="why"><b>Valuation basis:</b> {e(s["verdict_basis"])}. A high quality score doesn't make a stock cheap — the two are scored separately.</p>
<p class="why"><b>Top risks:</b> {e(", ".join(s["key_risks"]) or "none flagged by the rules")}.</p>
{f'<p class="why"><b>What could cause a 20–30% decline:</b></p>{_list(s["decline_triggers"])}' if s["decline_triggers"] else ""}
{f'<p class="why"><b>Assumptions that must hold for the base case:</b></p>{_list(s["assumptions"])}' if s.get("assumptions") else ""}
{f'<p class="why">The 5-year model return assumes the price converges to the base-case value and adds the dividend yield — an illustration of the model, not a forecast.</p>' if s.get("model_return") is not None else ""}
<div class="disclaimer">Model output from the stated assumptions and Yahoo Finance data — not investment advice.
Buy/sell ratings, entry prices and "would you buy" answers are intentionally not given: those are your decisions.
Moat, management and competitive dynamics are not assessed — they need qualitative research.</div></div>""")

    # 1 Business overview
    b = v["business"]
    path = " → ".join(f"{fy} {money(rev)}" for fy, rev in b["revenue_path"])
    votes = f"{r['light_votes']} of {r['signals_available']}"
    why_class = ("No capex reported, which is typical of banks and insurers: valued with the financials framework."
                 if ac == "financial" else
                 f"{votes} asset signals point asset-light (see the Asset model panel): "
                 + {"light": "low physical assets and capex, high margins and returns.",
                    "heavy": "significant physical assets, capex and depreciation, with lower margins or returns.",
                    "hybrid": "a mix — some signals light, some heavy — so both valuation frameworks apply."}[ac])
    parts.append(f"""<div class="tile vsec"><h3>1 · Business overview & classification</h3>
<p style="margin:0">{e(b["summary"]) or '<span class="muted">No business description available.</span>'}</p>
<ul class="notes"><li><b>Revenue trajectory:</b> {e(path)}{f" (CAGR {pct(b['cagr'])} over {b['years']} years)" if b["cagr"] is not None else ""}</li>
<li><b>Business pattern:</b> {e(b["model"])}. Whether revenue is recurring, subscription or transactional can't be read from the statements.</li>
<li><b>Classification: {e(CLASS_LABELS[ac])}</b> — {e(why_class)}</li>
<li><b>Competitors in this screen:</b> {", ".join(f'<a href="{e(p)}.html">{e(p)}</a>' for p in rel["peers"]) or "none"} <span class="muted">({e(rel["basis"])})</span></li></ul></div>""")

    # 2 Financial health
    cols = list(reversed(hist))
    header = "".join(f'<th class="num">{h["fy"]}</th>' for h in cols)

    def row(label, key, kind):
        cells = []
        for h in cols:
            x = h[key]
            cells.append("n/a" if x is None else money(x) if kind == "$" else pct(x) if kind == "%" else f"{x:.1f}x")
        return f"<tr><td>{label}</td>" + "".join(f'<td class="num">{c}</td>' for c in cells) + "</tr>"

    def group(title):
        return f'<tr class="grp"><td colspan="{len(cols) + 1}">{title}</td></tr>'

    g = v["growth"]
    growth_rows = "".join(
        f'<tr><td>{x["label"]}</td><td class="num {_signed_class(x["yoy"])}">{pct(x["yoy"], True)}</td>'
        f'<td class="num {_signed_class(x["cagr"])}">{pct(x["cagr"], True)}</td></tr>' for x in g.values())
    fin_rows = (group("Profitability") + row("Revenue", "revenue", "$") + row("Gross margin", "gross_margin", "%")
                + row("Operating margin", "op_margin", "%") + row("Net margin", "net_margin", "%")
                + row("EBITDA margin", "ebitda_margin", "%") + row("FCF margin", "fcf_margin", "%")
                + group("Capital efficiency") + row("ROIC", "roic", "%") + row("ROE", "roe", "%") + row("ROA", "roa", "%")
                + row("Asset turnover", "asset_turnover", "x") + row("Capex / revenue", "capex_pct", "%")
                + row("Invested capital / revenue", "ic_to_revenue", "x")
                + group("Financial strength") + row("Total debt", "debt", "$") + row("Net debt", "net_debt", "$")
                + row("Debt / EBITDA", "debt_to_ebitda", "x") + row("Debt / equity", "debt_to_equity", "x")
                + row("Interest coverage", "interest_coverage", "x") + row("Current ratio", "current_ratio", "x")
                + row("Working capital", "working_capital", "$"))
    trend_html = ""
    if v["improving"]:
        trend_html += f'<p class="why"><b class="pos">Improving:</b></p>{_list(v["improving"])}'
    if v["deteriorating"]:
        trend_html += f'<p class="why"><b class="neg">Deteriorating:</b></p>{_list(v["deteriorating"])}'
    parts.append(f"""<div class="tile vsec"><h3>2 · Financial health & history<small>fiscal years, as reported</small></h3>
<div class="scroll"><table><tr><th>Growth (latest FY)</th><th class="num">YoY</th><th class="num">CAGR ({g["revenue"]["years"]} yrs)</th></tr>{growth_rows}</table></div>
<div class="scroll" style="margin-top:8px"><table><tr><th></th>{header}</tr>{fin_rows}</table></div>
{trend_html or '<p class="why">No metric moved meaningfully vs its prior average.</p>'}</div>""")

    # 3 Framework
    fw = "".join(f'<div class="chip"><span>{e(MULTIPLES[k][0])}</span><b>{fmt(k, v["multiples"][k])}</b></div>' for k in FRAMEWORK[ac])
    parts.append(f"""<div class="tile vsec"><h3>3 · Valuation framework: {e(CLASS_LABELS[ac])}<small>{v["ltm_basis"]} figures, price {per_share(mk["price"])}</small></h3>
<div class="chips">{fw}</div>
<p class="why">For this business model the analysis focuses on {e(FRAMEWORK_FOCUS[ac])}. Multiples outside this set are shown in section 4 for comparison only and don't drive the valuation score. "n/a" means the input is missing or negative (a multiple of a loss isn't meaningful).</p></div>""")

    # 4 Relative valuation
    rel_rows = ""
    for row_ in rel["rows"]:
        k = row_["key"]
        own = row_["own"]
        own_text = f'{fmt(k, own["min"])}–{fmt(k, own["max"])} (median {fmt(k, own["median"])})' if own else "n/a"
        prem = row_["premium"]
        star = " ★" if k in FRAMEWORK[ac] else ""
        rel_rows += (f'<tr><td>{MULTIPLES[k][0]}{star}</td><td class="num">{fmt(k, row_["company"])}</td>'
                     f'<td class="num">{fmt(k, row_["peer_median"])}</td>'
                     f'<td class="num {_signed_class(-prem if prem is not None else None)}">'
                     f'{"n/a" if prem is None else ("+" if prem > 0 else "") + f"{prem * 100:.0f}%"}</td>'
                     f'<td class="num">{own_text}</td></tr>')
    parts.append(f"""<div class="tile vsec"><h3>4 · Relative valuation<small>vs {rel["peer_count"]} peers · ★ = framework multiple</small></h3>
<div class="scroll"><table><tr><th>Metric</th><th class="num">{e(r["ticker"])}</th><th class="num">Peer median</th><th class="num">Premium</th><th class="num">Own range (FY-ends)</th></tr>{rel_rows}</table></div>
{_list(rel["notes"])}<p class="why">Peers are {e(rel["basis"])}; their median stands in for both the industry average and the direct-competitor set. Own range uses fiscal-year-end prices with that year's results.</p></div>""")

    # 5 DCF
    if dcf["available"]:
        w = v["wacc"]
        scen = dcf["scenarios"]

        def srow(label, cell):
            return f"<tr><td>{label}</td>" + "".join(f'<td class="num">{cell(scen[n])}</td>' for n in ("bear", "base", "bull")) + "</tr>"

        dcf_table = (srow("Revenue growth, year 1 → 2", lambda x: f"{pct(x['growth_y1'])} → {pct(x['growth_y2'])}")
                     + srow("Terminal growth", lambda x: pct(x["terminal_growth"]))
                     + srow("Operating margin", lambda x: pct(x["margin"]))
                     + srow("WACC", lambda x: pct(x["wacc"]))
                     + srow("Enterprise value", lambda x: money(x["ev"]))
                     + srow("Equity value", lambda x: money(x["equity"]))
                     + srow("<b>Intrinsic value / share</b>", lambda x: f"<b>{per_share(x['per_share'])}</b>")
                     + srow("Upside vs price", lambda x: f'<span class="{_signed_class(x["upside"])}">{pct(x["upside"], True)}</span>')
                     + srow("Margin of safety", lambda x: mos_text(x["margin_of_safety"]))
                     + srow("Terminal value share of EV", lambda x: pct(x["terminal_share"])))
        assumptions = [
            f"Starting revenue {money(dcf['revenue0'])} ({v['ltm_basis']}); growth from {dcf['growth_source']}, fading linearly to terminal growth by year {DCF_YEARS}.",
            f"Base operating margin {pct(dcf['base_margin'])} = average of the current {pct(dcf['margin_ltm'])} and the {len(hist)}-year median {pct(dcf['margin_hist'])}. Bear {SCENARIOS['bear']['margin_shift']:+.0%}, bull {SCENARIOS['bull']['margin_shift']:+.0%} relative.",
            f"Tax rate {pct(dcf['tax_rate'])}; capex {pct(dcf['capex_pct'])} and depreciation {pct(dcf['dna_pct'])} of revenue (3-year averages); working capital {pct(dcf['nwc_pct'])} of incremental revenue.",
            f"WACC {pct(w['wacc'])}: cost of equity {pct(w['cost_of_equity'])} = risk-free {pct(w['risk_free'])} + beta {w['beta']:.2f} × {pct(EQUITY_RISK_PREMIUM)} premium"
            + (f" (Yahoo beta {w['beta_reported']:.2f}, clamped to {BETA_RANGE[0]}–{BETA_RANGE[1]})" if w["beta_reported"] is not None and abs(w["beta_reported"] - w["beta"]) > 1e-9 else "")
            + (f"; after-tax cost of debt {pct(w['cost_of_debt'] * (1 - dcf['tax_rate']))} at {pct(w['debt_weight'])} weight." if w["cost_of_debt"] is not None else "."),
            f"Terminal value reinvests growth ÷ return on new capital ({pct(_clamp(dcf['terminal_ronic'], scen['base']['wacc'], TERMINAL_RONIC_CAP)) if dcf['terminal_ronic'] is not None else 'WACC'}), so growth isn't free. Net debt {money(dcf['net_debt'])} is subtracted to reach equity.",
        ]
        cc = "".join(f'<tr><td>{e(label)}</td><td class="num">{per_share(ps)}</td>'
                     f'<td class="num">{pct(ps / scen["base"]["per_share"] - 1, True) if ps and scen["base"]["per_share"] else "n/a"}</td></tr>'
                     for label, ps in v["cross_checks"])
        cross = (f'<div class="scroll" style="margin-top:10px"><table><tr><th>Cross-check</th><th class="num">Value / share</th>'
                 f'<th class="num">vs base DCF</th></tr>{cc}</table></div><p class="why">{e(s["cross_check_note"])}</p>' if cc else
                 '<p class="why">No cross-check possible (missing peer multiples or forward EPS).</p>')
        parts.append(f"""<div class="tile vsec"><h3>5 · Intrinsic value (DCF)<small>{DCF_YEARS}-year free cash flow to the firm</small></h3>
<div class="scroll"><table><tr><th></th><th class="num">Bear</th><th class="num">Base</th><th class="num">Bull</th></tr>{dcf_table}</table></div>
{_list(assumptions)}{cross}
<p class="why">Values are rounded to the dollar on purpose: small changes to growth, margin or WACC move them a lot.</p></div>""")
    else:
        cc = "".join(f'<li>{e(label)}: {per_share(ps)}</li>' for label, ps in v["cross_checks"])
        parts.append(f"""<div class="tile vsec"><h3>5 · Intrinsic value (DCF)</h3><p class="why">{e(dcf["reason"])}</p>
{f'<p class="why"><b>Alternative methods:</b></p><ul class="notes">{cc}</ul>' if cc else ""}</div>""")

    # 6 Capital allocation
    cap_items = [("FCF", money(cap["fcf"])), ("FCF yield", pct(cap["fcf_yield"])), ("FCF / net income", pct(cap["fcf_conversion"])),
                 ("Dividends", money(cap["dividends"])), ("Dividend share of FCF", pct(cap["dividend_payout_fcf"])),
                 ("Buybacks", money(cap["buybacks"])), ("SBC / revenue", pct(cap["sbc_pct"])),
                 ("Share count change / yr", pct(cap["dilution"], True)), ("Debt repaid (FY)", money(cap["debt_repaid"])),
                 (f"Acquisitions ({cap['acquisition_years']} FYs)", money(cap["acquisitions"])),
                 ("Capex / operating cash flow", pct(cap["reinvestment_rate"]))]
    cap_html = "".join(f'<div class="chip"><span>{e(k)}</span><b>{val}</b></div>' for k, val in cap_items)
    parts.append(f"""<div class="tile vsec"><h3>6 · Free cash flow & capital allocation<small>{v["ltm_basis"]}</small></h3>
<div class="chips">{cap_html}</div>
<p class="why"><b>Capital allocation: {cap["verdict"]}</b></p>{_list([f"✓ {x}" for x in cap["good"]] + [f"✕ {x}" for x in cap["bad"]])}</div>""")

    # 7 Competitive position
    comp = v["competitive"]
    insider = f"Insiders hold {pct(comp['insider_pct'])} of shares." if comp["insider_pct"] is not None else "Insider ownership not reported."
    parts.append(f"""<div class="tile vsec"><h3>7 · Competitive advantage<small>numbers only</small></h3>
{_list(comp["notes"] + [insider])}
<p class="why">Management track record, switching costs, brand, customer concentration and barriers to entry are not assessed — they need qualitative research (filings, earnings calls, industry sources).</p></div>""")

    # 8 Risks
    risk_items = "".join(f'<li><span class="sev{sev}">{"●" * sev}</span> <b>{e(title)}</b> — {e(detail)}</li>' for sev, title, detail in v["risks"])
    parts.append(f"""<div class="tile vsec"><h3>8 · Risks & red flags<small>rule-based, most severe first</small></h3>
{f'<ul class="notes">{risk_items}</ul>' if risk_items else '<p class="why">No rule-based red flags triggered.</p>'}
<p class="why">Regulatory, technology-disruption and competitive threats aren't visible in the numbers and aren't covered here.</p></div>""")

    # 9 Score detail
    names = {"relative_valuation": "Relative valuation", "cash_flow_quality": "Cash flow quality",
             "capital_efficiency": "Capital efficiency", "financial_strength": "Financial strength"}
    score_rows = ""
    for key, cat in sc["categories"].items():
        comps = "; ".join(f"{label} {detail} → {'n/a' if s_ is None else f'{s_:.0f}'}" for label, s_, detail in cat["components"])
        cat_score = "n/a" if cat["score"] is None else f"{cat['score']:.0f}"
        score_rows += (f'<tr><td><b>{names[key]}</b></td><td class="num">{CATEGORY_WEIGHTS[key]:.0%}</td>'
                       f'<td class="num"><b>{cat_score}</b></td>'
                       f'<td class="why" style="margin:0">{e(comps) or "not applicable"}</td></tr>')
    parts.append(f"""<div class="tile vsec"><h3>9 · Score breakdown<small>bars adjusted for {e(CLASS_LABELS[ac].lower())}</small></h3>
<div class="scroll"><table><tr><th>Category</th><th class="num">Weight</th><th class="num">Score</th><th>Components (value → score)</th></tr>{score_rows}</table></div>
{'<p class="why">Some categories are not applicable here, so the overall score reweights the rest.</p>' if sc["renormalized"] else ""}
<p class="why">85+ exceptional · 75–84 attractive · 65–74 worth monitoring · 50–64 weak · below 50 unattractive.</p></div>""")
    return "\n".join(parts)
