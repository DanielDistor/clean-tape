"""Landing zone: persist raw drops untouched and record one manifest row per drop.

Idempotency: a drop whose sha256 equals an earlier drop for the same (source, dataset,
as_of_date) is ``unchanged`` and is not re-processed. A different hash for the same key is a
``revision`` — kept under a run-suffixed filename and re-processed (FRED revises data).

Subtlety (found by a test, see BUILD_LOG): if the earlier identical drop had in-progress
bars held back by the partial-bar rule, the same bytes must be re-processed later (status
``recheck``), otherwise a close downloaded at 16:20 ET is never stored that day.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from cleantape.ingest.base import RawDrop
from cleantape.logging_setup import get_logger
from cleantape.store.state import State

log = get_logger("landing")


@dataclass
class Landed:
    drop: RawDrop
    status: str  # new | revision | recheck | unchanged | error
    path: str | None
    deferred: int = 0  # rows held back as in-progress bars when this drop was processed

    @property
    def needs_processing(self) -> bool:
        return self.status in ("new", "revision", "recheck")


def classify(manifest: pd.DataFrame, source: str, dataset: str, as_of: dt.date, sha: str) -> str:
    """new / unchanged / revision, by comparing with earlier OK drops of the same key."""
    if manifest.empty:
        return "new"
    prior = manifest[
        (manifest["source"] == source)
        & (manifest["dataset"] == dataset)
        & (manifest["as_of_date"] == as_of)
        & (manifest["ok"])
    ]
    if prior.empty:
        return "new"
    same = prior[prior["sha256"] == sha]
    if same.empty:
        return "revision"
    if (same["deferred_rows"].fillna(0) == 0).any():
        return "unchanged"
    return "recheck"


def land(state: State, drops: list[RawDrop], run_id: str, as_of: dt.date) -> list[Landed]:
    """Classify each drop and write new/revised ones to raw/{source}/{as_of}/.

    Call ``record`` after normalisation to append the manifest rows.
    """
    manifest = state.read("manifest")
    landed: list[Landed] = []
    for d in drops:
        path: str | None = None
        if d.ok:
            status = classify(manifest, d.source, d.dataset, as_of, d.sha256)
            if status in ("new", "revision"):
                name = (
                    f"{d.dataset}.{d.ext}" if status == "new" else f"{d.dataset}.r{run_id}.{d.ext}"
                )
                rel = (
                    f"raw/{d.source}/{as_of.isoformat()}/{name.replace('^', '_').replace('=', '_')}"
                )
                target = state.root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(d.content)
                path = rel
            if status == "revision":
                log.info(
                    "revision: source=%s dataset=%s as_of=%s (new sha256)",
                    d.source,
                    d.dataset,
                    as_of,
                )
        else:
            status = "error"
        landed.append(Landed(d, status, path))
    return landed


def record(state: State, landed: list[Landed], run_id: str, as_of: dt.date) -> None:
    """Append one manifest row per landed drop."""
    rows = []
    for ld in landed:
        d, status, path = ld.drop, ld.status, ld.path
        rows.append(
            {
                "run_id": run_id,
                "source": d.source,
                "dataset": d.dataset,
                "as_of_date": as_of,
                "path": path,
                "received_at_utc": d.received_at_utc,
                "http_status": d.http_status,
                "bytes": len(d.content),
                "rows": d.rows,
                "sha256": d.sha256 if d.ok else None,
                "duration_ms": round(d.duration_ms, 1),
                "ok": d.ok,
                "status": status,
                "deferred_rows": ld.deferred,
                "error": d.error,
            }
        )
        log.info(
            "drop source=%s dataset=%s status=%s rows=%s bytes=%d ms=%.0f",
            d.source,
            d.dataset,
            status,
            d.rows,
            len(d.content),
            d.duration_ms,
        )
    if rows:
        df = pd.DataFrame(rows)
        df["http_status"] = df["http_status"].astype("Int32")
        df["rows"] = df["rows"].astype("Int64")
        state.append("manifest", df)
