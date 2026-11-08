"""Failure demo: a copy of the real dashboard with one deliberately broken value.

Built into site/demo/ on every site build so anyone can click through what the check engine
does when a vendor sends garbage: the latest 10-year Treasury yield is replaced with 250%
(a classic decimal-point error), derived data and checks are recomputed on the copy, and the
normal site builder renders it with a DEMO banner. The real state is never modified.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path
from typing import Any

from cleantape.config import Config
from cleantape.logging_setup import get_logger
from cleantape.store.state import State

log = get_logger("site.demo")
BROKEN_SERIES = "DGS10"
BROKEN_VALUE = 250.0


def build_failure_demo(
    cfg: Config, state: State, now_utc: dt.datetime, out_dir: Path
) -> dict[str, Any] | None:
    """Render the failure demo into ``out_dir``. Returns build info, or None without data."""
    from cleantape.checks.runner import build_context, persist, run_checks
    from cleantape.portfolio import pnl
    from cleantape.site.build import build_site
    from cleantape.transform.derive import build_derived

    obs = state.read("observations")
    rows = obs[obs["instrument_id"] == BROKEN_SERIES]
    if rows.empty:
        log.warning("no %s data; skipping failure demo", BROKEN_SERIES)
        return None
    obs = obs.copy()
    obs.loc[rows.sort_values("date").index[-1], "value"] = BROKEN_VALUE
    with tempfile.TemporaryDirectory(prefix="cleantape-demo-") as tmp:
        demo = State(Path(tmp))
        demo.write("observations", obs)
        demo.write("manifest", state.read("manifest"))
        d = cfg.settings.derive
        demo.write(
            "derived",
            build_derived(obs, cfg.universe, d.zscore_window, d.zscore_min_obs, d.ffill_max_days),
        )
        run_id = now_utc.strftime("%Y%m%dT%H%M%SZ")
        results = run_checks(build_context(cfg, demo, now_utc, run_id))
        persist(demo, results)
        flagged = set(results[results["severity"] == "FAIL"]["subject"])
        positions = pnl.load_positions(cfg.path(cfg.settings.paths.portfolio), cfg.universe)
        wide = pnl.wide_values(demo.read("derived"))
        as_of = pnl.expected_dates(cfg, positions, now_utc)
        pos = pnl.value_positions(positions, wide, cfg.universe, as_of, flagged)
        hist_dates = sorted(wide.index[wide.index <= max(as_of.values())])[-31:]
        portfolio = {
            "positions": pos,
            "by_book": pnl.aggregate(pos, "book"),
            "by_asset_class": pnl.aggregate(pos, "asset_class"),
            "totals": pnl.totals(pos),
            "shocks": pnl.shocks_table(pos, cfg.settings.shocks),
            "history": pnl.pnl_history(positions, wide, cfg.universe, hist_dates[1:], flagged),
        }
        info = build_site(
            cfg,
            demo,
            now_utc,
            portfolio,
            out_dir=out_dir,
            run_info={"demo": {"series": "UST 10Y", "value": "250%"}},
        )
    log.info("failure demo built: %s (status=%s)", out_dir, info["status"])
    return info
