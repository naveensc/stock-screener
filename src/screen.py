"""Apply the screening criteria to each ticker's price history and score/rank matches."""

import pandas as pd

from indicators import bollinger_signal, ma_cross_signal, rsi_signal, volume_spike_signal


def screen_ticker(ticker: str, df: pd.DataFrame, rules: dict) -> dict | None:
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

    return {
        "ticker": ticker,
        "price": close.iloc[-1],
        "score": len(signals),
        "signals": signals,
        "rsi": rsi["rsi"],
    }


def screen_universe(price_data: dict[str, pd.DataFrame], rules: dict) -> list[dict]:
    """Screen every ticker, returning matches sorted by score descending."""
    matches = []
    for ticker, df in price_data.items():
        try:
            result = screen_ticker(ticker, df, rules)
        except Exception:
            continue
        if result:
            matches.append(result)

    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches
