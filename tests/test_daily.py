import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import email_digest  # noqa: E402
import scores  # noqa: E402
import technical  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RULES = yaml.safe_load((ROOT / "config" / "rules.yaml").read_text())
WEIGHTS = RULES["composite"]


def prices(drift: float, days: int = 400, seed: int = 0) -> pd.DataFrame:
    """Daily OHLCV-ish frame with a log-drift per day plus small noise."""
    rng = np.random.default_rng(seed)
    steps = drift + rng.normal(0, 0.01, days)
    close = 100 * np.exp(np.cumsum(steps))
    volume = rng.integers(900_000, 1_100_000, days).astype(float)
    index = pd.bdate_range(end="2026-11-13", periods=days)
    return pd.DataFrame({"Close": close, "Volume": volume}, index=index)


# ---- technical factors ----


def test_uptrend_reads_constructive():
    f = technical.factors(prices(0.002)["Close"], prices(0.002)["Volume"], RULES["technical_score"], RULES["ma_cross"])
    assert f["trend"] > 0 and f["momentum"] > 0
    assert f["trend_state"] == "Uptrend" and f["above_200d"]


def test_downtrend_reads_weak():
    df = prices(-0.002)
    f = technical.factors(df["Close"], df["Volume"], RULES["technical_score"], RULES["ma_cross"])
    assert f["trend"] < 0 and f["momentum"] < 0
    assert f["trend_state"] == "Downtrend" and not f["above_200d"]


def test_short_history_has_no_factors():
    df = prices(0.001, days=200)
    assert technical.factors(df["Close"], df["Volume"], RULES["technical_score"], RULES["ma_cross"]) is None


def test_technical_score_orders_trends():
    data = {"UP": prices(0.002, seed=1), "FLAT": prices(0.0, seed=2), "DOWN": prices(-0.002, seed=3)}
    tech = scores.technical_scores(data, {t: "Tech" for t in data}, RULES)
    assert tech["UP"]["technical"] > tech["FLAT"]["technical"] > tech["DOWN"]["technical"]
    assert set(tech["UP"]["components"]) == set(RULES["technical_score"]["weights"])


# ---- ranking and composite ----


def test_percentile_skips_missing_and_scales_0_100():
    ranks = scores.percentile({"A": 1.0, "B": 2.0, "C": None, "D": 3.0})
    assert ranks["C"] is None
    assert ranks["A"] < ranks["B"] < ranks["D"] == 100


def _result(ticker, quality, valuation, phase=4, verdict="Fairly valued", price=100.0):
    return {"ticker": ticker, "name": ticker, "quality_score": quality, "valuation_score": valuation,
            "phase": phase, "phase_name": "Capital Return", "valuation_verdict": verdict, "price": price, "base_upside": None}


def test_composite_weights_and_reweights_missing():
    results = [_result("A", 90, 10), _result("B", 10, 90)]
    tech = {"A": {"technical": 50, "components": {}, "raw": {"trend_state": "Uptrend", "above_200d": True}}}
    scores.apply_scores(results, tech, WEIGHTS)
    a, b = results[0]["daily"], results[1]["daily"]
    assert a["composite"] == pytest.approx(0.4 * 100 + 0.3 * 50 + 0.3 * 100)
    # B has no technical score: Quality and Valuation are reweighted to 40/30 of 70
    assert b["technical"] is None
    assert b["composite"] == pytest.approx((0.4 * 50 + 0.3 * 100) / 0.7, abs=0.1)
    assert a["rank"] == 1 and b["rank"] == 2


# ---- history, movers, state changes ----


def _snap(day, rows):
    return pd.DataFrame([{"date": day, "ticker": t, "price": p, "quality": q, "valuation": v, "technical": tc,
                          "composite": round(0.4 * q + 0.3 * v + 0.3 * tc, 1), "rank": rank, "phase": ph,
                          "verdict": vd, "above_200d": ab, "trend_state": "Uptrend" if ab else "Downtrend"}
                         for t, p, q, v, tc, rank, ph, vd, ab in rows], columns=scores.HISTORY_COLUMNS)


def test_save_history_replaces_a_rerun(tmp_path):
    path = tmp_path / "h.csv"
    first = _snap("2026-11-13", [("A", 10, 50, 50, 50, 1, 4, "Fairly valued", True)])
    history = scores.save_history(scores.load_history(path), first, path)
    rerun = _snap("2026-11-13", [("A", 11, 60, 50, 50, 1, 4, "Fairly valued", True)])
    history = scores.save_history(history, rerun, path)
    assert len(history) == 1 and history["quality"].iloc[0] == 60


def test_movers_and_driver_attribution():
    cfg = {"min_change": 5, "top_n": 8, "top_list_size": 1}
    history = _snap("2026-11-12", [
        ("A", 100, 50, 40, 50, 2, 4, "Fairly valued", True),
        ("B", 100, 60, 60, 60, 1, 4, "Fairly valued", True),
        ("C", 100, 50, 50, 50, 3, 3, "Overvalued", True),
    ])
    today = _snap("2026-11-13", [
        ("A", 90, 50, 80, 50, 1, 4, "Undervalued", True),   # price fell -> valuation up a lot
        ("B", 100, 60, 60, 30, 2, 4, "Fairly valued", False),  # technical down, lost the 200-day
        ("C", 100, 51, 50, 50, 3, 4, "Overvalued", True),   # tiny move, but phase 3 -> 4
    ])
    out = scores.changes(history, today, WEIGHTS, cfg)
    assert out["has_history"] and out["prev_date"] == "2026-11-12"
    assert [m["ticker"] for m in out["up"]] == ["A"] and [m["ticker"] for m in out["down"]] == ["B"]
    assert out["up"][0]["driver"] == "Valuation +40 (price -10.0%)"
    assert out["down"][0]["driver"].startswith("Technical -30")
    kinds = {(s["ticker"], s["kind"]) for s in out["states"]}
    assert {("A", "Valuation"), ("B", "Trend"), ("C", "Phase"), ("A", "Top 1"), ("B", "Top 1")} <= kinds


def test_first_run_has_no_movers():
    today = _snap("2026-11-13", [("A", 100, 50, 50, 50, 1, 4, "Fairly valued", True)])
    out = scores.changes(scores.load_history(Path("does-not-exist.csv")), today, WEIGHTS, RULES["changes"])
    assert not out["has_history"] and out["up"] == [] and out["states"] == []


# ---- email ----


def _email_result(ticker, rank, composite):
    return {"ticker": ticker, "name": f"{ticker} Inc", "phase": 4, "phase_name": "Capital Return",
            "valuation_verdict": "Overvalued", "base_upside": -0.2,
            "daily": {"rank": rank, "composite": composite, "quality": 90, "valuation": 20, "technical": 70,
                      "trend_state": "Uptrend", "alerts": ["Volume spike"] if rank == 1 else []}}


def test_email_links_every_ticker_to_its_card():
    results = [_email_result("AAA", 1, 80), _email_result("BBB", 2, 70)]
    changed = {"has_history": True, "prev_date": "2026-11-12",
               "up": [{"ticker": "AAA", "composite": 80, "d1": 7, "d5": 12, "driver": "Quality +10 (fundamentals updated)"}],
               "down": [], "states": [{"ticker": "BBB", "kind": "Phase", "from": 3, "to": 4}]}
    html, subject = email_digest.build_html(
        results, changed, run_date="2026-11-13", regime="uptrend", universe_label="test", error_count=0,
        risk_free=0.05, site_url="https://example.github.io/stock-screener", top_n=10, weights=WEIGHTS, min_change=5)
    assert "https://example.github.io/stock-screener/cards/AAA.html" in html
    assert "https://example.github.io/stock-screener/cards/BBB.html" in html
    assert "Operating Leverage → Capital Return" in html
    assert "Strongest on quality (90), weakest on valuation (20)." in html
    assert "DCF: Overvalued" in html
    assert subject == "[Stock Screener] Nov 13 · Top: AAA, BBB · 1 up, 0 down, 1 state changes"
    assert f"<title>{subject}</title>" in html


def test_email_first_run_explains_missing_movers():
    html, _ = email_digest.build_html(
        [_email_result("AAA", 1, 80)], {"has_history": False, "prev_date": None, "up": [], "down": [], "states": []},
        run_date="2026-11-13", regime="downtrend", universe_label="test", error_count=3, risk_free=0.05,
        site_url="https://x", top_n=10, weights=WEIGHTS, min_change=5)
    assert "No earlier snapshot yet" in html and "3 tickers skipped" in html
