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
