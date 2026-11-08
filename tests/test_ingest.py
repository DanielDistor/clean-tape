from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from conftest import FIXTURES

from cleantape.clock import NY, Clock, FixedClock
from cleantape.config import Config, Instrument
from cleantape.ingest.base import FetchError, RawDrop, backoff_delays, with_retries
from cleantape.ingest.run import run_ingest
from cleantape.store.state import SchemaError, State, empty_frame

NOW = FixedClock(dt.datetime(2025, 11, 21, 19, 0, tzinfo=NY))  # Friday 19:00 ET


def drop(
    source: str, dataset: str, body: bytes, clock: Clock, ext: str = "csv", **meta: str
) -> RawDrop:
    return RawDrop(
        source,
        dataset,
        ext,
        body,
        clock.now_utc(),
        200,
        1.0,
        ok=True,
        rows=body.count(b"\n"),
        meta=dict(meta),
    )


def failed(source: str, dataset: str, clock: Clock) -> RawDrop:
    return RawDrop(
        source, dataset, "csv", b"", clock.now_utc(), 503, 1.0, ok=False, error="HTTP 503"
    )


class FakeSource:
    """Returns preset drops; records calls."""

    def __init__(self, name: str, make) -> None:  # type: ignore[no-untyped-def]
        self.name = name
        self.make = make
        self.calls: list[list[str]] = []

    def fetch(self, instruments: list[Instrument], start, end, clock: Clock) -> list[RawDrop]:  # type: ignore[no-untyped-def]
        self.calls.append([i.id for i in instruments])
        return self.make(instruments, clock)


def yahoo_body(rows: list[tuple[str, str, float]]) -> bytes:
    return ("date,symbol,close\n" + "".join(f"{d},{s},{v}\n" for d, s, v in rows)).encode()


def sources(
    fred_body: bytes = b"", yahoo_rows=None, yahoo_missing: str = "", nasdaq=None, ecb_fail=False
):  # type: ignore[no-untyped-def]
    fred_dgs10 = fred_body or (FIXTURES / "fred_DGS10.csv").read_bytes()
    rows = (
        yahoo_rows
        if yahoo_rows is not None
        else [("2025-11-20", "SPY", 660.0), ("2025-11-21", "SPY", 665.0)]
    )
    return {
        "fred": FakeSource("fred", lambda ins, c: [drop("fred", "DGS10", fred_dgs10, c)]),
        "yahoo": FakeSource(
            "yahoo",
            lambda ins, c: [
                drop("yahoo", "daily_close", yahoo_body(rows), c, missing=yahoo_missing)
            ],
        ),
        "nasdaq": FakeSource("nasdaq", nasdaq or (lambda ins, c: [])),
        "ecb": FakeSource(
            "ecb",
            lambda ins, c: (
                [failed("ecb", "EXR", c)]
                if ecb_fail
                else [drop("ecb", "EXR", (FIXTURES / "ecb_exr.csv").read_bytes(), c)]
            ),
        ),
    }


def obs(state: State) -> pd.DataFrame:
    return state.read("observations")


def test_end_to_end_ingest_writes_observations_and_manifest(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    s = run_ingest(tmp_cfg, st, NOW, "r1", dt.date(2025, 10, 1), sources=sources())
    assert s.ok and s.inserted > 0
    o = obs(st)
    assert {"DGS10", "SPY", "EURUSD_ECB", "USDJPY_ECB"} <= set(o["instrument_id"])
    # Veterans Day null dropped (non-session on us_bond), not stored as a value
    assert not ((o["instrument_id"] == "DGS10") & (o["date"] == dt.date(2025, 11, 11))).any()
    m = st.read("manifest")
    assert set(m["status"]) == {"new"} and len(m) == 3
    assert (st.root / m["path"].iloc[0]).exists()


def test_same_hash_is_skipped(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    run_ingest(tmp_cfg, st, NOW, "r1", dt.date(2025, 10, 1), sources=sources())
    before = obs(st)
    s2 = run_ingest(tmp_cfg, st, NOW, "r2", dt.date(2025, 10, 1), sources=sources())
    assert s2.unchanged_drops == 3 and s2.inserted == 0 and s2.revised == 0
    pd.testing.assert_frame_equal(before, obs(st))
    assert list(st.read("manifest")["status"][-3:]) == ["unchanged"] * 3


def test_new_hash_is_a_revision_and_reprocessed(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    run_ingest(tmp_cfg, st, NOW, "r1", dt.date(2025, 10, 1), sources=sources())
    revised = (
        (FIXTURES / "fred_DGS10.csv").read_bytes().replace(b"2025-11-10,4.13", b"2025-11-10,4.15")
    )
    s2 = run_ingest(
        tmp_cfg, st, NOW, "r2", dt.date(2025, 10, 1), sources=sources(fred_body=revised)
    )
    assert s2.revised == 1 and s2.inserted == 0
    o = obs(st)
    v = o[(o["instrument_id"] == "DGS10") & (o["date"] == dt.date(2025, 11, 10))]["value"]
    assert v.tolist() == [4.15]
    m = st.read("manifest")
    rev = m[m["status"] == "revision"]
    assert len(rev) == 1 and ".rr2." in rev["path"].iloc[0]


def test_failing_source_does_not_stop_others(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    s = run_ingest(tmp_cfg, st, NOW, "r1", dt.date(2025, 10, 1), sources=sources(ecb_fail=True))
    assert s.failed_sources == ["ecb"] and not s.ok
    assert {"DGS10", "SPY"} <= set(obs(st)["instrument_id"])
    m = st.read("manifest")
    row = m[m["source"] == "ecb"].iloc[0]
    assert not row["ok"] and row["status"] == "error" and row["http_status"] == 503


def test_yahoo_missing_symbol_triggers_nasdaq_fallback(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    nas_body = (FIXTURES / "nasdaq_SPY.json").read_bytes()
    srcs = sources(
        yahoo_rows=[],
        yahoo_missing="SPY,^VIX",
        nasdaq=lambda ins, c: [drop("nasdaq", "SPY", nas_body, c, ext="json")],
    )
    clock = FixedClock(dt.datetime(2026, 11, 7, 19, 0, tzinfo=NY))
    s = run_ingest(tmp_cfg, st, clock, "r1", dt.date(2026, 10, 1), sources=srcs)
    assert srcs["nasdaq"].calls == [["SPY"]]  # VIX has no fallback
    assert s.fallback_used == ["SPY"]
    o = obs(st)
    assert set(o[o["instrument_id"] == "SPY"]["source"]) == {"nasdaq"}


def test_partial_bars_are_not_stored(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    rows = [
        ("2025-11-20", "SPY", 660.0),
        ("2025-11-21", "SPY", 661.0),
        ("2025-11-20", "EURUSD=X", 1.15),
        ("2025-11-21", "EURUSD=X", 1.16),
    ]
    midday = FixedClock(dt.datetime(2025, 11, 21, 13, 0, tzinfo=NY))
    run_ingest(tmp_cfg, st, midday, "r1", dt.date(2025, 11, 1), sources=sources(yahoo_rows=rows))
    o = obs(st)
    latest = o[o["source"] == "yahoo"].groupby("instrument_id")["date"].max().to_dict()
    assert latest == {"SPY": dt.date(2025, 11, 20), "EURUSD": dt.date(2025, 11, 20)}
    evening = FixedClock(dt.datetime(2025, 11, 21, 19, 0, tzinfo=NY))
    run_ingest(tmp_cfg, st, evening, "r2", dt.date(2025, 11, 1), sources=sources(yahoo_rows=rows))
    o = obs(st)
    latest = o[o["source"] == "yahoo"].groupby("instrument_id")["date"].max().to_dict()
    # SPY closed at 16:00 → stored; EURUSD's Nov 21 bar is still forming until NY midnight
    assert latest == {"SPY": dt.date(2025, 11, 21), "EURUSD": dt.date(2025, 11, 20)}


# ------------------------------------------------------------------ store


def test_duplicate_primary_key_rejected(tmp_path: Path) -> None:
    st = State(tmp_path)
    row = {
        "date": dt.date(2025, 1, 2),
        "instrument_id": "X",
        "source": "s",
        "value": 1.0,
        "ingested_at_utc": pd.Timestamp("2025-01-02", tz="UTC"),
        "raw_sha256": "h",
    }
    with pytest.raises(SchemaError, match="duplicate primary keys"):
        st.write("observations", pd.DataFrame([row, row]))
    assert not st.path("observations").exists()


def test_wrong_columns_rejected(tmp_path: Path) -> None:
    with pytest.raises(SchemaError, match="missing columns"):
        State(tmp_path).write("observations", pd.DataFrame({"date": [dt.date(2025, 1, 2)]}))


def test_read_missing_table_is_typed_empty(tmp_path: Path) -> None:
    df = State(tmp_path).read("manifest")
    assert df.empty and list(df.columns) == list(empty_frame("manifest").columns)


def test_prune_raw(tmp_path: Path) -> None:
    st = State(tmp_path)
    for d in ["2025-10-01", "2025-11-20", "notadate"]:
        (tmp_path / "raw" / "fred" / d).mkdir(parents=True)
    assert st.prune_raw(dt.date(2025, 11, 21), 20) == 1
    assert sorted(p.name for p in (tmp_path / "raw" / "fred").iterdir()) == [
        "2025-11-20",
        "notadate",
    ]


# ------------------------------------------------------------------ retries


def test_backoff_grows_with_jitter() -> None:
    import random

    d = backoff_delays(4, 2.0, random.Random(0))
    assert all(2.0 * 2**k * 0.5 <= x <= 2.0 * 2**k for k, x in enumerate(d))


def test_with_retries_retries_then_succeeds() -> None:
    calls, slept = [], []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise FetchError("boom", status=503)
        return "ok"

    assert with_retries(flaky, what="x", retries=4, base_s=1, sleep=slept.append) == "ok"
    assert len(calls) == 3 and len(slept) == 2


def test_with_retries_gives_up_on_non_retryable() -> None:
    calls = []

    def bad() -> None:
        calls.append(1)
        raise FetchError("not found", status=404)

    with pytest.raises(FetchError):
        with_retries(
            bad,
            what="x",
            retries=4,
            base_s=0,
            sleep=lambda s: None,
            retry_if=lambda e: getattr(e, "status", 500) >= 500,
        )
    assert len(calls) == 1


def test_identical_drop_with_deferred_bars_is_rechecked(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    rows = [("2025-11-20", "SPY", 660.0), ("2025-11-21", "SPY", 661.0)]
    for i, hh in enumerate([16, 19, 20]):  # 16:00 → deferred; 19:00 → recheck; 20:00 → unchanged
        clock = FixedClock(dt.datetime(2025, 11, 21, hh, 0, tzinfo=NY))
        run_ingest(
            tmp_cfg, st, clock, f"r{i}", dt.date(2025, 11, 1), sources=sources(yahoo_rows=rows)
        )
    m = st.read("manifest")
    y = m[m["source"] == "yahoo"]
    assert y["status"].tolist() == ["new", "recheck", "unchanged"]
    assert y["deferred_rows"].tolist() == [1, 0, 0]


def test_weekend_bars_are_dropped(tmp_cfg: Config) -> None:
    st = State(tmp_cfg.state_dir)
    rows = [
        ("2025-11-21", "USDJPY=X", 156.0),
        ("2025-11-22", "USDJPY=X", 156.2),
        ("2025-11-23", "USDJPY=X", 156.3),
    ]
    monday = FixedClock(dt.datetime(2025, 11, 24, 19, 0, tzinfo=NY))
    run_ingest(tmp_cfg, st, monday, "r1", dt.date(2025, 11, 1), sources=sources(yahoo_rows=rows))
    o = obs(st)
    assert o[o["instrument_id"] == "USDJPY"]["date"].tolist() == [dt.date(2025, 11, 21)]
