"""Raw drops → canonical ``observations`` rows (date, instrument_id, source, value, ...).

Rules:
* missing values stay missing — no forward-fill in storage;
* a source-reported missing value (FRED ``.``) on a non-session day (holiday) is dropped;
  on a session day it is kept as null so the validity check flags it;
* weekend-dated rows are dropped (no calendar we use trades on weekends);
* in-progress daily bars are dropped (``calendars.bar_is_complete``), judged at the
  drop's ``received_at_utc``;
* upsert on (date, instrument_id, source); a changed value is a revision and is logged.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from collections.abc import Iterable

import numpy as np
import pandas as pd

from cleantape.calendars import bar_is_complete, completion_rule, get_calendar
from cleantape.config import Instrument, Universe
from cleantape.ingest.landing import Landed
from cleantape.logging_setup import get_logger

log = get_logger("normalize")

FRED_DATE_COLUMNS = ("observation_date", "DATE", "date")


class ParseError(ValueError):
    """A raw drop could not be parsed into the expected shape."""


def _frame(dates: Iterable, symbols: Iterable, values: Iterable) -> pd.DataFrame:
    df = pd.DataFrame({"date": list(dates), "symbol": list(symbols), "value": list(values)})
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["symbol"] = df["symbol"].astype(str)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    return df


# ------------------------------------------------------------------ FRED


def parse_fred_csv(body: bytes) -> pd.DataFrame:
    """Parse fredgraph.csv (one or more series). ``.`` / empty → null.

    The date column has been named ``DATE`` and ``observation_date`` over time; accept either.
    """
    try:
        raw = pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)
    except Exception as e:
        raise ParseError(f"FRED CSV unreadable: {e}") from e
    date_col = next((c for c in raw.columns if c.strip() in FRED_DATE_COLUMNS), None)
    if date_col is None:
        raise ParseError(f"FRED CSV has no date column; header={list(raw.columns)}")
    series = [c for c in raw.columns if c != date_col]
    if not series:
        raise ParseError("FRED CSV has no value columns")
    long = raw.melt(id_vars=date_col, value_vars=series, var_name="symbol", value_name="value")
    long["value"] = long["value"].str.strip().replace({".": None, "": None})
    return _frame(long[date_col], long["symbol"].str.strip(), long["value"])


def parse_fred_json(body: bytes, series_id: str) -> pd.DataFrame:
    """Parse the keyed FRED API ``series/observations`` JSON."""
    obs = json.loads(body).get("observations")
    if obs is None:
        raise ParseError("FRED JSON has no 'observations'")
    vals = [None if o.get("value") in (".", "") else o.get("value") for o in obs]
    return _frame([o["date"] for o in obs], [series_id] * len(obs), vals)


# ------------------------------------------------------------------ Yahoo / Nasdaq


def parse_yahoo_long(body: bytes) -> pd.DataFrame:
    """Parse our long-CSV serialisation of a yfinance download (date,symbol,close)."""
    if not body:
        return _frame([], [], [])
    raw = pd.read_csv(io.BytesIO(body), dtype={"symbol": str})
    need = {"date", "symbol", "close"}
    if not need <= set(raw.columns):
        raise ParseError(f"yahoo drop missing columns {need - set(raw.columns)}")
    return _frame(raw["date"], raw["symbol"], raw["close"])


def _money(s: str) -> float | None:
    s = (s or "").replace("$", "").replace(",", "").strip()
    return float(s) if s not in ("", "N/A") else None


def parse_nasdaq_json(body: bytes, symbol: str) -> pd.DataFrame:
    """Parse api.nasdaq.com historical JSON. Dates are MM/DD/YYYY; prices may carry '$'."""
    data = json.loads(body).get("data") or {}
    rows = (data.get("tradesTable") or {}).get("rows")
    if rows is None:
        raise ParseError("nasdaq JSON has no tradesTable.rows")
    dates = [dt.datetime.strptime(r["date"], "%m/%d/%Y").date() for r in rows]  # noqa: DTZ007
    return _frame(dates, [symbol] * len(rows), [_money(r.get("close")) for r in rows])


# ------------------------------------------------------------------ ECB


def parse_ecb_csv(body: bytes) -> pd.DataFrame:
    """Parse ECB SDMX csvdata: returns (date, symbol=currency code, value=units per 1 EUR)."""
    try:
        raw = pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)
    except Exception as e:
        raise ParseError(f"ECB CSV unreadable: {e}") from e
    need = {"CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE"}
    if not need <= set(raw.columns):
        raise ParseError(f"ECB CSV missing columns {sorted(need - set(raw.columns))}")
    raw = raw[raw["CURRENCY_DENOM"] == "EUR"]
    vals = raw["OBS_VALUE"].replace({"": None, "NaN": None})
    return _frame(raw["TIME_PERIOD"], raw["CURRENCY"], vals)


def ecb_cross(per_eur: dict[str, float], pair: str) -> float:
    """Convert ECB quotes (units of CCY per 1 EUR) into a Yahoo-convention pair.

    Pair ``XXXYYY`` = units of YYY per 1 XXX = (YYY per EUR) / (XXX per EUR), with EUR per
    EUR = 1. So EURUSD = USD; USDJPY = JPY / USD; GBPUSD = USD / GBP; EURJPY = JPY.
    """
    if len(pair) != 6:
        raise ValueError(f"pair must be 6 letters, got {pair!r}")
    base, quote = pair[:3], pair[3:]
    rates = {**per_eur, "EUR": 1.0}
    try:
        return rates[quote] / rates[base]
    except KeyError as e:
        raise KeyError(f"ECB rate for {e.args[0]} missing; cannot build {pair}") from e


def ecb_pairs(per_eur_long: pd.DataFrame, pairs: list[str]) -> pd.DataFrame:
    """Build Yahoo-convention pairs per date; a pair is null if either leg is missing that day."""
    if per_eur_long.empty:
        return _frame([], [], [])
    wide = per_eur_long.pivot_table(
        index="date", columns="symbol", values="value", aggfunc="first", dropna=False
    )
    wide["EUR"] = 1.0
    out = []
    for pair in pairs:
        base, quote = pair[:3], pair[3:]
        if base not in wide or quote not in wide:
            log.warning("ECB drop lacks a leg for %s; skipped", pair)
            continue
        s = wide[quote] / wide[base]  # same-date legs only; NaN if either is missing
        out.append(_frame(s.index, [pair] * len(s), s.to_numpy()))
    return pd.concat(out, ignore_index=True) if out else _frame([], [], [])


# ------------------------------------------------------------------ drops → observations


def parse_drop(landed: Landed, universe: Universe) -> pd.DataFrame:
    """Parse one landed drop into (date, symbol, value) keyed by the source's own symbols."""
    d = landed.drop
    if d.source == "fred":
        return (
            parse_fred_csv(d.content) if d.ext == "csv" else parse_fred_json(d.content, d.dataset)
        )
    if d.source == "yahoo":
        return parse_yahoo_long(d.content)
    if d.source == "nasdaq":
        return parse_nasdaq_json(d.content, d.dataset)
    if d.source == "ecb":
        pairs = [i.source_symbol for i in universe.by_source("ecb")]
        return ecb_pairs(parse_ecb_csv(d.content), pairs)
    raise ParseError(f"no parser for source {d.source!r}")


def symbol_map(universe: Universe, source: str) -> dict[str, Instrument]:
    """source_symbol → instrument, including fallback symbols for that source."""
    m = {i.source_symbol: i for i in universe.instruments if i.source == source}
    for i in universe.instruments:
        if i.fallback and i.fallback.source == source:
            m[i.fallback.source_symbol] = i
    return m


def drop_partial_bars(
    df: pd.DataFrame, inst: Instrument, received_at_utc: dt.datetime, close_buffer: dt.time
) -> pd.DataFrame:
    """Remove rows whose daily bar was not final when the drop was received."""
    if df.empty:
        return df
    rule = completion_rule(inst.source, inst.calendar, inst.is_future)
    keep = [bar_is_complete(d, received_at_utc, rule, close_buffer) for d in df["date"]]
    return df[np.array(keep, dtype=bool)]


def drop_weekends(df: pd.DataFrame) -> pd.DataFrame:
    """Drop Saturday/Sunday-dated rows. None of our calendars trade on weekends; Yahoo emits
    in-progress weekend FX bars and ICE marks HY OAS on month-end weekend days."""
    if df.empty:
        return df
    wd = np.array([d.weekday() < 5 for d in df["date"]], dtype=bool)
    return df[wd]


def drop_holiday_nulls(df: pd.DataFrame, inst: Instrument) -> pd.DataFrame:
    """Drop null values on dates that aren't sessions on the instrument's calendar."""
    if df.empty:
        return df
    cal = get_calendar(inst.calendar)
    is_sess = np.array([cal.is_session(d) for d in df["date"]], dtype=bool)
    return df[df["value"].notna().to_numpy() | is_sess]


def to_observations(
    landed: list[Landed], universe: Universe, close_buffer: dt.time
) -> tuple[pd.DataFrame, list[str]]:
    """Parse every drop that needs processing. Returns (observations, parse errors)."""
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for ld in landed:
        if not ld.needs_processing:
            continue
        d = ld.drop
        try:
            parsed = parse_drop(ld, universe)
        except Exception as e:
            msg = f"parse failed source={d.source} dataset={d.dataset}: {e}"
            log.error(msg)
            errors.append(msg)
            continue
        smap = symbol_map(universe, d.source)
        for sym, grp in parsed.groupby("symbol", sort=True):
            inst = smap.get(str(sym))
            if inst is None:
                continue
            n_before = len(grp)
            grp = drop_partial_bars(grp, inst, d.received_at_utc, close_buffer)
            ld.deferred += n_before - len(grp)
            grp = drop_holiday_nulls(drop_weekends(grp), inst)
            frames.append(
                pd.DataFrame(
                    {
                        "date": grp["date"].to_numpy(),
                        "instrument_id": inst.id,
                        "source": d.source,
                        "value": grp["value"].to_numpy(),
                        "ingested_at_utc": d.received_at_utc,
                        "raw_sha256": d.sha256,
                    }
                )
            )
    if not frames:
        return empty_observations(), errors
    obs = pd.concat(frames, ignore_index=True)
    obs = obs.drop_duplicates(subset=["date", "instrument_id", "source"], keep="last")
    return obs.reset_index(drop=True), errors


def empty_observations() -> pd.DataFrame:
    from cleantape.store.state import empty_frame

    return empty_frame("observations")


def upsert(existing: pd.DataFrame, incoming: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Merge on (date, instrument_id, source). Returns (table, n_inserted, n_revised).

    Unchanged values keep their original ``ingested_at_utc``/``raw_sha256``; changed values
    (revisions) take the incoming row.
    """
    key = ["date", "instrument_id", "source"]
    if incoming.empty:
        return existing, 0, 0
    if existing.empty:
        return incoming.sort_values(key).reset_index(drop=True), len(incoming), 0
    merged = incoming.merge(
        existing[[*key, "value"]], on=key, how="left", suffixes=("", "_old"), indicator=True
    )
    is_new = merged["_merge"] == "left_only"
    both = ~is_new
    old, new = merged["value_old"], merged["value"]
    changed = both & ~((old == new) | (old.isna() & new.isna()))
    take = merged[is_new | changed][incoming.columns]
    idx_existing = existing.set_index(key).index
    idx_take = pd.MultiIndex.from_frame(take[key])
    kept = existing[~idx_existing.isin(idx_take)]
    out = pd.concat([kept, take], ignore_index=True).sort_values(key).reset_index(drop=True)
    n_rev = int(changed.sum())
    if n_rev:
        sample = merged[changed][[*key, "value_old", "value"]].head(5).to_dict("records")
        log.info("revisions: %d values changed, e.g. %s", n_rev, sample)
    return out, int(is_new.sum()), n_rev
