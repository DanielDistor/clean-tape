from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

import pytest
from broken_scenarios import clean_manifest, clean_obs, context
from conftest import ROOT

from cleantape.clock import NY, FixedClock
from cleantape.config import Config, load_config
from cleantape.pipeline import run, succeeded_slots
from cleantape.store.state import State
from cleantape.store.sync import SyncError, pull, push

# Saturday 2026-11-07 19:00 ET is inside the evening window; the fixture sample ends Fri 11-06.
EVENING = FixedClock(dt.datetime(2026, 11, 7, 19, 0, tzinfo=NY))
LATER_EVENING = FixedClock(dt.datetime(2026, 11, 7, 20, 30, tzinfo=NY))
AFTERNOON = FixedClock(dt.datetime(2026, 11, 7, 14, 0, tzinfo=NY))


def seeded(tmp_path: Path) -> Config:
    c = load_config(ROOT, state_dir=tmp_path / "state")
    st = State(c.state_dir)
    obs = clean_obs()
    st.write("observations", obs)
    st.write("manifest", clean_manifest())
    st.write("derived", context(c, obs).derived)
    return c


def site_dir(c: Config) -> Path:
    return c.site_dir


@pytest.fixture()
def cfg_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    c = seeded(tmp_path)
    # keep the site out of the real repo
    monkeypatch.setattr(type(c), "site_dir", property(lambda self: tmp_path / "site"))
    monkeypatch.setenv("CLEANTAPE_NOW", EVENING.at.isoformat())
    return c


def test_evening_run_does_all_stages_and_marks_slot(cfg_tmp: Config) -> None:
    out = run(cfg_tmp, EVENING, "r1", ingest_fn=lambda: "fake ingest")
    assert out.ran and out.slot == "evening" and out.stage_failures == []
    runs = State(cfg_tmp.state_dir).read("runs")
    assert list(runs["stage"]) == [
        "ingest",
        "transform",
        "checks",
        "pnl",
        "report",
        "prune",
        "build_site",
        "run",
    ]
    assert (dt.date(2026, 11, 7), "evening") in succeeded_slots(runs)
    assert (cfg_tmp.state_dir / "reports" / "2026-11-06" / "eod_2026-11-06.xlsx").exists()
    assert (cfg_tmp.site_dir / "index.html").exists()


def test_second_cron_same_slot_is_a_noop(cfg_tmp: Config) -> None:
    run(cfg_tmp, EVENING, "r1", ingest_fn=lambda: "x")
    again = run(cfg_tmp, LATER_EVENING, "r2", ingest_fn=lambda: "x")
    assert not again.ran and "already ran" in again.reason and again.exit_code == 0


def test_outside_window_is_a_noop_but_force_runs(cfg_tmp: Config) -> None:
    assert not run(cfg_tmp, AFTERNOON, "r1", ingest_fn=lambda: "x").ran
    forced = run(cfg_tmp, AFTERNOON, "r2", force=True, ingest_fn=lambda: "x")
    assert forced.ran and forced.slot == "evening"


def test_stage_failure_is_recorded_and_site_still_built(cfg_tmp: Config) -> None:
    def broken() -> None:
        raise ConnectionError("all sources down")

    out = run(cfg_tmp, EVENING, "r1", ingest_fn=broken)
    assert out.stage_failures == ["ingest"] and out.exit_code == 1
    runs = State(cfg_tmp.state_dir).read("runs")
    assert not runs[runs["stage"] == "ingest"]["ok"].iloc[0]
    assert not runs[runs["stage"] == "run"]["ok"].iloc[0]  # slot not marked as succeeded
    html = (cfg_tmp.site_dir / "index.html").read_text()
    assert "failing stages: ingest" in html


def test_morning_run_has_no_report(cfg_tmp: Config) -> None:
    morning = FixedClock(dt.datetime(2026, 11, 9, 7, 0, tzinfo=NY))
    out = run(cfg_tmp, morning, "r1", ingest_fn=lambda: "x")
    assert out.ran and out.slot == "morning"
    stages = list(State(cfg_tmp.state_dir).read("runs")["stage"])
    assert "report" not in stages


# ------------------------------------------------------------------ data-branch sync


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def test_push_pull_roundtrip_keeps_one_commit(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    state = repo / "state"
    st = State(state)
    st.write("observations", clean_obs())
    with pytest.raises(SyncError, match="does not exist"):
        pull(repo, tmp_path / "elsewhere")
    push(repo, state, "state r1")
    (state / "extra.txt").write_text("second run")
    push(repo, state, "state r2")
    log = _git("log", "--oneline", "data", cwd=remote)
    assert len(log.strip().splitlines()) == 1 and "state r2" in log  # history never grows
    other = tmp_path / "pulled"
    pull(repo, other)
    assert (other / "curated" / "observations.parquet").exists()
    assert (other / "extra.txt").read_text() == "second run"


def test_push_refuses_empty_state(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="empty state"):
        push(tmp_path, tmp_path / "state", "x")
