"""Daily scores: percentile-rank Quality, Valuation and Technical across the universe, blend
them into a composite, and work out what moved since the last run.

Every score is a 0-100 percentile within the screened universe, so 80 always means "better
than 80% of today's universe" and the three can be weighted together. Quality and Valuation
come from valuation.py (the life-cycle cards); Technical comes from technical.py.

Score history is one CSV row per ticker per run date. Movers and state changes compare today
against the most recent earlier date in it.
"""

from pathlib import Path

import pandas as pd

import technical
from indicators import below_long_ma

HISTORY_COLUMNS = ["date", "ticker", "price", "quality", "valuation", "technical", "composite", "rank",
                   "phase", "verdict", "above_200d", "trend_state"]
SCORE_KEYS = ("quality", "valuation", "technical")
SCORE_LABELS = {"quality": "Quality", "valuation": "Valuation", "technical": "Technical"}


def market_regime(spy_close: pd.Series, window: int) -> str:
    """'uptrend' or 'downtrend': SPY's close vs its trailing `window`-day average."""
    return "downtrend" if below_long_ma(spy_close, window) else "uptrend"


def percentile(values: dict[str, float | None]) -> dict[str, float | None]:
    """0-100 percentile rank of each value among the non-missing ones (ties share the average rank)."""
    series = pd.Series({k: v for k, v in values.items() if v is not None}, dtype=float)
    ranks = (series.rank(pct=True) * 100).round(1) if not series.empty else series
    return {k: (float(ranks[k]) if k in ranks.index else None) for k in values}


def _weighted(parts: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """Weighted mean over the parts that exist, reweighting the rest."""
    present = [(weights[k], v) for k, v in parts.items() if v is not None and weights.get(k)]
    total = sum(w for w, _ in present)
    return round(sum(w * v for w, v in present) / total, 1) if total else None


def technical_scores(price_data: dict[str, pd.DataFrame], sectors: dict[str, str], rules: dict) -> dict[str, dict]:
    """{ticker: {technical, components, raw}}. The score is the average of the last
    `smoothing_days` daily scores (each ranked across the universe that day), so a single
    volatile session doesn't register as a move."""
    cfg = rules["technical_score"]
    weights = cfg["weights"]
    days = cfg["smoothing_days"]
    daily_scores: dict[str, list[float]] = {t: [] for t in price_data}
    today: dict[str, dict] = {}
    for offset in range(days):
        raw = {}
        for ticker, df in price_data.items():
            if len(df) <= offset:
                continue
            end = len(df) - offset
            f = technical.factors(df["Close"].iloc[:end], df["Volume"].iloc[:end], cfg, rules["ma_cross"])
            if f is not None:
                raw[ticker] = f
        sector_returns: dict[str, list[float]] = {}
        for ticker, f in raw.items():
            sector_returns.setdefault(sectors.get(ticker, ""), []).append(f["return_6m"])
        sector_median = {s: pd.Series(v).median() for s, v in sector_returns.items()}
        for ticker, f in raw.items():
            f["relative_strength"] = f["return_6m"] - sector_median[sectors.get(ticker, "")]
        components = {key: percentile({t: f[key] for t, f in raw.items()}) for key in weights}
        for ticker in raw:
            score = _weighted({key: components[key][ticker] for key in weights}, weights)
            if score is not None:
                daily_scores[ticker].append(score)
            if offset == 0:
                today[ticker] = {"raw": raw[ticker], "components": {key: components[key][ticker] for key in weights}}
    out = {}
    for ticker, info in today.items():
        scores = daily_scores[ticker]
        out[ticker] = {**info, "technical": round(sum(scores) / len(scores), 1) if scores else None}
    return out


def apply_scores(results: list[dict], tech: dict[str, dict], weights: dict[str, float]) -> None:
    """Adds r["daily"] = {quality, valuation, technical, composite, rank, ...} to each result."""
    quality = percentile({r["ticker"]: r.get("quality_score") for r in results})
    valuation = percentile({r["ticker"]: r.get("valuation_score") for r in results})
    technical_rank = percentile({r["ticker"]: (tech.get(r["ticker"]) or {}).get("technical") for r in results})
    for r in results:
        t = r["ticker"]
        parts = {"quality": quality[t], "valuation": valuation[t], "technical": technical_rank[t]}
        info = tech.get(t) or {}
        raw = info.get("raw") or {}
        r["daily"] = {**parts, "composite": _weighted(parts, weights), "technical_components": info.get("components") or {},
                      "technical_raw": raw, "above_200d": raw.get("above_200d"), "trend_state": raw.get("trend_state") or "n/a"}
    ranked = sorted((r for r in results if r["daily"]["composite"] is not None), key=lambda r: -r["daily"]["composite"])
    for i, r in enumerate(ranked, start=1):
        r["daily"]["rank"] = i
    for r in results:
        r["daily"].setdefault("rank", None)
        r["composite"] = r["daily"]["composite"]


def snapshot(results: list[dict], run_date: str) -> pd.DataFrame:
    rows = [{"date": run_date, "ticker": r["ticker"], "price": r.get("price"),
             **{k: r["daily"][k] for k in (*SCORE_KEYS, "composite", "rank", "above_200d", "trend_state")},
             "phase": r["phase"], "verdict": r.get("valuation_verdict")} for r in results]
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS)


def load_history(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    return pd.read_csv(path, dtype={"date": str, "ticker": str})


def save_history(history: pd.DataFrame, today: pd.DataFrame, path: Path) -> pd.DataFrame:
    """Replaces any rows already stored for today's date (a re-run), then appends today's."""
    run_date = today["date"].iloc[0]
    kept = history[history["date"] != run_date] if not history.empty else history
    combined = pd.concat([kept, today], ignore_index=True) if not kept.empty else today
    combined = combined.sort_values(["date", "rank"], na_position="last")
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(path, index=False)
    return combined


def _prior(history: pd.DataFrame, run_date: str, back: int) -> pd.DataFrame | None:
    """The snapshot `back` run-dates before run_date, indexed by ticker, or None."""
    dates = sorted(d for d in history["date"].unique() if d < run_date) if not history.empty else []
    if len(dates) < back:
        return None
    return history[history["date"] == dates[-back]].set_index("ticker")


def _driver(row: pd.Series, prev: pd.Series, weights: dict[str, float]) -> str:
    """Which score moved the composite most, with a human-readable reason."""
    contributions = {}
    for key in SCORE_KEYS:
        if pd.notna(row[key]) and pd.notna(prev[key]):
            contributions[key] = weights[key] * (row[key] - prev[key])
    if not contributions:
        return ""
    key = max(contributions, key=lambda k: abs(contributions[k]))
    delta = row[key] - prev[key]
    text = f"{SCORE_LABELS[key]} {delta:+.0f}"
    if key == "valuation" and pd.notna(row["price"]) and pd.notna(prev["price"]) and prev["price"]:
        text += f" (price {row['price'] / prev['price'] - 1:+.1%})"
    elif key == "quality":
        text += " (fundamentals updated)"
    elif key == "technical":
        text += f" ({row['trend_state'].lower()})" if isinstance(row["trend_state"], str) else ""
    return text


def changes(history: pd.DataFrame, today: pd.DataFrame, weights: dict[str, float], cfg: dict) -> dict:
    """Movers (composite change vs the previous and 5-runs-ago snapshots) and state changes."""
    run_date = today["date"].iloc[0]
    prev, prev5 = _prior(history, run_date, 1), _prior(history, run_date, 5)
    out = {"has_history": prev is not None, "prev_date": None, "up": [], "down": [], "states": []}
    if prev is None:
        return out
    out["prev_date"] = history[history["date"] < run_date]["date"].max()
    now = today.set_index("ticker")
    movers = []
    for ticker, row in now.iterrows():
        if ticker not in prev.index or pd.isna(row["composite"]) or pd.isna(prev.loc[ticker, "composite"]):
            continue
        before = prev.loc[ticker]
        d1 = row["composite"] - before["composite"]
        d5 = (row["composite"] - prev5.loc[ticker, "composite"]
              if prev5 is not None and ticker in prev5.index and pd.notna(prev5.loc[ticker, "composite"]) else None)
        if abs(d1) >= cfg["min_change"]:
            movers.append({"ticker": ticker, "composite": row["composite"], "d1": d1, "d5": d5,
                           "driver": _driver(row, before, weights)})
    out["up"] = sorted((m for m in movers if m["d1"] > 0), key=lambda m: -m["d1"])[: cfg["top_n"]]
    out["down"] = sorted((m for m in movers if m["d1"] < 0), key=lambda m: m["d1"])[: cfg["top_n"]]

    top = cfg["top_list_size"]
    states = []
    for ticker, row in now.iterrows():
        if ticker not in prev.index:
            continue
        before = prev.loc[ticker]
        if pd.notna(before["phase"]) and int(before["phase"]) != int(row["phase"]):
            states.append({"ticker": ticker, "kind": "Phase", "from": int(before["phase"]), "to": int(row["phase"])})
        if isinstance(before["verdict"], str) and before["verdict"] != row["verdict"]:
            states.append({"ticker": ticker, "kind": "Valuation", "from": before["verdict"], "to": row["verdict"]})
        if pd.notna(before["above_200d"]) and pd.notna(row["above_200d"]) and bool(before["above_200d"]) != bool(row["above_200d"]):
            states.append({"ticker": ticker, "kind": "Trend",
                           "from": "above 200-day" if bool(before["above_200d"]) else "below 200-day",
                           "to": "above 200-day" if bool(row["above_200d"]) else "below 200-day"})
        was_top = pd.notna(before["rank"]) and before["rank"] <= top
        is_top = pd.notna(row["rank"]) and row["rank"] <= top
        if was_top != is_top:
            states.append({"ticker": ticker, "kind": f"Top {top}", "from": "out" if is_top else "in",
                           "to": f"in (#{int(row['rank'])})" if is_top else "out"})
    out["states"] = states
    return out


def ticker_history(history: pd.DataFrame, ticker: str, days: int) -> list[dict]:
    """The last `days` stored snapshots for one ticker, oldest first (for the card's chart)."""
    if history.empty:
        return []
    rows = history[history["ticker"] == ticker].sort_values("date").tail(days)
    return [{"date": d, **{k: (None if pd.isna(row[k]) else float(row[k])) for k in (*SCORE_KEYS, "composite")}}
            for d, row in zip(rows["date"], rows.to_dict("records"))]
