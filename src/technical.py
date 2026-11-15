"""Continuous technical factors for the daily score.

Replaces counting one-day events (which had no direction and was zero most days) with factor
values every ticker has every day, each oriented so that higher = more constructive:

  trend              price vs its 50/200-day averages and the 50-day average's slope
  momentum           12-1 month return (skips the latest month, which tends to mean-revert)
  relative_strength  6-month return minus its sector's median 6-month return
  rsi                100 - RSI percentile vs its own past year (a pullback scores higher)
  volume             up-day volume / down-day volume over the window (accumulation)

scores.py turns each factor into a 0-100 percentile across the universe so factors in
different units can be weighted together. The old one-day events (golden cross, volume spike,
band touches) live on as alert tags via screen.screen_ticker — they annotate, they don't score.
"""

import pandas as pd

from indicators import compute_rsi


def factors(close: pd.Series, volume: pd.Series, cfg: dict, ma_cfg: dict) -> dict | None:
    """Raw factor values as of the last row. None if there isn't a full year of history."""
    lookback, skip = cfg["momentum_lookback_days"], cfg["momentum_skip_days"]
    if len(close) <= lookback:
        return None
    fast = close.rolling(ma_cfg["fast"]).mean()
    slow = close.rolling(ma_cfg["slow"]).mean()
    last, fast_now, slow_now = close.iloc[-1], fast.iloc[-1], slow.iloc[-1]
    slope_days = cfg["ma_slope_days"]

    rsi = compute_rsi(close).dropna()
    window = rsi.iloc[-lookback:]
    rsi_percentile = (window <= rsi.iloc[-1]).mean() * 100 if not window.empty else None

    vol_window = cfg["volume_window_days"]
    moves = close.diff().iloc[-vol_window:]
    vols = volume.iloc[-vol_window:]
    down_volume = vols[moves < 0].sum()
    volume_ratio = vols[moves > 0].sum() / down_volume if down_volume > 0 else None

    above_slow = bool(last > slow_now)
    if above_slow and fast_now > slow_now:
        trend_state = "Uptrend"
    elif not above_slow and fast_now < slow_now:
        trend_state = "Downtrend"
    else:
        trend_state = "Mixed"

    return {
        # trend is the mean of three distances, each roughly a % — kept as one number so the
        # trend factor isn't triple-weighted against the others
        "trend": ((last / fast_now - 1) + (last / slow_now - 1) + (fast_now / fast.iloc[-1 - slope_days] - 1)) / 3,
        "momentum": close.iloc[-1 - skip] / close.iloc[-1 - lookback] - 1,
        "return_6m": last / close.iloc[-1 - cfg["relative_strength_days"]] - 1,
        "rsi": 100 - rsi_percentile if rsi_percentile is not None else None,
        "rsi_value": rsi.iloc[-1],
        "volume": volume_ratio,
        "above_200d": above_slow,
        "trend_state": trend_state,
        "price": last,
    }
