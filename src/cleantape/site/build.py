"""Static site generator: state → site/ (Jinja2 templates, one CSS, one small JS, Plotly CDN).

All links are relative so the site works under https://<user>.github.io/clean-tape/ and from
the local filesystem. Bad numbers are never shown as if they were fine: instruments with a FAIL
check are struck through with the reason in a tooltip; WARN instruments get a ⚠ marker.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cleantape.calendars import completion_rule, expected_latest_date, get_calendar
from cleantape.clock import fmt_ny
from cleantape.config import Config
from cleantape.logging_setup import get_logger
from cleantape.store.state import State
from cleantape.transform.derive import SeriesMeta, series_catalog

log = get_logger("site")
HERE = Path(__file__).parent
PAGES = ["overview", "rates", "markets", "portfolio", "health", "reports", "about"]
GROUPS = [
    ("rates", "Rates & spreads"),
    ("credit_vol", "Credit & vol"),
    ("equities", "Equities"),
    ("fx", "FX"),
    ("commodities", "Commodities"),
]
PLOTLY_CDN = "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"


# ------------------------------------------------------------------ formatting


def fmt_level(v: float | None, units: str, series_id: str = "") -> str:
    """Display a level with sensible decimals for its units."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if units == "pct_yield":
        return f"{v:.2f}%"
    if units == "bp_spread":
        return f"{v:.0f}bp"
    if units == "fx_rate":
        return f"{v:.3f}" if "JPY" in series_id else f"{v:.4f}"
    if abs(v) >= 1000:
        return f"{v:,.1f}"
    if abs(v) >= 10:
        return f"{v:.2f}"
    return f"{v:.3f}"


def fmt_change(v: float | None, mode: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.1f}bp" if mode == "bp" else f"{v:+.2f}%"


def sign_class(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)) or abs(v) < 1e-12:
        return "flat"
    return "up" if v > 0 else "down"


def fmt_usd(v: float | None) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v:,.0f}"


def sparkline_svg(values: list[float], width: int = 84, height: int = 20, area: bool = True) -> str:
    """Inline SVG sparkline (line + faint area + last-point dot). Empty string if < 2 points."""
    vals = [v for v in values if v is not None and not math.isnan(v)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    step = (width - 4) / (len(vals) - 1)
    xy = [(2 + i * step, height - 2 - (v - lo) / span * (height - 4)) for i, v in enumerate(vals)]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    last_x, last_y = xy[-1]
    fill = ""
    if area:
        poly = f"{xy[0][0]:.1f},{height} {pts} {last_x:.1f},{height}"
        fill = f'<polygon points="{poly}" fill="currentColor" fill-opacity="0.13" stroke="none"/>'
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true" preserveAspectRatio="none">{fill}<polyline points="{pts}" '
        f'fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" '
        f'vector-effect="non-scaling-stroke"/><circle cx="{last_x:.1f}" cy="{last_y:.1f}" '
        f'r="2" fill="currentColor"/></svg>'
    )


def z_bar(z: float | None, cap: float = 4.0) -> dict[str, float] | None:
    """Geometry (% of cell) for a diverging bar centred on zero, capped at |z| = cap."""
    if z is None or (isinstance(z, float) and math.isnan(z)):
        return None
    zc = max(-cap, min(cap, float(z)))
    width = abs(zc) / cap * 50
    return {"left": 50 if zc >= 0 else 50 - width, "width": round(width, 1)}


def _r(v: float, units: str) -> float | None:
    """Round for compact JSON."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    digits = {"pct_yield": 3, "bp_spread": 2, "fx_rate": 5}.get(units, 4)
    return round(float(v), digits)


# ------------------------------------------------------------------ model


@dataclass
class Flag:
    severity: str  # FAIL | WARN
    messages: list[str]

    @property
    def tooltip(self) -> str:
        return " | ".join(self.messages)


def latest_check_run(checks: pd.DataFrame) -> pd.DataFrame:
    if checks.empty:
        return checks
    return checks[checks["run_id"] == checks["run_id"].max()]


def flags_by_subject(results: pd.DataFrame) -> dict[str, Flag]:
    """Worst non-OK severity per subject with its messages."""
    out: dict[str, Flag] = {}
    for subj, grp in results[results["severity"] != "OK"].groupby("subject"):
        sev = "FAIL" if (grp["severity"] == "FAIL").any() else "WARN"
        msgs = grp[grp["severity"] == sev]["message"].tolist()
        out[str(subj)] = Flag(sev, msgs)
    return out


def subject_flag(
    flags: dict[str, Flag], series_id: str, meta: SeriesMeta, cfg: Config
) -> Flag | None:
    """Flag for a displayed series, including derived series whose legs are flagged."""
    own = flags.get(series_id)
    legs: list[str] = []
    for s in cfg.universe.spreads:
        if s.id == series_id:
            legs = [s.long, s.short]
    for c in cfg.universe.crosses:
        if c.id == series_id:
            legs = [c.left, c.right]
    leg_flags = [flags[x] for x in legs if x in flags]
    all_flags = [f for f in [own, *leg_flags] if f]
    if not all_flags:
        return None
    sev = "FAIL" if any(f.severity == "FAIL" for f in all_flags) else "WARN"
    return Flag(sev, [m for f in all_flags for m in f.messages])


def headline_date(latest: pd.DataFrame, cat: dict[str, SeriesMeta]) -> dt.date | None:
    """The page headline as-of date: the most recent NYSE-calendar date among displayed series."""
    nyse = latest[
        latest["series_id"].map(
            lambda s: cat[s].calendar == "nyse" and cat[s].display_group is not None
        )
    ]
    if nyse.empty:
        return latest["date"].max() if not latest.empty else None
    return nyse["date"].max()


def overview_rows(
    cfg: Config, derived: pd.DataFrame, flags: dict[str, Flag], headline: dt.date | None
) -> dict[str, list[dict[str, Any]]]:
    cat = series_catalog(cfg.universe)
    groups: dict[str, list[dict[str, Any]]] = {g: [] for g, _ in GROUPS}
    for sid, grp in derived.groupby("series_id"):
        meta = cat.get(str(sid))
        if meta is None or meta.display_group not in groups:
            continue
        grp = grp.sort_values("date")
        last = grp.iloc[-1]
        flag = subject_flag(flags, str(sid), meta, cfg)
        z = last["z_1d"]
        groups[meta.display_group].append(
            {
                "id": sid,
                "name": meta.name,
                "order": meta.display_order,
                "last": fmt_level(last["value"], meta.units, str(sid)),
                "chg": {
                    k: fmt_change(last[f"chg_{k}"], meta.change_mode)
                    for k in ("1d", "1w", "1m", "ytd")
                },
                "cls": {k: sign_class(last[f"chg_{k}"]) for k in ("1d", "1w", "1m", "ytd")},
                "z": "—" if pd.isna(z) else f"{z:+.1f}",
                "z_hot": (not pd.isna(z)) and abs(z) > 2,
                "zbar": z_bar(None if pd.isna(z) else float(z)),
                "as_of": last["date"] if headline is None or last["date"] != headline else None,
                "spark": sparkline_svg(grp["value"].tail(60).tolist()),
                "source": last["source"],
                "fallback": last["source"] == "nasdaq",
                "flag": flag,
                "future": meta.is_future,
            }
        )
    for rows in groups.values():
        rows.sort(key=lambda r: r["order"])
    return groups


def hero_tiles(cfg: Config, derived: pd.DataFrame, flags: dict[str, Flag]) -> list[dict[str, Any]]:
    """Big headline tiles for the Overview (series listed in settings.site.hero)."""
    cat = series_catalog(cfg.universe)
    out = []
    for sid in cfg.settings.site.hero:
        meta = cat.get(sid)
        grp = derived[derived["series_id"] == sid].sort_values("date")
        if meta is None or grp.empty:
            continue
        last = grp.iloc[-1]
        flag = subject_flag(flags, sid, meta, cfg)
        out.append(
            {
                "id": sid,
                "name": meta.name,
                "date": last["date"],
                "flag": flag,
                "bad": flag is not None and flag.severity == "FAIL",
                "last": fmt_level(last["value"], meta.units, sid),
                "chg": fmt_change(last["chg_1d"], meta.change_mode),
                "cls": sign_class(last["chg_1d"]),
                "ytd": fmt_change(last["chg_ytd"], meta.change_mode),
                "spark": sparkline_svg(grp["value"].tail(120).tolist(), width=220, height=44),
            }
        )
    return out


def tape_items(cfg: Config, derived: pd.DataFrame, flags: dict[str, Flag]) -> list[dict[str, Any]]:
    """Scrolling ticker-tape entries: every displayed series in display order.

    FAIL-flagged series appear with a ✕ and no numbers, so the tape never shows a bad print.
    """
    cat = series_catalog(cfg.universe)
    order = [g for g, _ in GROUPS]
    latest = derived.sort_values("date").groupby("series_id").tail(1)
    items = []
    for r in latest.itertuples():
        meta = cat.get(r.series_id)
        if meta is None or meta.display_group not in order:
            continue
        flag = subject_flag(flags, r.series_id, meta, cfg)
        bad = flag is not None and flag.severity == "FAIL"
        items.append(
            {
                "id": r.series_id,
                "name": meta.name,
                "bad": bad,
                "last": "" if bad else fmt_level(r.value, meta.units, r.series_id),
                "chg": "" if bad else fmt_change(r.chg_1d, meta.change_mode),
                "cls": "flat" if bad else sign_class(r.chg_1d),
                "key": (order.index(meta.display_group), meta.display_order),
            }
        )
    return sorted(items, key=lambda x: x["key"])


def top_movers(
    cfg: Config, derived: pd.DataFrame, flags: dict[str, Flag], n: int = 5
) -> tuple[list[dict[str, Any]], list[str]]:
    """Top |z| moves among displayed series. Series with a FAIL flag (own or on a leg) are
    excluded — a broken print would otherwise top the list as the "biggest move"."""
    cat = series_catalog(cfg.universe)
    latest = derived.sort_values("date").groupby("series_id").tail(1)
    latest = latest[
        latest["series_id"].map(lambda s: s in cat and cat[s].display_group is not None)
    ]
    excluded = []
    for sid in latest["series_id"]:
        f = subject_flag(flags, sid, cat[sid], cfg)
        if f and f.severity == "FAIL":
            excluded.append(sid)
    latest = latest[~latest["series_id"].isin(excluded)]
    latest = latest.dropna(subset=["z_1d"])
    latest = latest.reindex(latest["z_1d"].abs().sort_values(ascending=False).index).head(n)
    return [
        {
            "id": r.series_id,
            "name": cat[r.series_id].name,
            "chg": fmt_change(r.chg_1d, cat[r.series_id].change_mode),
            "cls": sign_class(r.chg_1d),
            "z": f"{r.z_1d:+.1f}σ",
            "date": r.date,
        }
        for r in latest.itertuples()
    ], sorted(cat[x].name for x in excluded)


def series_json(derived: pd.DataFrame, cat: dict[str, SeriesMeta]) -> dict[str, dict[str, Any]]:
    """Compact per-series JSON for the Markets page."""
    out = {}
    for sid, grp in derived.groupby("series_id"):
        meta = cat.get(str(sid))
        if meta is None:
            continue
        grp = grp.sort_values("date")
        out[str(sid)] = {
            "id": sid,
            "name": meta.name,
            "units": meta.units,
            "mode": meta.change_mode,
            "d": [x.isoformat() for x in grp["date"]],
            "v": [_r(v, meta.units) for v in grp["value"]],
            "m": [None if pd.isna(x) else round(float(x), 3) for x in grp["chg_1d"]],
        }
    return out


def curve_points(derived: pd.DataFrame) -> dict[str, Any]:
    """Treasury curve today / 1w / 1m ago (same-date points only)."""
    tenors = [
        ("DGS3MO", "3M", 0.25),
        ("DGS2", "2Y", 2),
        ("DGS5", "5Y", 5),
        ("DGS10", "10Y", 10),
        ("DGS30", "30Y", 30),
    ]
    w = (
        derived[derived["series_id"].isin([t[0] for t in tenors])]
        .pivot_table(index="date", columns="series_id", values="value")
        .dropna()
    )
    if w.empty:
        return {}
    today = w.index.max()
    cal = get_calendar("us_bond")

    def row(d: dt.date) -> dict[str, Any] | None:
        cands = w.index[w.index <= d]
        if len(cands) == 0:
            return None
        dd = cands.max()
        return {"date": dd.isoformat(), "y": [round(float(w.at[dd, t[0]]), 3) for t in tenors]}

    return {
        "x": [t[1] for t in tenors],
        "curves": [
            {"label": "Today", **(row(today) or {})},
            {"label": "1w ago", **(row(cal.on_or_before(today - dt.timedelta(days=7))) or {})},
            {"label": "1m ago", **(row(cal.on_or_before(today - dt.timedelta(days=30))) or {})},
        ],
    }


def check_history(checks: pd.DataFrame, days: int = 30) -> dict[str, Any]:
    """WARN/FAIL counts for the last run of each NY day, for the last ``days`` days."""
    if checks.empty:
        return {"d": [], "warn": [], "fail": []}
    c = checks.copy()
    # run_id is a UTC timestamp; evening runs after 19:00 ET fall on the next UTC day, so group
    # by the run's New York date instead.
    started = pd.to_datetime(c["run_id"], format="%Y%m%dT%H%M%SZ", utc=True, errors="coerce")
    c["day"] = started.dt.tz_convert("America/New_York").dt.strftime("%Y%m%d")
    c = c.dropna(subset=["day"])
    last_runs = c.groupby("day")["run_id"].max()
    c = c[c["run_id"].isin(last_runs.values)]
    counts = c.groupby(["day", "severity"]).size().unstack(fill_value=0)
    counts = counts.tail(days)
    return {
        "d": [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in counts.index],
        "warn": [int(x) for x in counts.get("WARN", pd.Series(0, index=counts.index))],
        "fail": [int(x) for x in counts.get("FAIL", pd.Series(0, index=counts.index))],
    }


# ------------------------------------------------------------------ build


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(usd=fmt_usd, chg=fmt_change, sign=sign_class, level=fmt_level)
    return env


def _json(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str, allow_nan=False)


def _clean(obj: Any) -> Any:
    """Replace NaN with None recursively so JSON stays valid."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.floating):
        return _clean(float(obj))
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def asset_version(static_dir: Path) -> str:
    """Short content hash of the static assets, appended as ?v= so browsers never pair new HTML
    with a cached old stylesheet (GitHub Pages serves max-age=600)."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted(static_dir.rglob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def build_site(
    cfg: Config,
    state: State,
    now_utc: dt.datetime,
    portfolio: dict[str, Any] | None,
    out_dir: Path | None = None,
    run_info: dict[str, Any] | None = None,
    demo: bool = True,
) -> dict[str, Any]:
    """Render every page into ``out_dir`` (default cfg.site_dir). Returns a summary.

    With ``demo=True`` (and data available) a failure demo is also built into ``out/demo/``
    and linked from the About and Health pages; links only appear when the demo exists.
    """
    out = out_dir or cfg.site_dir
    is_demo = bool((run_info or {}).get("demo"))
    with_demo = False
    if demo and not is_demo:
        obs_ids = state.read("observations")["instrument_id"]
        from cleantape.site.demo import BROKEN_SERIES

        with_demo = bool((obs_ids == BROKEN_SERIES).any())
    if out.exists():
        shutil.rmtree(out)
    (out / "data" / "series").mkdir(parents=True)
    shutil.copytree(HERE / "static", out / "static")

    derived = state.read("derived")
    checks = state.read("check_results")
    manifest = state.read("manifest")
    cat = series_catalog(cfg.universe)
    results = latest_check_run(checks)
    flags = flags_by_subject(results) if not results.empty else {}
    latest = (
        derived.sort_values("date").groupby("series_id").tail(1) if not derived.empty else derived
    )
    headline = headline_date(latest, cat) if not derived.empty else None

    if results.empty:
        status = "FAIL"
    else:
        sev = set(results["severity"])
        status = "FAIL" if "FAIL" in sev else "WARN" if "WARN" in sev else "OK"
    fail_subjects = sorted(s for s, f in flags.items() if f.severity == "FAIL")
    warn_subjects = sorted(s for s, f in flags.items() if f.severity == "WARN")

    nyse = get_calendar("nyse")
    expected = expected_latest_date(
        nyse, now_utc, 0, completion_rule("yahoo", "nyse", False), cfg.settings.nyse_close_buffer
    )
    behind = nyse.sessions_after(headline, expected) if headline else 99
    stale = behind > cfg.settings.site.stale_after_business_days
    stage_failures = (run_info or {}).get("stage_failures", [])

    common = {
        "title": cfg.settings.site.title,
        "tagline": cfg.settings.site.tagline,
        "headline": headline,
        "updated": fmt_ny(now_utc),
        "updated_full": fmt_ny(now_utc, True),
        "status": status,
        "fail_subjects": fail_subjects,
        "warn_subjects": warn_subjects,
        "stale": stale,
        "behind": behind,
        "expected": expected,
        "stage_failures": stage_failures,
        "demo": (run_info or {}).get("demo"),
        "has_demo": with_demo,
        "repo_url": cfg.settings.site.repo_url,
        "plotly": PLOTLY_CDN,
        "asset_v": asset_version(HERE / "static"),
        "pages": PAGES,
        "tape": tape_items(cfg, derived, flags) if not derived.empty else [],
        "commands": sorted(
            (m.series_id, m.name) for m in cat.values() if m.display_group is not None
        ),
        "check_counts": results["severity"].value_counts().to_dict() if not results.empty else {},
        "names": {k: v.name for k, v in cat.items()},
    }
    env = _env()
    ctx: dict[str, dict[str, Any]] = {p: {} for p in PAGES}

    ctx["overview"] = {
        "hero": hero_tiles(cfg, derived, flags) if not derived.empty else [],
        "groups": [
            (g, label, rows)
            for (g, label), rows in zip(
                GROUPS, overview_rows(cfg, derived, flags, headline).values(), strict=True
            )
        ]
        if not derived.empty
        else [],
        "movers": [],
        "movers_excluded": [],
    }
    if not derived.empty:
        ctx["overview"]["movers"], ctx["overview"]["movers_excluded"] = top_movers(
            cfg, derived, flags
        )

    rates_ids = ["S2S10S", "S5S30S", "S3M10Y", "T10YIE", "HY_OAS"]
    sj = series_json(derived, cat) if not derived.empty else {}
    ctx["rates"] = {
        "curve": _clean(curve_points(derived)) if not derived.empty else {},
        "history": {k: sj[k] for k in rates_ids if k in sj},
    }
    for sid, payload in sj.items():
        (out / "data" / "series" / f"{sid}.json").write_text(_json(_clean(payload)))
    displayed = sorted(
        (m for m in cat.values() if m.display_group and m.series_id in sj),
        key=lambda m: ([g for g, _ in GROUPS].index(m.display_group), m.display_order),
    )
    ctx["markets"] = {"options": [(m.series_id, m.name) for m in displayed]}

    if portfolio:
        pos = portfolio["positions"]
        ctx["portfolio"] = {
            "positions": pos.to_dict("records"),
            "by_book": portfolio["by_book"].to_dict("records"),
            "by_asset_class": portfolio["by_asset_class"].to_dict("records"),
            "totals": portfolio["totals"],
            "shocks": portfolio["shocks"].to_dict("records"),
            "history": _clean(
                {
                    "d": [d.isoformat() for d in portfolio["history"]["date"]],
                    "cum": portfolio["history"]["cum_pnl_usd"].round(0).tolist(),
                }
            ),
        }

    res_rows = (
        results.sort_values(
            ["severity", "check", "subject"],
            key=lambda c: c.map({"FAIL": 0, "WARN": 1, "OK": 2}) if c.name == "severity" else c,
        )
        if not results.empty
        else results
    )
    man = (
        manifest.sort_values("received_at_utc", ascending=False).head(40).copy()
        if not manifest.empty
        else manifest
    )
    if not man.empty:
        man["error"] = man["error"].fillna("")
        man["received_et"] = man["received_at_utc"].map(lambda t: fmt_ny(t.to_pydatetime(), True))
    ctx["health"] = {
        "results": res_rows.to_dict("records") if not res_rows.empty else [],
        "counts": results["severity"].value_counts().to_dict() if not results.empty else {},
        "manifest": man.to_dict("records") if not man.empty else [],
        "history": check_history(checks),
    }

    ctx["reports"] = {"reports": copy_reports(state, out, cfg.settings.site.report_archive_days)}
    ctx["about"] = {
        "universe": cfg.universe.instruments,
        "coverage": coverage(cfg),
        "engine": engine_rows(cfg),
    }

    for page in PAGES:
        html = env.get_template(f"{page}.html").render(page=page, **common, **ctx[page], json=_json)
        name = "index.html" if page == "overview" else f"{page}.html"
        (out / name).write_text(html)
    (out / ".nojekyll").write_text("")
    if with_demo:
        from cleantape.site.demo import build_failure_demo

        try:
            build_failure_demo(cfg, state, now_utc, out / "demo")
        except Exception as e:  # the demo is a nice-to-have; never block the real site
            log.exception("failure demo build failed: %s", e)
    size = _dir_size(out)
    log.info("site built: %s (%.1f KB, %d series files)", out, size / 1024, len(sj))
    return {"dir": out, "bytes": size, "status": status, "headline": headline, "stale": stale}


ASSET_LABELS = {
    "rates": "US rates",
    "credit": "Credit",
    "vol": "Volatility",
    "equities": "Equities",
    "fx": "FX",
    "commodities": "Commodities",
}
SOURCE_LABELS = {"fred": "FRED", "yahoo": "Yahoo Finance", "nasdaq": "Nasdaq", "ecb": "ECB"}


def coverage(cfg: Config) -> list[dict[str, Any]]:
    """What's on the tape, grouped by asset class (displayed series + cross-check sources)."""
    out = []
    for ac, label in ASSET_LABELS.items():
        shown = [i for i in cfg.universe.instruments if i.asset_class == ac and i.display_group]
        checks = [
            i for i in cfg.universe.instruments if i.asset_class == ac and not i.display_group
        ]
        names = [i.name for i in shown]
        if ac == "rates":
            names += [f"{s.name} spread" for s in cfg.universe.spreads]
        if not names:
            continue
        sources = sorted(
            {SOURCE_LABELS[i.source] for i in shown}
            | {SOURCE_LABELS[i.fallback.source] for i in shown if i.fallback}
        )
        names_by_id = {
            **{i.id: i.name for i in cfg.universe.instruments},
            **{c.id: "EUR/USD × USD/JPY" for c in cfg.universe.crosses},
        }
        second = [
            f"{i.name} vs {names_by_id[i.cross_check.against]}" for i in shown if i.cross_check
        ]
        if ac == "rates" and cfg.universe.spreads:
            second.append("each spread = long leg − short leg")
        del checks  # second sources (ECB, EIA spot) are named inside the pairs above
        out.append({"label": label, "names": names, "sources": sources, "second": second})
    return out


def engine_rows(cfg: Config) -> list[dict[str, str]]:
    """The 7-check engine, with thresholds read from the live config."""
    c = cfg.settings.checks
    fx = [v for k, v in c.consistency.items() if k not in ("EURJPY", "CL_F")]
    fx_rng = (
        f"{min(t.warn for t in fx):.1%}–{max(t.warn for t in fx):.1%}" if fx else "uncalibrated"
    )
    tri = c.consistency.get("EURJPY")
    return [
        {
            "check": "Freshness",
            "question": "Is the latest value as recent as the market calendar "
            "and the source's publishing delay say it should be?",
            "rule": f"{c.freshness.warn_business_days} business day behind = WARN, "
            f"≥{c.freshness.fail_business_days} = FAIL",
        },
        {
            "check": "Completeness",
            "question": "Are trading days missing, did a download fail, "
            "did a file arrive with far fewer rows than usual?",
            "rule": f"gaps in last {c.completeness.lookback_business_days} sessions "
            f"(≥{c.completeness.fail_missing_sessions} = FAIL); rows ±"
            f"{c.completeness.row_count_tolerance:.0%} vs median = WARN",
        },
        {
            "check": "Schema",
            "question": "Does each raw vendor file still have the columns and "
            "types the parser expects?",
            "rule": "any missing column / non-numeric value = FAIL",
        },
        {
            "check": "Validity",
            "question": "Is the number physically plausible, with no nulls on "
            "trading days and no duplicate records?",
            "rule": "outside per-instrument bounds or duplicate = FAIL; null = WARN",
        },
        {
            "check": "Staleness",
            "question": "Has a value stopped changing (a dead feed)?",
            "rule": f"same value ≥{c.staleness.warn_sessions} sessions = WARN",
        },
        {
            "check": "Outliers",
            "question": "Is today's move abnormal for this series, measured "
            "against its own last year of daily moves?",
            "rule": f"|z| > {c.outliers.warn_z:g} = WARN, > {c.outliers.fail_z:g} = FAIL "
            "(futures capped at WARN with a roll hint)",
        },
        {
            "check": "Consistency",
            "question": "Do independent sources agree, and do derived numbers add up?",
            "rule": f"Yahoo vs ECB FX: WARN > {fx_rng}; EUR/JPY triangle: WARN > {tri.warn:.2%}"
            if tri
            else "uncalibrated",
        },
    ]


def copy_reports(state: State, out: Path, keep: int) -> list[dict[str, Any]]:
    """Copy the last ``keep`` reports from state/reports into site/reports; return listing."""
    src = state.root / "reports"
    if not src.exists():
        return []
    days = sorted((p for p in src.iterdir() if p.is_dir()), reverse=True)[:keep]
    listing = []
    for d in days:
        dst = out / "reports" / d.name
        shutil.copytree(d, dst)
        files = {f.suffix: f"reports/{d.name}/{f.name}" for f in dst.iterdir()}
        listing.append({"date": d.name, "html": files.get(".html"), "xlsx": files.get(".xlsx")})
    return listing
