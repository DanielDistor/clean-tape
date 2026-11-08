"""`cleantape` command-line interface."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer

from cleantape.clock import get_clock
from cleantape.config import Config, load_config
from cleantape.logging_setup import get_logger, set_run_id, setup_logging

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Clean Tape market data pipeline"
)
log = get_logger("cli")

StateOpt = Annotated[Path | None, typer.Option(help="Override the state directory")]


def _boot(state_dir: Path | None = None) -> Config:
    cfg = load_config(state_dir=state_dir)
    setup_logging(cfg.logs_dir)
    return cfg


def new_run_id() -> str:
    """Run id: UTC timestamp, sortable and unique enough for twice-daily runs."""
    return get_clock().now_utc().strftime("%Y%m%dT%H%M%SZ")


@app.command()
def config(state_dir: StateOpt = None) -> None:
    """Validate configuration and print a summary."""
    cfg = _boot(state_dir)
    u = cfg.universe
    typer.echo(
        f"config OK: {len(u.instruments)} instruments, {len(u.spreads)} spreads, "
        f"{len(u.crosses)} crosses; state_dir={cfg.state_dir}"
    )
    for src in sorted({i.source for i in u.instruments}):
        typer.echo(f"  {src:7s} {len(u.by_source(src))} instruments")


@app.command()
def version() -> None:
    """Print the version."""
    from cleantape import __version__

    typer.echo(__version__)


def _state(cfg: Config):
    from cleantape.store.state import State

    return State(cfg.state_dir)


def _print_ingest(summary) -> None:  # type: ignore[no-untyped-def]
    typer.echo(
        f"drops={summary.drops} inserted={summary.inserted} revised={summary.revised} "
        f"unchanged_drops={summary.unchanged_drops} failed_sources={summary.failed_sources or '-'} "
        f"fallback_used={summary.fallback_used or '-'}"
    )
    for e in summary.parse_errors:
        typer.echo(f"  parse error: {e}", err=True)


@app.command()
def backfill(
    start: Annotated[str, typer.Option(help="First date, YYYY-MM-DD")] = "",
    state_dir: StateOpt = None,
) -> None:
    """Download history from --start (default from settings) into the state directory."""
    from cleantape.ingest.run import run_ingest

    cfg = _boot(state_dir)
    run_id = new_run_id()
    set_run_id(run_id)
    first = _date(start) or cfg.settings.ingest.backfill_start
    summary = run_ingest(cfg, _state(cfg), get_clock(), run_id, first)
    _print_ingest(summary)


@app.command()
def ingest(state_dir: StateOpt = None) -> None:
    """Fetch latest data with a lookback window (catches late data and revisions)."""
    from cleantape.clock import ny_today
    from cleantape.ingest.run import lookback_start, run_ingest

    cfg = _boot(state_dir)
    run_id = new_run_id()
    set_run_id(run_id)
    clock = get_clock()
    start = lookback_start(ny_today(clock.now_utc()), cfg.settings.ingest.lookback_business_days)
    summary = run_ingest(cfg, _state(cfg), clock, run_id, start)
    _print_ingest(summary)


def run_transform(cfg: Config) -> int:
    """Rebuild derived.parquet from observations. Returns row count."""
    from cleantape.transform.derive import build_derived

    st = _state(cfg)
    d = cfg.settings.derive
    derived = build_derived(
        st.read("observations"), cfg.universe, d.zscore_window, d.zscore_min_obs, d.ffill_max_days
    )
    st.write("derived", derived)
    log.info("derived rows=%d series=%d", len(derived), derived["series_id"].nunique())
    return len(derived)


@app.command()
def transform(state_dir: StateOpt = None) -> None:
    """Rebuild derived series (spreads, crosses, changes, z-scores) from observations."""
    cfg = _boot(state_dir)
    typer.echo(f"derived rows: {run_transform(cfg)}")


@app.command()
def query(
    sql: Annotated[
        str,
        typer.Argument(
            help="SQL over views: observations, derived, latest, "
            "catalog, manifest, check_results, runs"
        ),
    ] = "",
    examples: Annotated[bool, typer.Option(help="List example queries")] = False,
    state_dir: StateOpt = None,
) -> None:
    """Run SQL against the parquet state with DuckDB and print the result."""
    import pandas as pd

    from cleantape.store.queries import EXAMPLE_QUERIES
    from cleantape.store.queries import query as run_query

    cfg = _boot(state_dir)
    if examples or not sql:
        for title, q in EXAMPLE_QUERIES.items():
            typer.echo(f"-- {title}\n{q};\n")
        return
    with pd.option_context(
        "display.max_rows", 200, "display.width", 200, "display.max_columns", 30
    ):
        typer.echo(run_query(_state(cfg), cfg.universe, sql).to_string(index=False))


def print_results(results, only_problems: bool = False, limit: int = 60) -> None:  # type: ignore[no-untyped-def]
    """Readable summary table of check results."""
    from cleantape.checks.runner import overall

    counts = results["severity"].value_counts().to_dict()
    typer.echo(
        f"overall: {overall(results).name}   "
        + "  ".join(f"{k}={counts.get(k, 0)}" for k in ("OK", "WARN", "FAIL"))
    )
    by_check = results.groupby("check")["severity"].value_counts().unstack(fill_value=0)
    typer.echo(by_check.reindex(columns=["OK", "WARN", "FAIL"], fill_value=0).to_string())
    rank = {"FAIL": 0, "WARN": 1, "OK": 2}
    shown = results[results["severity"] != "OK"] if only_problems else results
    shown = shown.sort_values(
        ["severity", "check", "subject"], key=lambda c: c.map(rank) if c.name == "severity" else c
    )
    if shown.empty:
        typer.echo("no WARN/FAIL results")
        return
    typer.echo("")
    for r in shown.head(limit).itertuples():
        typer.echo(f"  {r.severity:4s} {r.check:12s} {r.message}")
    if len(shown) > limit:
        typer.echo(f"  ... {len(shown) - limit} more")


@app.command()
def check(
    audit: Annotated[
        bool, typer.Option(help="Validity/outlier/consistency audit over history")
    ] = False,
    start: Annotated[str, typer.Option(help="Audit start date")] = "2020-01-01",
    all_rows: Annotated[bool, typer.Option("--all", help="Show OK rows too")] = False,
    state_dir: StateOpt = None,
) -> None:
    """Run data-quality checks; exit 1 if any FAIL (audit mode never fails the exit code)."""
    from cleantape.checks.runner import build_context, overall, persist, run_checks

    cfg = _boot(state_dir)
    run_id = new_run_id()
    set_run_id(run_id)
    st = _state(cfg)
    ctx = build_context(
        cfg,
        st,
        get_clock().now_utc(),
        run_id,
        audit=audit,
        audit_start=_date(start) if audit else None,
    )
    results = run_checks(ctx)
    print_results(results, only_problems=not all_rows, limit=200 if audit else 60)
    if audit:
        return
    persist(st, results)
    if overall(results).name == "FAIL":
        raise typer.Exit(1)


@app.command()
def calibrate(
    write: Annotated[bool, typer.Option(help="Write config/calibration.yaml")] = True,
    state_dir: StateOpt = None,
) -> None:
    """Calibrate cross-source/triangle tolerances from history."""
    import yaml

    from cleantape.checks.consistency import calibrate as calib_fn

    cfg = _boot(state_dir)
    c = cfg.settings.checks.calibration
    tol = calib_fn(_state(cfg).read("derived"), cfg.universe.instruments, c)
    typer.echo(
        f"method: tolerance = quantile(|a/b-1|) x {c.safety_factor}; "
        f"warn q={c.warn_quantile}, fail q={c.fail_quantile}"
    )
    typer.echo(
        f"{'instrument':12s} {'n':>5s} {'median':>8s} {'p99':>8s} {'p99.9':>8s} "
        f"{'max':>8s} {'WARN>':>8s} {'FAIL>':>8s}"
    )
    for k, v in tol.items():
        typer.echo(
            f"{k:12s} {v['_n']:5d} {v['_median']:8.4%} {v['_p99']:8.4%} {v['_p999']:8.4%} "
            f"{v['_max']:8.4%} {v['warn']:8.4%} {v['fail']:8.4%}"
        )
    if write:
        body = {
            "consistency": {k: {"warn": v["warn"], "fail": v["fail"]} for k, v in tol.items()},
            "_calibration_detail": tol,
        }
        path = cfg.root / "config" / "calibration.yaml"
        header = (
            "# GENERATED by `cleantape calibrate` — do not edit by hand.\n"
            "# Units: relative difference |a/b - 1| (0.001 = 0.1%).\n"
        )
        path.write_text(header + yaml.safe_dump(body, sort_keys=True))
        typer.echo(f"wrote {path.relative_to(cfg.root)}")


def _not_yet(name: str) -> None:
    typer.echo(f"`{name}` is not implemented yet", err=True)
    raise typer.Exit(2)


@app.command()
def status(state_dir: StateOpt = None) -> None:
    """Print the last runs and current health."""
    _boot(state_dir)
    _not_yet("status")


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


if __name__ == "__main__":  # pragma: no cover
    set_run_id(new_run_id())
    app()
