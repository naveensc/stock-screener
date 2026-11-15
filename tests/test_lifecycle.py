import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import valuation  # noqa: E402
from lifecycle import (  # noqa: E402
    InsufficientData,
    _fy,
    analyze,
    analyze_full,
    analyze_with_transition,
    classify_phase,
    demo_universe,
    make_raw,
    render,
    render_screener,
)


def phase_of(*years, quarterly=None):
    return analyze(make_raw("TEST", list(years), quarterly)).get("phase")


# ---- one case per phase ----


def test_startup_unprofitable_slow_growth():
    assert phase_of(_fy(50, -20, -15, 2), _fy(55, -25, -20, 2)) == 1


def test_hyper_growth_unprofitable_fast_growth():
    assert phase_of(_fy(100, -50, -40, 2), _fy(150, -60, -45, 3)) == 2


def test_operating_leverage_growing_profitable_no_capital_return():
    assert phase_of(_fy(1000, 50, 80, 100), _fy(1100, 90, 120, 110)) == 3


def test_capital_return_growing_profitable_buybacks():
    assert phase_of(_fy(1000, 100, 150, 100), _fy(1100, 120, 170, 110, buybacks=50)) == 4


def test_capital_return_dividends_alone_count():
    assert phase_of(_fy(1000, 100, 150, 100), _fy(1100, 120, 170, 110, dividends=30)) == 4


def test_decline_shrinking_revenue_even_if_returning_capital():
    assert phase_of(_fy(1000, 100, 150, 100), _fy(950, 90, 140, 100, buybacks=50, dividends=50)) == 5


def test_unprofitable_mature_payer_is_decline_not_startup():
    # Ford-like: flat-ish revenue, a loss year, still paying dividends.
    assert phase_of(_fy(180000, 5000, 15000, 9000, 1000, 2500), _fy(182000, -7800, 14000, 9500, 200, 2500)) == 5


def test_flat_revenue_is_not_growing():
    assert phase_of(_fy(1000, 100, 150, 100), _fy(1000, 100, 150, 100)) == 5


# ---- edge cases ----


def test_negative_ltm_cash_flow_makes_asset_light_company_unprofitable():
    # Positive net income and positive annual OCF, but the last four quarters' OCF sum is
    # negative. Asset-light -> judged on LTM OCF -> not profitable -> hyper growth, not phase 3.
    quarters = [_fy(40, 5, -10, 1), _fy(45, 5, -12, 1), _fy(50, 6, -8, 1), _fy(55, 6, 5, 1)]
    r = analyze(make_raw("TEST", [_fy(100, 10, 12, 2), _fy(160, 20, 15, 3)], quarters))
    assert r["profit_metric"] == "Operating cash flow"
    assert r["profit_basis"] == "LTM"
    assert r["profit_value"] == pytest.approx(-25e6)
    assert not r["profitable"]
    assert r["phase"] == 2


def test_zero_revenue_is_startup_without_dividing_by_zero():
    r = analyze(make_raw("TEST", [_fy(0, -10, -8, 1), _fy(0, -20, -15, 1)]))
    assert r["revenue_growth"] is None
    assert not r["revenue_growing"]
    assert not r["returning_capital"]
    assert r["phase"] == 1


def test_hyper_growth_and_profitable_is_operating_leverage():
    r = analyze(make_raw("TEST", [_fy(100, 10, 15, 2), _fy(200, 30, 40, 4)]))
    assert r["revenue_growth"] == pytest.approx(1.0)
    assert r["profitable"]
    assert r["phase"] == 3


def test_capex_heavy_company_judged_on_net_income():
    # OCF positive but net income negative: capex 20% of revenue -> net income decides.
    r = analyze(make_raw("TEST", [_fy(1000, -10, 200, 200), _fy(1300, -20, 250, 260)]))
    assert r["profit_metric"] == "Net income"
    assert r["phase"] == 2


def test_missing_capex_falls_back_to_net_income():
    r = analyze(make_raw("TEST", [_fy(1000, 100, -500, None), _fy(1100, 120, -600, None)]))
    assert r["profit_metric"] == "Net income"
    assert r["profitable"]


def test_token_buybacks_below_threshold_do_not_count():
    assert phase_of(_fy(1000, 100, 150, 100), _fy(1100, 120, 170, 110, buybacks=5)) == 3


def test_ltm_ignored_when_a_quarter_is_missing():
    raw = make_raw("TEST", [_fy(100, 10, 12, 2), _fy(160, 20, 15, 3)],
                   [_fy(40, 5, -10, 1), _fy(45, 5, -12, 1), _fy(50, 6, -8, 1), _fy(55, 6, 5, 1), _fy(60, 6, 5, 1)])
    del raw["quarterly"][sorted(raw["quarterly"])[-2]]  # gap: latest four now span > 300 days
    r = analyze(raw)
    assert r["profit_basis"] == "FY2025"
    assert r["profitable"]


def _with_assets(base: dict, **rows) -> dict:
    return {**base, **{k: v * 1e6 for k, v in rows.items()}}


def test_asset_vote_overrules_low_capex():
    # Capex 3% (light) but PP&E-heavy, high depreciation, thin margin, low ROIC -> 1 of 5 -> heavy,
    # so a positive OCF with a net loss is NOT profitable.
    latest = _with_assets(_fy(1300, -20, 250, 39), ppe=3000, total_assets=5000, invested_capital=4000,
                          dna=130, operating_income=40, ebit=40)
    r = analyze(make_raw("TEST", [_fy(1000, 10, 200, 30), latest]))
    assert (r["light_votes"], r["signals_available"]) == (1, 5)
    assert r["asset_model"] == "Asset-heavy"
    assert r["profit_metric"] == "Net income"
    assert r["phase"] == 2


def test_asset_vote_light_majority():
    # High capex (8%, heavy) but light on the other four -> 4 of 5 -> asset-light.
    latest = _with_assets(_fy(1000, 150, 300, 80), ppe=100, total_assets=2000, invested_capital=1000,
                          dna=20, operating_income=250, ebit=250)
    r = analyze(make_raw("TEST", [_fy(800, 100, 200, 60), latest]))
    assert r["roic"] == pytest.approx(250 * (1 - 0.21) / 1000)
    assert (r["light_votes"], r["signals_available"]) == (4, 5)
    assert r["asset_model"] == "Asset-light"
    assert r["profit_metric"] == "Operating cash flow"


def test_asset_vote_tie_is_hybrid_but_judged_on_net_income():
    latest = _with_assets(_fy(1000, 50, 100, 30), ppe=600, total_assets=1000)  # capex light, PP&E heavy
    r = analyze(make_raw("TEST", [_fy(900, 40, 90, 30), latest]))
    assert (r["light_votes"], r["signals_available"]) == (1, 2)
    assert r["asset_model"] == "Hybrid"
    assert r["profit_metric"] == "Net income"  # the profit metric treats a tie as heavy


@pytest.mark.parametrize("light, expected", [(5, "Asset-light"), (4, "Asset-light"), (3, "Hybrid"),
                                             (2, "Hybrid"), (1, "Asset-heavy"), (0, "Asset-heavy")])
def test_asset_model_bands(light, expected):
    # Five signals, `light` of them pointing asset-light (the rest set to their heavy side).
    heavy = 5 - light
    rows = dict(ppe=900 if heavy > 0 else 100, total_assets=1000,  # PP&E/assets
                dna=80 if heavy > 1 else 10,  # depreciation/revenue
                operating_income=50 if heavy > 2 else 300, ebit=50 if heavy > 2 else 300,  # margin
                invested_capital=5000 if heavy > 3 else 200)  # ROIC (200 keeps it >15% even at EBIT 50)
    capex = 200 if heavy > 4 else 20  # capex/revenue
    latest = _with_assets(_fy(1000, 50, 100, capex), **rows)
    r = analyze(make_raw("TEST", [_fy(900, 40, 90, capex), latest]))
    assert (r["light_votes"], r["signals_available"]) == (light, 5)
    assert r["asset_model"] == expected


def test_placeholder_periods_for_a_just_closed_year_are_skipped():
    # Yahoo adds a few-row column (no revenue, no income) right after a period closes. It must
    # not be taken as the latest year or quarter (AMAT, RJF: "no net income").
    quarters = [_fy(40, 5, 8, 1), _fy(45, 5, 9, 1), _fy(50, 6, 9, 1), _fy(55, 6, 10, 1)]
    raw = make_raw("TEST", [_fy(100, 10, 12, 2), _fy(160, 20, 30, 3)], quarters)
    raw["annual"]["2026-12-31"] = {"Ordinary Shares Number": 1e9, "Share Issued": 1e9}
    raw["quarterly"]["2026-09-30"] = {"Ordinary Shares Number": 1e9}
    r = analyze(raw)
    assert r["fiscal_year"] == "FY2025"
    assert r["profit_basis"] == "LTM" and r["profit_value"] == pytest.approx(36e6)
    raw["quarterly"] = {}  # without quarterlies the fiscal-year fallback must skip the stub too
    assert analyze(raw)["profit_basis"] == "FY2025"


def test_single_year_of_revenue_is_insufficient():
    with pytest.raises(InsufficientData):
        analyze(make_raw("TEST", [_fy(100, 10, 12, 2)]))


# ---- transitions ----


@pytest.mark.parametrize("ticker, prior, current, label", [
    ("LEVR", 2, 3, "Just turned profitable"),
    ("RTRN", 3, 4, "Started returning capital"),
    ("FADE", 4, 5, "Entered decline"),
])
def test_key_transitions(ticker, prior, current, label):
    r = analyze_with_transition(demo_universe()[ticker])
    assert (r["prior_phase"], r["phase"], r["transition"]) == (prior, current, label)


def test_prior_year_uses_current_profit_metric():
    # Capex drifts across the 5% cutoff (4% -> 6%). Prior OCF is positive but prior net income
    # is negative: judging the prior year on OCF would invent a 3 -> ... "transition".
    raw = make_raw("TEST", [_fy(1000, -10, 100, 40), _fy(1100, -20, 110, 44), _fy(1200, -30, 120, 72)])
    r = analyze_with_transition(raw)
    assert r["profit_metric"] == "Net income"
    assert (r["prior_phase"], r["phase"]) == (1, 1)
    assert not r["phase_changed"]


def test_no_transition_when_phase_unchanged():
    r = analyze_with_transition(demo_universe()["CASH"])
    assert not r["phase_changed"]
    assert r["transition"] == ""


def test_demo_covers_every_phase_and_renders():
    results = [analyze_full(raw, valuation.RISK_FREE_FALLBACK) for raw in demo_universe().values()]
    valuation.apply_relative(results)
    assert {r["phase"] for r in results} == {1, 2, 3, 4, 5}
    for r in results:
        assert "Model summary" in render(r)
        assert r["score"] is None or 0 <= r["score"] <= 100
    assert "</script>" in render_screener(results, "demo")


# ---- valuation ----


def test_lin_scales_both_directions():
    assert valuation.lin(0.5, 0, 1) == 50
    assert valuation.lin(2, 0, 1) == 100 and valuation.lin(-1, 0, 1) == 0
    assert valuation.lin(1.0, 5.0, 1.0) == 100  # lower is better (e.g. Debt/EBITDA)
    assert valuation.lin(None, 0, 1) is None


def test_dcf_matches_gordon_growth_in_steady_state():
    # Constant 3% growth, no net capex or working capital, terminal reinvestment g/RONIC: the
    # explicit years plus terminal value collapse to a Gordon growth formula on FCFF.
    revenue, margin, tax, g, wacc, ronic = 1000.0, 0.2, 0.25, 0.03, 0.09, 0.03 / 1e-9
    res = valuation.dcf_value(revenue, margin, tax, 0.0, 0.0, 0.0, [g] * 10, g, wacc, ronic, 0.0, 1.0)
    expected = revenue * (1 + g) * margin * (1 - tax) / (wacc - g)
    assert res["ev"] == pytest.approx(expected, rel=1e-6)


def test_dcf_subtracts_net_debt():
    common = (1000.0, 0.2, 0.25, 0.0, 0.0, 0.0, [0.03] * 10, 0.03, 0.09, 0.2)
    no_debt = valuation.dcf_value(*common, 0.0, 10.0)
    with_debt = valuation.dcf_value(*common, 500.0, 10.0)
    assert no_debt["per_share"] - with_debt["per_share"] == pytest.approx(50.0)


def test_financials_have_their_own_return_bars():
    # A 1.3% ROA is strong for a bank but weak for an industrial.
    assert valuation.lin(0.013, *valuation._bar("roa", "financial")) > 80
    assert valuation.lin(0.013, *valuation._bar("roa", "heavy")) < 20
    assert valuation._bar("roa", "hybrid") == pytest.approx((0.0, 0.115))  # midpoint of light and heavy


def test_premium_inverts_for_yields():
    assert valuation._premium("ev_ebitda", 15.0, 10.0) == pytest.approx(0.5)  # 50% pricier
    assert valuation._premium("fcf_yield", 0.02, 0.04) == pytest.approx(1.0)  # half the yield = pricier
    assert valuation._premium("ev_ebitda", 15.0, None) is None


def test_banks_skip_dcf_and_ev_multiples():
    raw = make_raw("BANK", [_fy(100000, 20000, 50000, None), _fy(105000, 22000, -30000, None)], market_cap=300e9)
    r = analyze_full(raw, 0.04)
    v = r["valuation"]
    assert v["asset_class"] == "financial"
    assert not v["dcf"]["available"]
    assert v["multiples"]["ev_ebitda"] is None and v["multiples"]["fcf_yield"] is None


def test_unprofitable_company_skips_dcf():
    latest = _with_assets(_fy(1000, -200, -100, 30), operating_income=-150, ebit=-150)
    r = analyze_full(make_raw("LOSS", [_fy(800, -150, -80, 30), latest], market_cap=5e9), 0.04)
    assert not r["valuation"]["dcf"]["available"]
    assert "negative" in r["valuation"]["dcf"]["reason"]


def test_verdict_follows_base_case_upside():
    results = [analyze_full(raw, valuation.RISK_FREE_FALLBACK) for raw in demo_universe().values()]
    valuation.apply_relative(results)
    for r in results:
        dcf = r["valuation"]["dcf"]
        if dcf["available"]:
            up = dcf["scenarios"]["base"]["upside"]
            band = valuation.VALUATION_BAND
            expected = "Undervalued" if up > band else "Overvalued" if up < -band else "Fairly valued"
            assert r["valuation_verdict"] == expected
            bear, base, bull = (dcf["scenarios"][s]["per_share"] for s in ("bear", "base", "bull"))
            assert bear < base < bull
