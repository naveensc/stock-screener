"""Reading values out of the cached Yahoo statements, shared by lifecycle.py and valuation.py.

A raw record stores each statement as {"YYYY-MM-DD": {line item: value}} under "annual" and
"quarterly" (income, cash flow and balance sheet merged per period — see lifecycle.fetch_raw).
Yahoo labels the same concept differently across companies, so each concept is a list of
line items tried in order.
"""

from datetime import date

LTM_MAX_SPAN_DAYS = 300  # four consecutive quarter-ends span ~273 days; more means a quarter is missing

REVENUE_ROWS = ["Total Revenue", "Operating Revenue"]
GROSS_PROFIT_ROWS = ["Gross Profit"]
OPERATING_INCOME_ROWS = ["Operating Income"]
EBIT_ROWS = ["EBIT"]
EBITDA_ROWS = ["EBITDA", "Normalized EBITDA"]
NET_INCOME_ROWS = ["Net Income", "Net Income Common Stockholders"]
EPS_ROWS = ["Diluted EPS", "Basic EPS"]
SHARES_ROWS = ["Diluted Average Shares", "Basic Average Shares"]
INTEREST_ROWS = ["Interest Expense", "Interest Expense Non Operating"]
TAX_RATE_ROWS = ["Tax Rate For Calcs"]
DEPRECIATION_ROWS = ["Reconciled Depreciation", "Depreciation And Amortization", "Depreciation Amortization Depletion"]

OCF_ROWS = ["Operating Cash Flow"]
CAPEX_ROWS = ["Capital Expenditure"]
FCF_ROWS = ["Free Cash Flow"]
SBC_ROWS = ["Stock Based Compensation"]
BUYBACK_ROWS = ["Repurchase Of Capital Stock", "Common Stock Payments"]
DIVIDEND_ROWS = ["Cash Dividends Paid", "Common Stock Dividend Paid"]
DEBT_REPAYMENT_ROWS = ["Repayment Of Debt"]
ACQUISITION_ROWS = ["Purchase Of Business", "Net Business Purchase And Sale"]

PPE_ROWS = ["Net PPE"]
TOTAL_ASSETS_ROWS = ["Total Assets"]
INVESTED_CAPITAL_ROWS = ["Invested Capital"]
DEBT_ROWS = ["Total Debt"]
CASH_ROWS = ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"]
EQUITY_ROWS = ["Stockholders Equity", "Common Stock Equity"]
CURRENT_ASSETS_ROWS = ["Current Assets"]
CURRENT_LIABILITIES_ROWS = ["Current Liabilities"]
WORKING_CAPITAL_ROWS = ["Working Capital"]


def value(period: dict, rows: list[str]) -> float | None:
    for row in rows:
        if row in period:
            return period[row]
    return None


def _reported(periods: dict) -> list[str]:
    """Period dates that have revenue, newest first. Right after a period closes Yahoo often adds
    a placeholder column with a handful of rows and no results yet; those are skipped, or they'd
    be taken as "the latest period" and every figure in it would read as missing."""
    return [d for d in sorted(periods, reverse=True) if value(periods[d], REVENUE_ROWS) is not None]


def ltm(quarterly: dict, rows: list[str]) -> float | None:
    """Sum of the latest four reported quarters, or None if any is missing or they aren't consecutive."""
    dates = _reported(quarterly)[:4]
    if len(dates) < 4:
        return None
    if (date.fromisoformat(dates[0]) - date.fromisoformat(dates[3])).days > LTM_MAX_SPAN_DAYS:
        return None
    values = [value(quarterly[d], rows) for d in dates]
    return None if None in values else sum(values)


def metric(raw: dict, rows: list[str]) -> tuple[float | None, str]:
    """Returns (value, basis): LTM when available, else the latest fiscal year."""
    total = ltm(raw.get("quarterly", {}), rows)
    if total is not None:
        return total, "LTM"
    years = fiscal_years(raw)
    latest = years[0] if years else max(raw["annual"])
    return value(raw["annual"][latest], rows), f"FY{latest[:4]}"


def fiscal_years(raw: dict) -> list[str]:
    """Annual period dates that have revenue, newest first. Skips placeholder columns for a year
    that just closed, and the extra year Yahoo's cash flow and balance sheet often reach back."""
    return _reported(raw["annual"])
