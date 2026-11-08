"""`cleantape run`: slot guard → ingest → transform → checks → P&L → (report) → site.

Every stage is timed and recorded in ``meta/runs.parquet``. A failing stage is logged and
recorded, and downstream stages still run where they can (the site is always rebuilt so it can
show the failure). The run's final ``run`` row marks the slot as succeeded for the NY date.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from cleantape.clock import Clock, SlotWindow, decide_slot, ny_today
from cleantape.config import Config
from cleantape.logging_setup import get_logger, set_run_id
from cleantape.store.state import State

log = get_logger("pipeline")
CHECK_HISTORY_DAYS = 120


@dataclass
class RunOutcome:
    ran: bool
    reason: str
    run_id: str
    slot: str | None = None
    status: str = "n/a"  # overall check severity
    stage_failures: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 = fine or nothing to do; 1 = checks FAILed or a stage crashed."""
        if not self.ran:
            return 0
        return 1 if (self.status == "FAIL" or self.stage_failures) else 0

    def to_json(self) -> str:
        return json.dumps({**self.__dict__, "exit_code": self.exit_code})


def succeeded_slots(runs: pd.DataFrame) -> set[tuple[dt.date, str]]:
    """(ny_date, slot) pairs that completed (a `run` row with ok=True)."""
    if runs.empty:
        return set()
    done = runs[(runs["stage"] == "run") & runs["ok"]]
    return {(d, s) for d, s in zip(done["ny_date"], done["slot"], strict=True)}


def windows(cfg: Config) -> list[SlotWindow]:
    return [SlotWindow(name, s.start, s.end) for name, s in cfg.settings.slots.items()]


def summarize(out: Any) -> str:
    """Short, human detail string for a stage result."""
    if out is None:
        return ""
    if isinstance(out, dict) and "totals" in out:
        t = out["totals"]
        return f"pnl_usd={t['pnl_usd']:,.0f} n_a={t['n_a']}"
    return str(out)[:300]


class StageRecorder:
    def __init__(self, run_id: str, slot: str, ny_date: dt.date, clock: Clock) -> None:
        self.run_id, self.slot, self.ny_date, self.clock = run_id, slot, ny_date, clock
        self.rows: list[dict[str, Any]] = []
        self.failures: list[str] = []

    def stage(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run one stage; never raises. Returns fn's result or None on failure."""
        started = self.clock.now_utc()
        t0 = time.perf_counter()
        log.info("stage=%s start", name)
        ok, detail, out = True, "", None
        try:
            out = fn()
            detail = summarize(out)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:500]
            self.failures.append(name)
            log.error("stage=%s FAILED: %s\n%s", name, e, traceback.format_exc())
        ms = (time.perf_counter() - t0) * 1000
        log.info("stage=%s end ok=%s duration_ms=%.0f", name, ok, ms)
        self.rows.append(
            {
                "run_id": self.run_id,
                "slot": self.slot,
                "ny_date": self.ny_date,
                "started_at_utc": started,
                "finished_at_utc": self.clock.now_utc(),
                "stage": name,
                "ok": ok,
                "duration_ms": round(ms, 1),
                "detail": detail,
            }
        )
        return out

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def run(
    cfg: Config,
    clock: Clock,
    run_id: str,
    slot: str = "auto",
    force: bool = False,
    ingest_fn: Callable[[], Any] | None = None,
) -> RunOutcome:
    """Execute one pipeline run (or decide not to). ``ingest_fn`` is injectable for tests."""
    from cleantape import cli  # stage helpers shared with the individual commands
    from cleantape.checks.runner import build_context, overall, persist, run_checks
    from cleantape.ingest.run import lookback_start, run_ingest
    from cleantape.report.build import default_report_date, write_report
    from cleantape.site.build import build_site
    from cleantape.transform.derive import series_catalog

    set_run_id(run_id)
    state = State(cfg.state_dir)
    now = clock.now_utc()
    decision = decide_slot(now, windows(cfg), slot, succeeded_slots(state.read("runs")), force)
    if not decision.run:
        log.info("nothing to do: %s", decision.reason)
        return RunOutcome(False, decision.reason, run_id, decision.slot)
    assert decision.slot is not None
    log.info("running slot=%s ny_date=%s (%s)", decision.slot, decision.ny_date, decision.reason)
    if not state.exists():
        log.warning(
            "state is empty: bootstrap with `make bootstrap` (laptop) then "
            "scripts/push_state.sh to create the data branch"
        )
    rec = StageRecorder(run_id, decision.slot, decision.ny_date, clock)

    def ingest() -> Any:
        if ingest_fn is not None:
            return ingest_fn()
        start = lookback_start(ny_today(now), cfg.settings.ingest.lookback_business_days)
        s = run_ingest(cfg, state, clock, run_id, start)
        if not s.drops or len(s.failed_sources) == 4:
            raise RuntimeError(f"no source delivered data: {s}")
        return s

    rec.stage("ingest", ingest)
    rec.stage("transform", lambda: cli.run_transform(cfg))

    results_box: dict[str, Any] = {}

    def checks() -> str:
        ctx = build_context(cfg, state, now, run_id)
        res = run_checks(ctx)
        persist(state, res)
        results_box["status"] = overall(res).name
        counts = res["severity"].value_counts().to_dict()
        return f"overall={results_box['status']} {counts}"

    rec.stage("checks", checks)
    portfolio = rec.stage("pnl", lambda: cli.compute_portfolio(cfg))
    if cfg.settings.slots[decision.slot].report:

        def report() -> str:
            day = default_report_date(state, series_catalog(cfg.universe))
            if day is None:
                raise RuntimeError("no data to report on")
            return str(write_report(cfg, state, day, now, portfolio, rec.failures)["html"])

        rec.stage("report", report)
    rec.stage("prune", lambda: prune_history(state, ny_today(now)))
    # record before the site build so the site can show this run's stage failures
    rec.stage(
        "build_site",
        lambda: build_site(
            cfg, state, now, portfolio, run_info={"stage_failures": list(rec.failures)}
        )["bytes"],
    )
    status = results_box.get("status", "FAIL")
    rec.rows.append(
        {
            "run_id": run_id,
            "slot": decision.slot,
            "ny_date": decision.ny_date,
            "started_at_utc": now,
            "finished_at_utc": clock.now_utc(),
            "stage": "run",
            "ok": not rec.failures,
            "duration_ms": None,
            "detail": f"checks={status} failed_stages={rec.failures}",
        }
    )
    state.append("runs", rec.frame())
    outcome = RunOutcome(True, decision.reason, run_id, decision.slot, status, rec.failures)
    log.info(
        "run finished: slot=%s checks=%s failed_stages=%s exit=%d",
        decision.slot,
        status,
        rec.failures,
        outcome.exit_code,
    )
    return outcome


def prune_history(state: State, today: dt.date) -> int:
    """Keep ~4 months of check results so the data branch stays small."""
    c = state.read("check_results")
    if c.empty:
        return 0
    cutoff = (today - dt.timedelta(days=CHECK_HISTORY_DAYS)).strftime("%Y%m%d")
    keep = c[c["run_id"].str.slice(0, 8) >= cutoff]
    if len(keep) < len(c):
        state.write("check_results", keep)
    return len(c) - len(keep)


def write_outcome(path: Path, outcome: RunOutcome) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(outcome.to_json())
