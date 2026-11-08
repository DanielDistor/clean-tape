"""End-of-day report: one data model rendered as email-safe HTML and a formatted Excel workbook.

Written to ``state/reports/{date}/eod_{date}.{html,xlsx}`` so the archive persists on the
``data`` branch; ``build-site`` copies the last 30 into the site's Reports page.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cleantape.clock import fmt_ny
from cleantape.config import Config
from cleantape.logging_setup import get_logger
from cleantape.site.build import (
    Flag,
    flags_by_subject,
    fmt_change,
    fmt_level,
    fmt_usd,
    latest_check_run,
    subject_flag,
)
from cleantape.store.state import State
from cleantape.transform.derive import series_catalog

log = get_logger("report")
HERE = Path(__file__).parent

# Excel sheet / HTML section per display group (credit & vol split by asset class)
SHEET_OF_GROUP = {
    "rates": "Rates",
    "equities": "Equities",
    "fx": "FX",
    "commodities": "Commodities",
}
SHEETS = ["Rates", "Equities", "FX", "Commodities"]


def sheet_for(series_id: str, display_group: str, asset_class: str) -> str:
    if display_group == "credit_vol":
        return "Rates" if asset_class == "credit" else "Equities"
    return SHEET_OF_GROUP[display_group]


@dataclass
class ReportRow:
    series_id: str
    name: str
    units: str
    mode: str
    date: dt.date
    value: float
    chg_1d: float
    chg_1w: float
    chg_1m: float
    chg_ytd: float
    z: float
    source: str
    flag: Flag | None

    @property
    def status(self) -> str:
        return "OK" if self.flag is None else self.flag.severity

    @property
    def unreliable(self) -> bool:
        return self.flag is not None and self.flag.severity == "FAIL"


@dataclass
class ReportModel:
    date: dt.date
    generated_utc: dt.datetime
    status: str
    sections: dict[str, list[ReportRow]]
    movers: list[ReportRow]
    curve: list[dict[str, Any]]
    checks: pd.DataFrame  # WARN/FAIL rows only
    unreliable: list[str]
    portfolio: dict[str, Any] | None
    site_url: str
    stage_failures: list[str] = field(default_factory=list)


def build_model(
    cfg: Config,
    state: State,
    date: dt.date,
    now_utc: dt.datetime,
    portfolio: dict[str, Any] | None,
    stage_failures: list[str] | None = None,
) -> ReportModel:
    """Collect everything the report shows, as of ``date`` (no data after it is used)."""
    derived = state.read("derived")
    derived = derived[derived["date"] <= date]
    results = latest_check_run(state.read("check_results"))
    flags = flags_by_subject(results) if not results.empty else {}
    cat = series_catalog(cfg.universe)
    latest = derived.sort_values("date").groupby("series_id").tail(1)
    sections: dict[str, list[ReportRow]] = {s: [] for s in SHEETS}
    rows: list[ReportRow] = []
    for r in latest.itertuples():
        meta = cat.get(r.series_id)
        if meta is None or meta.display_group is None:
            continue
        row = ReportRow(
            r.series_id,
            meta.name,
            meta.units,
            meta.change_mode,
            r.date,
            r.value,
            r.chg_1d,
            r.chg_1w,
            r.chg_1m,
            r.chg_ytd,
            r.z_1d,
            r.source,
            subject_flag(flags, r.series_id, meta, cfg),
        )
        sections[sheet_for(r.series_id, meta.display_group, meta.asset_class)].append(row)
        rows.append(row)
    order = {sid: (m.display_group or "", m.display_order) for sid, m in cat.items()}
    for v in sections.values():
        v.sort(key=lambda x: order[x.series_id])
    movers = sorted(
        (x for x in rows if not x.unreliable and not _nan(x.z)), key=lambda x: -abs(x.z)
    )[:8]
    curve = []
    for sid in ["DGS3MO", "DGS2", "DGS5", "DGS10", "DGS30", "S2S10S", "S5S30S", "S3M10Y"]:
        x = next((r for r in rows if r.series_id == sid), None)
        if x:
            curve.append(
                {
                    "name": x.name,
                    "date": x.date,
                    "level": fmt_level(x.value, x.units, sid),
                    "d1": x.chg_1d,
                    "w1": x.chg_1w,
                    "m1": x.chg_1m,
                    "flag": x.flag,
                }
            )
    problems = results[results["severity"] != "OK"] if not results.empty else results
    if results.empty:
        status = "FAIL"
    else:
        status = (
            "FAIL"
            if (results["severity"] == "FAIL").any()
            else ("WARN" if (results["severity"] == "WARN").any() else "OK")
        )
    unreliable = sorted({x.name for x in rows if x.unreliable})
    if portfolio:
        pos = portfolio["positions"]
        ok = pos[pos["status"] == "ok"]
        portfolio = {
            **portfolio,
            "top": ok.reindex(ok["pnl_usd"].abs().sort_values(ascending=False).index)
            .head(5)
            .to_dict("records"),
            "na": pos[pos["status"] != "ok"].to_dict("records"),
        }
    return ReportModel(
        date,
        now_utc,
        status,
        sections,
        movers,
        curve,
        problems.sort_values(["severity", "check"]) if not problems.empty else problems,
        unreliable,
        portfolio,
        cfg.settings.site.base_url,
        stage_failures or [],
    )


def default_report_date(state: State, universe_cat: dict) -> dt.date | None:  # type: ignore[type-arg]
    """Latest NYSE-calendar date in derived data (the evening's as-of date)."""
    d = state.read("derived")
    if d.empty:
        return None
    nyse = d[d["series_id"].map(lambda s: s in universe_cat and universe_cat[s].calendar == "nyse")]
    return (nyse if not nyse.empty else d)["date"].max()


def _nan(v: float | None) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def render_html(model: ReportModel) -> str:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "j2"])
    )
    env.filters.update(level=fmt_level, chg=fmt_change, usd=fmt_usd)
    env.globals["color"] = lambda v: (
        ("#006300" if v > 0 else "#c22f2f" if v < 0 else "#52514e") if not _nan(v) else "#52514e"
    )
    return env.get_template("eod_report.html.j2").render(
        m=model, generated=fmt_ny(model.generated_utc, True), isnan=_nan
    )


def write_report(
    cfg: Config,
    state: State,
    date: dt.date,
    now_utc: dt.datetime,
    portfolio: dict[str, Any] | None,
    stage_failures: list[str] | None = None,
) -> dict[str, Path]:
    """Render HTML + Excel into state/reports/{date}/. Returns the paths."""
    from cleantape.report.excel import write_workbook

    model = build_model(cfg, state, date, now_utc, portfolio, stage_failures)
    out = state.root / "reports" / date.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"eod_{date.isoformat()}.html"
    xlsx_path = out / f"eod_{date.isoformat()}.xlsx"
    html_path.write_text(render_html(model))
    write_workbook(model, xlsx_path)
    log.info("report written: %s (+ .xlsx) status=%s", html_path, model.status)
    return {"html": html_path, "xlsx": xlsx_path}
