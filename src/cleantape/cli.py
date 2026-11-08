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


def compute_portfolio(cfg: Config, flagged: set[str] | None = None) -> dict:
    """Value the MOCK portfolio from state. Returns positions, aggregates, shocks, history."""
    from cleantape.portfolio import pnl

    st = _state(cfg)
    positions = pnl.load_positions(cfg.path(cfg.settings.paths.portfolio), cfg.universe)
    derived = st.read("derived")
    if flagged is None:
        checks = st.read("check_results")
        last = checks[checks["run_id"] == checks["run_id"].max()] if not checks.empty else checks
        flagged = set(last[last["severity"] == "FAIL"]["subject"]) if not last.empty else set()
    wide = pnl.wide_values(derived)
    as_of = pnl.expected_dates(cfg, positions, get_clock().now_utc())
    pos = pnl.value_positions(positions, wide, cfg.universe, as_of, flagged)
    hist_dates = sorted(wide.index[wide.index <= max(as_of.values())])[-31:]
    return {
        "positions": pos,
        "by_book": pnl.aggregate(pos, "book"),
        "by_asset_class": pnl.aggregate(pos, "asset_class"),
        "totals": pnl.totals(pos),
        "shocks": pnl.shocks_table(pos, cfg.settings.shocks),
        "history": pnl.pnl_history(positions, wide, cfg.universe, hist_dates[1:], flagged),
    }


@app.command(name="pnl")
def pnl_cmd(state_dir: StateOpt = None) -> None:
    """Print MOCK portfolio P&L, exposures and shock scenarios."""
    import pandas as pd

    cfg = _boot(state_dir)
    r = compute_portfolio(cfg)
    fmt = {
        "pnl_usd": "{:,.0f}".format,
        "exposure_usd": "{:,.0f}".format,
        "gross_exposure_usd": "{:,.0f}".format,
        "net_exposure_usd": "{:,.0f}".format,
        "dv01_usd": "{:,.0f}".format,
        "move": "{:+.2f}".format,
    }
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        typer.echo("MOCK PORTFOLIO — P&L by position")
        cols = [
            "position_id",
            "instrument_id",
            "book",
            "as_of",
            "move",
            "move_unit",
            "pnl_usd",
            "exposure_usd",
            "status",
        ]
        typer.echo(r["positions"][cols].to_string(index=False, formatters=fmt))
        typer.echo("\nby book")
        typer.echo(r["by_book"].to_string(index=False, formatters=fmt))
        t = r["totals"]
        typer.echo(
            f"\nTOTAL P&L {t['pnl_usd']:,.0f} USD  (n/a positions: {t['n_a']})  "
            f"gross {t['gross_exposure_usd']:,.0f}  net {t['net_exposure_usd']:,.0f}  "
            f"DV01 {t['dv01_usd']:,.0f}"
        )
        typer.echo("\nshocks (linear approximations)")
        typer.echo(r["shocks"].to_string(index=False, formatters=fmt))


@app.command(name="build-site")
def build_site_cmd(state_dir: StateOpt = None) -> None:
    """Generate the static site into site/ from state."""
    from cleantape.site.build import build_site

    cfg = _boot(state_dir)
    try:
        portfolio = compute_portfolio(cfg)
    except Exception as e:  # the site must still build and show the problem
        log.exception("portfolio valuation failed: %s", e)
        portfolio = None
    now = get_clock().now_utc()
    info = build_site(cfg, _state(cfg), now, portfolio)
    typer.echo(
        f"site: {info['dir']}  {info['bytes'] / 1024:.0f} KB  status={info['status']} "
        f"headline={info['headline']} stale={info['stale']}"
    )


@app.command()
def report(
    date: Annotated[str, typer.Option(help="As-of date YYYY-MM-DD (default: latest)")] = "",
    state_dir: StateOpt = None,
) -> None:
    """Write the end-of-day report (HTML + Excel) into state/reports/{date}/."""
    from cleantape.report.build import default_report_date, write_report
    from cleantape.transform.derive import series_catalog

    cfg = _boot(state_dir)
    st = _state(cfg)
    day = _date(date) or default_report_date(st, series_catalog(cfg.universe))
    if day is None:
        typer.echo("no data in state; run `cleantape backfill` first", err=True)
        raise typer.Exit(1)
    paths = write_report(cfg, st, day, get_clock().now_utc(), compute_portfolio(cfg))
    typer.echo(f"report: {paths['html']}\n        {paths['xlsx']}")


@app.command()
def run(
    slot: Annotated[str, typer.Option(help="auto | morning | evening")] = "auto",
    force: Annotated[bool, typer.Option(help="Bypass the slot window / already-ran guard")] = False,
    outcome_file: Annotated[Path | None, typer.Option(help="Write a JSON outcome here")] = None,
    state_dir: StateOpt = None,
) -> None:
    """Full pipeline run for a slot. Exits 1 if checks FAIL or a stage crashed."""
    from cleantape.pipeline import run as run_pipeline
    from cleantape.pipeline import write_outcome

    cfg = _boot(state_dir)
    run_id = new_run_id()
    set_run_id(run_id)
    outcome = run_pipeline(cfg, get_clock(), run_id, slot=slot, force=force)
    if outcome_file:
        write_outcome(outcome_file, outcome)
    if not outcome.ran:
        typer.echo(f"nothing to do: {outcome.reason}")
        return
    typer.echo(
        f"run {run_id} slot={outcome.slot} checks={outcome.status} "
        f"failed_stages={outcome.stage_failures or '-'}"
    )
    raise typer.Exit(outcome.exit_code)


@app.command()
def status(state_dir: StateOpt = None) -> None:
    """Print the last runs and current data health."""
    import pandas as pd

    from cleantape.checks.runner import overall

    cfg = _boot(state_dir)
    st = _state(cfg)
    runs = st.read("runs")
    if runs.empty:
        typer.echo("no runs recorded yet")
    else:
        last = runs[runs["stage"] == "run"].tail(8)
        with pd.option_context("display.width", 200, "display.max_colwidth", 80):
            typer.echo(last[["run_id", "slot", "ny_date", "ok", "detail"]].to_string(index=False))
        lr = runs[runs["run_id"] == runs["run_id"].max()]
        typer.echo("\nstages of last run:")
        lr = lr.assign(detail=lr["detail"].str.slice(0, 90))
        typer.echo(lr[["stage", "ok", "duration_ms", "detail"]].to_string(index=False))
    checks = st.read("check_results")
    if checks.empty:
        typer.echo("\nno check results yet")
        return
    latest = checks[checks["run_id"] == checks["run_id"].max()]
    typer.echo(f"\ncurrent health ({latest['run_id'].iloc[0]}): {overall(latest).name}")
    print_results(latest, only_problems=True, limit=20)


state_app = typer.Typer(help="Sync ./state with the `data` branch")
app.add_typer(state_app, name="state")


@state_app.command("pull")
def state_pull(state_dir: StateOpt = None) -> None:
    """Replace the state directory with the remote `data` branch."""
    from cleantape.store.sync import pull

    cfg = _boot(state_dir)
    pull(cfg.root, cfg.state_dir)
    typer.echo(f"pulled data branch into {cfg.state_dir}")


@state_app.command("push")
def state_push(
    message: Annotated[str, typer.Option(help="Commit message")] = "",
    state_dir: StateOpt = None,
) -> None:
    """Force-push the state directory as a single commit on the `data` branch."""
    from cleantape.store.sync import push

    cfg = _boot(state_dir)
    push(cfg.root, cfg.state_dir, message or f"state {new_run_id()}")
    typer.echo("pushed state to origin/data")


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


if __name__ == "__main__":  # pragma: no cover
    set_run_id(new_run_id())
    app()
