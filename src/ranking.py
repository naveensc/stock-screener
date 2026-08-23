"""Combine the technical and quality-value screens into a single ranked daily shortlist.

Two adjustments on top of the raw per-screen scores:
  - Regime awareness: a bullish signal (e.g. RSI oversold) means something different in a
    confirmed market uptrend than in a broad selloff, and vice versa for bearish signals.
    Signals fighting the current regime are down-weighted rather than dropped.
  - Confluence: a ticker that clears the quality-value hard filter AND has a technical entry
    trigger firing today is a stronger claim than either alone, and is rewarded accordingly.
"""

import pandas as pd

from indicators import below_long_ma

BULLISH_SIGNALS = {"RSI oversold", "Golden cross (50/200)", "At lower Bollinger Band"}
BEARISH_SIGNALS = {"RSI overbought", "Death cross (50/200)", "At upper Bollinger Band"}
# "Volume spike" is direction-agnostic and always counts at full weight.


def market_regime(spy_close: pd.Series, window: int) -> str:
    """Returns 'uptrend' or 'downtrend' based on SPY's close vs. its trailing `window`-day MA."""
    below = below_long_ma(spy_close, window)
    return "downtrend" if below else "uptrend"


def _regime_adjusted_technical_score(signals: list[str], regime: str, misaligned_weight: float) -> float:
    aligned = BULLISH_SIGNALS if regime == "uptrend" else BEARISH_SIGNALS
    misaligned = BEARISH_SIGNALS if regime == "uptrend" else BULLISH_SIGNALS

    score = 0.0
    for signal in signals:
        if signal in misaligned:
            score += misaligned_weight
        elif signal in aligned:
            score += 1.0
        else:
            score += 1.0  # neutral (e.g. volume spike)
    return score


def build_top_ideas(
    matches: list[dict],
    quality_value_matches: list[dict],
    regime: str,
    cfg: dict,
) -> list[dict]:
    """Returns the top `cfg['top_n']` tickers across both screens, ranked by a single
    composite score, highest first.
    """
    technical_by_ticker = {m["ticker"]: m for m in matches}
    quality_value_by_ticker = {m["ticker"]: m for m in quality_value_matches}
    all_tickers = set(technical_by_ticker) | set(quality_value_by_ticker)

    ranked = []
    for ticker in all_tickers:
        technical = technical_by_ticker.get(ticker)
        quality_value = quality_value_by_ticker.get(ticker)
        base = quality_value or technical
        signals = base["signals"]

        technical_component = _regime_adjusted_technical_score(signals, regime, cfg["regime_misaligned_weight"])

        fundamental_component = 0.0
        if quality_value is not None:
            fundamental_component = (
                quality_value["revenue_growth"] + quality_value["eps_growth"]
            ) * cfg["fundamental_weight"] + quality_value["pct_below_high"] * cfg["discount_weight"]

        is_confluence = technical is not None and quality_value is not None
        confluence_bonus = cfg["confluence_bonus"] if is_confluence else 0.0

        ranked.append(
            {
                "ticker": ticker,
                "name": base["name"],
                "sector": base["sector"],
                "price": base["price"],
                "signals": signals,
                "composite_score": technical_component + fundamental_component + confluence_bonus,
                "confluence": is_confluence,
                "source": "confluence" if is_confluence else ("quality_value" if quality_value else "technical"),
            }
        )

    ranked.sort(key=lambda m: m["composite_score"], reverse=True)
    return ranked[: cfg["top_n"]]
