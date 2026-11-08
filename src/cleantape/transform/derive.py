"""Derived data: best-source series, spreads, crosses, changes and z-scores.

* **Best series**: one value per (instrument, date). Primary source wins; the fallback source
  only fills dates the primary doesn't have. The chosen source is kept on every row.
* **Spreads** (bp) and **crosses** combine observations from the *same date only*.
* **Changes**: ``bp`` mode → difference in basis points (yields in % are ×100; spreads
  already in bp); ``pct`` mode → percentage change. 1d is vs the previous session on the
  series' own calendar and is null if that observation is missing. 1w / 1m / YTD use the
  last observation on or before the reference date, looking back at most ``ffill_max_days``.
* **z-score** of today's 1d move vs the trailing ``window`` daily moves, *excluding today*;
  null when fewer than ``min_obs`` moves are available.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd

from cleantape.calendars import get_calendar
from cleantape.config import Universe

DERIVED_COLUMNS = [
    "date",
    "series_id",
    "value",
    "source",
    "chg_1d",
    "chg_1w",
    "chg_1m",
    "chg_ytd",
    "z_1d",
]


@dataclass(frozen=True)
class SeriesMeta:
    series_id: str
    name: str
    asset_class: str
    units: str
    change_mode: str
    calendar: str
    display_group: str | None
    display_order: int
    kind: str  # instrument | spread | cross
    is_future: bool = False


def series_catalog(universe: Universe) -> dict[str, SeriesMeta]:
    """Metadata for every series the pipeline produces (instruments + spreads + crosses)."""
    cat = {
        i.id: SeriesMeta(
            i.id,
            i.name,
            i.asset_class,
            i.units,
            i.change_mode,
            i.calendar,
            i.display_group,
            i.display_order,
            "instrument",
            i.is_future,
        )
        for i in universe.instruments
    }
    for s in universe.spreads:
        cat[s.id] = SeriesMeta(
            s.id,
            s.name,
            "rates",
            "bp_spread",
            "bp",
            "us_bond",
            s.display_group,
            s.display_order,
            "spread",
        )
    for c in universe.crosses:
        left = universe.by_id[c.left]
        cat[c.id] = SeriesMeta(
            c.id, c.name, "fx", "fx_rate", "pct", left.calendar, None, 0, "cross"
        )
    return cat


# ------------------------------------------------------------------ best series


def best_series(obs: pd.DataFrame, universe: Universe) -> pd.DataFrame:
    """One non-null value per (instrument_id, date): primary source first, then fallback."""
    if obs.empty:
        return pd.DataFrame(columns=["date", "series_id", "value", "source"])
    primary = {i.id: i.source for i in universe.instruments}
    o = obs.dropna(subset=["value"]).copy()
    o["rank"] = np.where(o["source"].to_numpy() == o["instrument_id"].map(primary).to_numpy(), 0, 1)
    o = o.sort_values(["instrument_id", "date", "rank"]).drop_duplicates(
        ["instrument_id", "date"], keep="first"
    )
    return o.rename(columns={"instrument_id": "series_id"})[
        ["date", "series_id", "value", "source"]
    ].reset_index(drop=True)


def _wide(best: pd.DataFrame, ids: list[str]) -> pd.DataFrame:
    sub = best[best["series_id"].isin(ids)]
    return sub.pivot_table(index="date", columns="series_id", values="value", aggfunc="first")


def spreads(best: pd.DataFrame, universe: Universe) -> pd.DataFrame:
    """Spread series in bp = (long − short) × 100, only on dates where both legs exist."""
    out = []
    for s in universe.spreads:
        w = _wide(best, [s.long, s.short])
        if s.long not in w or s.short not in w:
            continue
        both = w[[s.long, s.short]].dropna()
        out.append(
            pd.DataFrame(
                {
                    "date": both.index,
                    "series_id": s.id,
                    "value": (both[s.long] - both[s.short]) * 100.0,
                    "source": "derived",
                }
            )
        )
    return pd.concat(out, ignore_index=True) if out else best.iloc[0:0]


def crosses(best: pd.DataFrame, universe: Universe) -> pd.DataFrame:
    """Implied crosses = left × right on dates where both legs exist."""
    out = []
    for c in universe.crosses:
        w = _wide(best, [c.left, c.right])
        if c.left not in w or c.right not in w:
            continue
        both = w[[c.left, c.right]].dropna()
        out.append(
            pd.DataFrame(
                {
                    "date": both.index,
                    "series_id": c.id,
                    "value": both[c.left] * both[c.right],
                    "source": "derived",
                }
            )
        )
    return pd.concat(out, ignore_index=True) if out else best.iloc[0:0]


# ------------------------------------------------------------------ changes


def change(new: np.ndarray, old: np.ndarray, mode: str, units: str) -> np.ndarray:
    """Change from ``old`` to ``new``: bp difference or % change."""
    new = np.asarray(new, dtype="float64")
    old = np.asarray(old, dtype="float64")
    if mode == "bp":
        scale = 1.0 if units == "bp_spread" else 100.0
        return (new - old) * scale
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (new / old - 1.0) * 100.0
    out[~np.isfinite(out)] = np.nan
    return out


def asof_values(
    dates: np.ndarray, values: np.ndarray, targets: list[dt.date], max_back_days: int
) -> np.ndarray:
    """Value of the last observation on or before each target, within ``max_back_days``."""
    idx = np.searchsorted(dates, np.array(targets, dtype="datetime64[D]"), side="right") - 1
    out = np.full(len(targets), np.nan)
    ok = idx >= 0
    found = np.where(ok, idx, 0)
    gap = (np.array(targets, dtype="datetime64[D]") - dates[found]).astype(int)
    good = ok & (gap <= max_back_days)
    out[good] = values[found[good]]
    return out


def _months_back(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 - n, 12)
    year, month = d.year + y, m + 1
    import calendar as _cal

    day = min(d.day, _cal.monthrange(year, month)[1])
    return dt.date(year, month, day)


def zscores(moves: np.ndarray, window: int, min_obs: int) -> np.ndarray:
    """z_t = (m_t − mean(m_{t−window..t−1})) / std(...), excluding today; NaN if < min_obs."""
    s = pd.Series(moves)
    hist = s.shift(1).rolling(window, min_periods=min_obs)
    mean, std = hist.mean(), hist.std(ddof=1)
    z = (s - mean) / std
    z[(std == 0) | std.isna()] = np.nan
    return z.to_numpy()


def series_changes(
    one: pd.DataFrame, meta: SeriesMeta, window: int, min_obs: int, ffill_max_days: int
) -> pd.DataFrame:
    """Add chg_1d/1w/1m/ytd and z_1d to one series (columns date, value, ...)."""
    one = one.sort_values("date").reset_index(drop=True)
    dates = np.array(one["date"].tolist(), dtype="datetime64[D]")
    vals = one["value"].to_numpy(dtype="float64")
    cal = get_calendar(meta.calendar)
    days: list[dt.date] = one["date"].tolist()
    prev_sess = [cal.previous(d) for d in days]
    # 1d: previous session's own observation must exist (no bridging gaps)
    prev_vals = asof_values(dates, vals, prev_sess, 0)
    w_ref = [cal.on_or_before(d - dt.timedelta(days=7)) for d in days]
    m_ref = [cal.on_or_before(_months_back(d, 1)) for d in days]
    y_ref = [cal.on_or_before(dt.date(d.year - 1, 12, 31)) for d in days]
    mode, units = meta.change_mode, meta.units
    out = one.copy()
    out["chg_1d"] = change(vals, prev_vals, mode, units)
    out["chg_1w"] = change(vals, asof_values(dates, vals, w_ref, ffill_max_days), mode, units)
    out["chg_1m"] = change(vals, asof_values(dates, vals, m_ref, ffill_max_days), mode, units)
    out["chg_ytd"] = change(vals, asof_values(dates, vals, y_ref, ffill_max_days), mode, units)
    moves = out["chg_1d"].to_numpy()
    valid = ~np.isnan(moves)
    z = np.full(len(moves), np.nan)
    z[valid] = zscores(moves[valid], window, min_obs)
    out["z_1d"] = z
    return out


def build_derived(
    obs: pd.DataFrame, universe: Universe, window: int, min_obs: int, ffill_max_days: int
) -> pd.DataFrame:
    """Full derived table for every series."""
    best = best_series(obs, universe)
    allv = pd.concat([best, spreads(best, universe), crosses(best, universe)], ignore_index=True)
    cat = series_catalog(universe)
    parts = []
    for sid, grp in allv.groupby("series_id", sort=True):
        meta = cat.get(str(sid))
        if meta is None or grp.empty:
            continue
        parts.append(series_changes(grp, meta, window, min_obs, ffill_max_days))
    if not parts:
        return pd.DataFrame(columns=DERIVED_COLUMNS)
    df = pd.concat(parts, ignore_index=True)[DERIVED_COLUMNS]
    df["series_id"] = df["series_id"].astype(str)
    return df.sort_values(["series_id", "date"]).reset_index(drop=True)
