"""Technical indicator calculations for the screener.

Each *_signal function takes a price/volume series plus the relevant slice of
config/rules.yaml and returns a dict describing whether that ticker matches
the criterion today. See PLAN.md for the reasoning behind each approach.
"""

import pandas as pd


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi[avg_loss == 0] = 100
    rsi[(avg_gain == 0) & (avg_loss == 0)] = 50
    return rsi


def rsi_signal(close: pd.Series, cfg: dict) -> dict:
    """Flag oversold/overbought using each ticker's own rolling RSI percentile rank.

    Falls back to the static global threshold (cfg['fallback_oversold']/
    ['fallback_overbought']) when there isn't enough history for the
    percentile calculation (e.g. a recent IPO).
    """
    period = cfg["period"]
    lookback = cfg["lookback_days"]
    rsi = compute_rsi(close, period)
    rsi_valid = rsi.dropna()

    if rsi_valid.empty:
        return {"rsi": None, "oversold": False, "overbought": False, "method": "none"}

    current_rsi = rsi_valid.iloc[-1]

    if len(rsi_valid) >= lookback:
        window = rsi_valid.iloc[-lookback:]
        percentile_rank = (window <= current_rsi).mean() * 100
        return {
            "rsi": current_rsi,
            "percentile_rank": percentile_rank,
            "oversold": percentile_rank <= cfg["oversold_percentile"],
            "overbought": percentile_rank >= cfg["overbought_percentile"],
            "method": "percentile",
        }

    return {
        "rsi": current_rsi,
        "percentile_rank": None,
        "oversold": current_rsi <= cfg["fallback_oversold"],
        "overbought": current_rsi >= cfg["fallback_overbought"],
        "method": "fallback",
    }


def ma_cross_signal(close: pd.Series, cfg: dict) -> dict:
    """Flag a golden/death cross that occurred on the most recent trading day."""
    fast_window = cfg["fast"]
    slow_window = cfg["slow"]
    if len(close) < slow_window + 1:
        return {"golden_cross": False, "death_cross": False}

    fast_ma = close.rolling(fast_window).mean()
    slow_ma = close.rolling(slow_window).mean()

    golden_cross = fast_ma.iloc[-1] > slow_ma.iloc[-1] and fast_ma.iloc[-2] <= slow_ma.iloc[-2]
    death_cross = fast_ma.iloc[-1] < slow_ma.iloc[-1] and fast_ma.iloc[-2] >= slow_ma.iloc[-2]
    return {"golden_cross": bool(golden_cross), "death_cross": bool(death_cross)}


def volume_spike_signal(volume: pd.Series, cfg: dict) -> dict:
    lookback = cfg["lookback_days"]
    if len(volume) < lookback + 1:
        return {"spike": False, "ratio": None}

    avg_volume = volume.shift(1).rolling(lookback).mean().iloc[-1]
    current_volume = volume.iloc[-1]
    if pd.isna(avg_volume) or avg_volume == 0:
        return {"spike": False, "ratio": None}

    ratio = current_volume / avg_volume
    return {"spike": ratio >= cfg["spike_multiple"], "ratio": ratio}


def pct_below_52w_high(close: pd.Series, window: int = 252) -> float | None:
    """Returns how far below its trailing-year high today's close is (0.20 = 20% below)."""
    if close.empty:
        return None
    window_close = close.iloc[-window:] if len(close) >= window else close
    high = window_close.max()
    if high <= 0:
        return None
    return (high - close.iloc[-1]) / high


def below_long_ma(close: pd.Series, window: int) -> bool | None:
    """Returns whether today's close is below the trailing `window`-day moving average."""
    if len(close) < window:
        return None
    ma = close.rolling(window).mean().iloc[-1]
    return bool(close.iloc[-1] < ma)


def trailing_return(close: pd.Series, window_days: int) -> float | None:
    """Returns the % price change over the trailing `window_days`, or None without enough history."""
    if len(close) <= window_days:
        return None
    past = close.iloc[-window_days - 1]
    if past == 0:
        return None
    return (close.iloc[-1] - past) / past


def bollinger_signal(close: pd.Series, cfg: dict) -> dict:
    window = cfg["window"]
    num_std = cfg["num_std"]
    if len(close) < window:
        return {"at_upper": False, "at_lower": False}

    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    current_close = close.iloc[-1]

    return {
        "at_upper": bool(current_close >= upper.iloc[-1]),
        "at_lower": bool(current_close <= lower.iloc[-1]),
        "upper": upper.iloc[-1],
        "lower": lower.iloc[-1],
    }
