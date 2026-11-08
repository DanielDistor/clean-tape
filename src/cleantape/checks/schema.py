"""Schema: each raw drop in the latest run has the columns and types its parser expects."""

from __future__ import annotations

import io
import json

import pandas as pd

from cleantape.checks.base import CheckContext, CheckResult, Severity, result
from cleantape.transform.normalize import FRED_DATE_COLUMNS

NAME = "schema"


def _numeric_problems(values: pd.Series, allowed_missing: set[str]) -> list[str]:
    v = values.astype(str).str.strip()
    v = v[~v.isin(allowed_missing)]
    bad = v[pd.to_numeric(v, errors="coerce").isna()]
    return [f"{len(bad)} non-numeric values, e.g. {bad.iloc[0]!r}"] if len(bad) else []


def _date_problems(values: pd.Series, fmt: str | None = None) -> list[str]:
    parsed = pd.to_datetime(values, errors="coerce", format=fmt)
    n = int(parsed.isna().sum())
    return [f"{n} unparseable dates"] if n else []


def validate(source: str, body: bytes) -> list[str]:
    """Return a list of schema problems for a raw drop body (empty list = valid)."""
    if not body:
        return ["empty body"]
    try:
        if source == "fred":
            df = pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)
            dcol = next((c for c in df.columns if c in FRED_DATE_COLUMNS), None)
            if dcol is None:
                return [f"no date column (got {list(df.columns)})"]
            vals = [c for c in df.columns if c != dcol]
            if len(vals) != 1:
                return [f"expected 1 value column, got {vals}"]
            return _date_problems(df[dcol]) + _numeric_problems(df[vals[0]], {".", ""})
        if source == "ecb":
            df = pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)
            need = {"CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE"}
            if not need <= set(df.columns):
                return [f"missing columns {sorted(need - set(df.columns))}"]
            return _date_problems(df["TIME_PERIOD"]) + _numeric_problems(
                df["OBS_VALUE"], {"", "NaN"}
            )
        if source == "yahoo":
            df = pd.read_csv(io.BytesIO(body), dtype=str, keep_default_na=False)
            need = {"date", "symbol", "close"}
            if not need <= set(df.columns):
                return [f"missing columns {sorted(need - set(df.columns))}"]
            return _date_problems(df["date"]) + _numeric_problems(df["close"], set())
        if source == "nasdaq":
            rows = (json.loads(body).get("data") or {}).get("tradesTable", {}).get("rows")
            if not rows:
                return ["no tradesTable.rows"]
            df = pd.DataFrame(rows)
            if not {"date", "close"} <= set(df.columns):
                return ["rows lack date/close"]
            closes = df["close"].str.replace(r"[$,]", "", regex=True)
            return _date_problems(df["date"], "%m/%d/%Y") + _numeric_problems(closes, {"N/A"})
    except Exception as e:  # unreadable at all
        return [f"unreadable: {e}"]
    return [f"no schema rules for source {source!r}"]


def run(ctx: CheckContext) -> list[CheckResult]:
    m = ctx.manifest
    if m.empty:
        return [
            result(
                NAME, "manifest", None, Severity.FAIL, "empty", ">0 drops", "no drops to validate"
            )
        ]
    run_rows = m[(m["run_id"] == m["run_id"].max()) & m["ok"]]
    out = []
    for _, d in run_rows.iterrows():
        subject = f"{d['source']}:{d['dataset']}"
        if d["path"] is None or pd.isna(d["path"]):
            out.append(
                result(
                    NAME,
                    subject,
                    d["as_of_date"],
                    Severity.OK,
                    d["status"],
                    "",
                    f"{subject}: unchanged since last validated drop",
                )
            )
            continue
        p = ctx.cfg.state_dir / d["path"]
        problems = validate(d["source"], p.read_bytes()) if p.exists() else ["raw file missing"]
        sev = Severity.FAIL if problems else Severity.OK
        out.append(
            result(
                NAME,
                subject,
                d["as_of_date"],
                sev,
                "; ".join(problems) or "valid",
                "required columns + types",
                f"{subject}: " + ("; ".join(problems) if problems else "schema OK"),
            )
        )
    return out
