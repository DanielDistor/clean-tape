"""MOCK portfolio: daily P&L, exposures and linear shock scenarios.

P&L rules (each unit-tested against hand-computed numbers):

* ``shares``      qty × (P_t − P_{t−1}) in USD (ETFs/equities; commodities are held via ETFs).
* ``dv01``        −dv01_usd × Δyield_bp. Positive DV01 = long duration, loses when yields rise.
* ``fx_notional`` quantity in base-currency units; P&L in quote ccy = N × (S_t − S_{t−1}),
                  converted to USD at S_t when the quote currency isn't USD
                  (USDJPY: JPY P&L ÷ USDJPY_t).

Dates: each position is valued on its instrument's own expected latest date t (yields lag
equities by a day), and t−1 is the previous session on that instrument's calendar. If either
value is missing, or the instrument has a FAIL check, the P&L is "n/a — data issue".
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, model_validator

from cleantape.calendars import completion_rule, expected_latest_date, get_calendar
from cleantape.config import Config, Shock, Universe

NA = "n/a — data issue"


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str
    instrument_id: str
    book: str
    kind: Literal["shares", "dv01", "fx_notional"]
    quantity: float | None = None
    dv01_usd: float | None = None
    entry_date: dt.date
    note: str | None = None

    @model_validator(mode="after")
    def _fields(self) -> Position:
        if self.kind == "dv01" and self.dv01_usd is None:
            raise ValueError(f"{self.position_id}: dv01 position needs dv01_usd")
        if self.kind != "dv01" and self.quantity is None:
            raise ValueError(f"{self.position_id}: {self.kind} position needs quantity")
        return self


def load_positions(path: Path, universe: Universe) -> list[Position]:
    """Read and validate portfolio.csv against the universe (fail fast)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    out = []
    for rec in df.to_dict("records"):
        rec = {k: (v if v != "" else None) for k, v in rec.items()}
        p = Position.model_validate(rec)
        inst = universe.by_id.get(p.instrument_id)
        if inst is None:
            raise ValueError(f"{p.position_id}: unknown instrument {p.instrument_id}")
        expected_units = {"shares": "price", "dv01": "pct_yield", "fx_notional": "fx_rate"}
        if inst.units != expected_units[p.kind]:
            raise ValueError(
                f"{p.position_id}: kind {p.kind} needs {expected_units[p.kind]} "
                f"instrument, {p.instrument_id} is {inst.units}"
            )
        if p.kind == "fx_notional" and len(p.instrument_id) != 6:
            raise ValueError(f"{p.position_id}: fx instrument id must be a 6-letter pair")
        if inst.is_future:
            raise ValueError(f"{p.position_id}: futures are display-only; hold the ETF instead")
        out.append(p)
    ids = [p.position_id for p in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate position_id in portfolio.csv")
    return out


# ------------------------------------------------------------------ pure P&L maths


def pnl_shares(qty: float, p_t: float, p_prev: float) -> float:
    return qty * (p_t - p_prev)


def pnl_dv01(dv01_usd: float, y_t_pct: float, y_prev_pct: float) -> float:
    """−DV01 × Δy in bp (yields stored in percent)."""
    return -dv01_usd * (y_t_pct - y_prev_pct) * 100.0


def to_usd(amount: float, ccy: str, fx: dict[str, float]) -> float:
    """Convert ``amount`` of ``ccy`` to USD using same-date pair levels in ``fx``.

    Looks for USD{ccy} (divide) or {ccy}USD (multiply). NaN if no rate is available.
    """
    if ccy == "USD":
        return amount
    if f"USD{ccy}" in fx and not np.isnan(fx[f"USD{ccy}"]):
        return amount / fx[f"USD{ccy}"]
    if f"{ccy}USD" in fx and not np.isnan(fx[f"{ccy}USD"]):
        return amount * fx[f"{ccy}USD"]
    return float("nan")


def pnl_fx(pair: str, notional: float, s_t: float, s_prev: float, fx: dict[str, float]) -> float:
    """N × ΔS in the quote currency, converted to USD at time-t rates."""
    quote = pair[3:]
    local = notional * (s_t - s_prev)
    if quote == "USD":
        return local
    if pair[:3] == "USD":  # e.g. USDJPY: JPY P&L ÷ USDJPY_t
        return local / s_t
    return to_usd(local, quote, fx)


def fx_exposure_usd(pair: str, notional: float, s_t: float, fx: dict[str, float]) -> float:
    """USD value of the base-currency leg."""
    base = pair[:3]
    if base == "USD":
        return notional
    if pair[3:] == "USD":
        return notional * s_t
    return to_usd(notional, base, fx)


# ------------------------------------------------------------------ valuation


def _value(wide: pd.DataFrame, iid: str, d: dt.date) -> float:
    try:
        v = wide.at[d, iid]
    except KeyError:
        return float("nan")
    return float(v) if pd.notna(v) else float("nan")


def value_positions(
    positions: list[Position],
    wide: pd.DataFrame,
    universe: Universe,
    as_of: dict[str, dt.date],
    flagged: set[str],
) -> pd.DataFrame:
    """One row per position for its instrument's date ``as_of[instrument_id]``.

    ``wide``: index = date, columns = series_id, values = best value.
    """
    rows = []
    for p in positions:
        inst = universe.by_id[p.instrument_id]
        t = as_of[p.instrument_id]
        prev = get_calendar(inst.calendar).previous(t)
        v_t, v_prev = _value(wide, p.instrument_id, t), _value(wide, p.instrument_id, prev)
        fx = {c: _value(wide, c, t) for c in wide.columns if len(c) == 6}
        reason = ""
        if p.instrument_id in flagged:
            reason = f"{p.instrument_id} has a FAIL data check"
        elif np.isnan(v_t):
            reason = f"no {p.instrument_id} value for {t}"
        elif np.isnan(v_prev):
            reason = f"no {p.instrument_id} value for {prev}"
        pnl = exposure = float("nan")
        dv01 = 0.0
        if p.kind == "shares":
            qty = float(p.quantity or 0)
            pnl = pnl_shares(qty, v_t, v_prev)
            exposure = qty * v_t
            move = (v_t / v_prev - 1) * 100
        elif p.kind == "dv01":
            dv01 = float(p.dv01_usd or 0)
            pnl = pnl_dv01(dv01, v_t, v_prev)
            exposure = 0.0
            move = (v_t - v_prev) * 100
        else:
            n = float(p.quantity or 0)
            pnl = pnl_fx(p.instrument_id, n, v_t, v_prev, fx)
            exposure = fx_exposure_usd(p.instrument_id, n, v_t, fx)
            move = (v_t / v_prev - 1) * 100
        if not reason and np.isnan(pnl):
            reason = "FX conversion rate missing"
        rows.append(
            {
                "position_id": p.position_id,
                "instrument_id": p.instrument_id,
                "book": p.book,
                "asset_class": inst.asset_class,
                "kind": p.kind,
                "quantity": p.quantity,
                "dv01_usd": p.dv01_usd,
                "as_of": t,
                "prev_date": prev,
                "value_t": v_t,
                "value_prev": v_prev,
                "move": move,
                "move_unit": "bp" if p.kind == "dv01" else "%",
                "pnl_usd": float("nan") if reason else pnl,
                "exposure_usd": exposure if not np.isnan(v_t) else float("nan"),
                "dv01_total": dv01,
                "status": NA if reason else "ok",
                "reason": reason,
                "note": p.note,
            }
        )
    return pd.DataFrame(rows)


def aggregate(pos: pd.DataFrame, by: str) -> pd.DataFrame:
    """P&L, gross/net exposure and DV01 by ``book`` / ``asset_class`` (n/a positions counted)."""
    g = pos.groupby(by, sort=True)
    out = pd.DataFrame(
        {
            "pnl_usd": g["pnl_usd"].sum(min_count=1),
            "positions": g.size(),
            "n_a": g["status"].apply(lambda s: int((s != "ok").sum())),
            "gross_exposure_usd": g["exposure_usd"].apply(lambda s: s.abs().sum()),
            "net_exposure_usd": g["exposure_usd"].sum(),
            "dv01_usd": g["dv01_total"].sum(),
        }
    ).reset_index()
    return out


def totals(pos: pd.DataFrame) -> dict[str, float]:
    return {
        "pnl_usd": float(pos["pnl_usd"].sum(min_count=1))
        if pos["pnl_usd"].notna().any()
        else float("nan"),
        "n_a": int((pos["status"] != "ok").sum()),
        "gross_exposure_usd": float(pos["exposure_usd"].abs().sum()),
        "net_exposure_usd": float(pos["exposure_usd"].sum()),
        "dv01_usd": float(pos["dv01_total"].sum()),
    }


def shock_pnl(pos: pd.DataFrame, shock: Shock) -> float:
    """Linear first-order P&L of one shock scenario."""
    total = 0.0
    for r in pos.itertuples():
        if np.isnan(r.value_t):
            continue
        if shock.kind == "rates_bp" and r.kind == "dv01":
            total += -float(r.dv01_usd) * shock.size
        elif (
            shock.kind == "asset_class_pct" and r.kind == "shares" and r.asset_class == shock.target
        ) or (
            shock.kind == "instruments_pct"
            and r.kind == "shares"
            and r.instrument_id in (shock.target or "").split(",")
        ):
            total += float(r.exposure_usd) * shock.size / 100
        elif shock.kind == "usd_pct" and r.kind == "fx_notional":
            pair, n = r.instrument_id, float(r.quantity)
            if pair[:3] == "USD":  # USD up → USDxxx up; P&L ≈ N(USD) × size%
                total += n * shock.size / 100
            elif pair[3:] == "USD":  # USD up → xxxUSD down
                total += -n * float(r.value_t) * shock.size / 100
    return total


def shocks_table(pos: pd.DataFrame, shocks: list[Shock]) -> pd.DataFrame:
    return pd.DataFrame([{"scenario": s.name, "pnl_usd": shock_pnl(pos, s)} for s in shocks])


def pnl_history(
    positions: list[Position], wide: pd.DataFrame, universe: Universe, dates: list[dt.date]
) -> pd.DataFrame:
    """Daily total P&L for each date in ``dates`` (positions with missing data contribute 0)."""
    rows = []
    for d in dates:
        as_of = {
            p.instrument_id: get_calendar(universe.by_id[p.instrument_id].calendar).on_or_before(d)
            for p in positions
        }
        v = value_positions(positions, wide, universe, as_of, set())
        rows.append(
            {
                "date": d,
                "pnl_usd": float(v["pnl_usd"].fillna(0).sum()),
                "n_a": int((v["status"] != "ok").sum()),
            }
        )
    h = pd.DataFrame(rows)
    h["cum_pnl_usd"] = h["pnl_usd"].cumsum()
    return h


def expected_dates(
    cfg: Config, positions: list[Position], now_utc: dt.datetime
) -> dict[str, dt.date]:
    """Each position instrument's expected latest date (calendar + lag + run time)."""
    out = {}
    for p in positions:
        inst = cfg.universe.by_id[p.instrument_id]
        out[p.instrument_id] = expected_latest_date(
            get_calendar(inst.calendar),
            now_utc,
            inst.expected_lag_business_days,
            completion_rule(inst.source, inst.calendar, inst.is_future),
            cfg.settings.nyse_close_buffer,
        )
    return out


def wide_values(derived: pd.DataFrame) -> pd.DataFrame:
    """date × series_id matrix of values from the derived table."""
    return derived.pivot_table(index="date", columns="series_id", values="value", aggfunc="first")
