"""One-day technical events for a ticker (RSI extremes, golden/death cross, volume spike,
Bollinger touches). The daily run shows them as alert tags; they don't feed the scores."""

import pandas as pd

from indicators import bollinger_signal, ma_cross_signal, rsi_signal, volume_spike_signal


def screen_ticker(ticker: str, df: pd.DataFrame, rules: dict, ticker_info: dict) -> dict | None:
    """Score a single ticker against all criteria. Returns None if nothing matched."""
    close = df["Close"]
    volume = df["Volume"]

    rsi = rsi_signal(close, rules["rsi"])
    ma_cross = ma_cross_signal(close, rules["ma_cross"])
    volume_spike = volume_spike_signal(volume, rules["volume"])
    bollinger = bollinger_signal(close, rules["bollinger"])

    signals = []
    if rsi["oversold"]:
        signals.append("RSI oversold")
    if rsi["overbought"]:
        signals.append("RSI overbought")
    if ma_cross["golden_cross"]:
        signals.append("Golden cross (50/200)")
    if ma_cross["death_cross"]:
        signals.append("Death cross (50/200)")
    if volume_spike["spike"]:
        signals.append("Volume spike")
    if bollinger["at_upper"]:
        signals.append("At upper Bollinger Band")
    if bollinger["at_lower"]:
        signals.append("At lower Bollinger Band")

    if not signals:
        return None

    info = ticker_info.get(ticker, {})
    return {
        "ticker": ticker,
        "name": info.get("name", ticker),
        "sector": info.get("sector", "Unknown"),
        "price": close.iloc[-1],
        "score": len(signals),
        "signals": signals,
        "rsi": rsi["rsi"],
    }

