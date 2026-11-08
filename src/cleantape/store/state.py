"""Parquet-backed pipeline state: table schemas, atomic reads/writes, retention.

Layout under the state directory (locally ./state, in CI a checkout of the ``data`` branch):

    curated/observations.parquet   tidy observations, PK (date, instrument_id, source)
    curated/derived.parquet        derived series (spreads, crosses) + changes / z-scores
    meta/manifest.parquet          one row per raw drop
    meta/check_results.parquet     append-only check history
    meta/runs.parquet              run log incl. per-stage status and slot success markers
    raw/{source}/{as_of_date}/...  untouched drops, last N days
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from cleantape.logging_setup import get_logger

log = get_logger("state")

TS = pa.timestamp("us", tz="UTC")

SCHEMAS: dict[str, pa.Schema] = {
    "observations": pa.schema(
        [
            ("date", pa.date32()),
            ("instrument_id", pa.string()),
            ("source", pa.string()),
            ("value", pa.float64()),
            ("ingested_at_utc", TS),
            ("raw_sha256", pa.string()),
        ]
    ),
    "manifest": pa.schema(
        [
            ("run_id", pa.string()),
            ("source", pa.string()),
            ("dataset", pa.string()),
            ("as_of_date", pa.date32()),
            ("path", pa.string()),
            ("received_at_utc", TS),
            ("http_status", pa.int32()),
            ("bytes", pa.int64()),
            ("rows", pa.int64()),
            ("sha256", pa.string()),
            ("duration_ms", pa.float64()),
            ("ok", pa.bool_()),
            ("status", pa.string()),
            ("deferred_rows", pa.int64()),
            ("error", pa.string()),
        ]
    ),
    "check_results": pa.schema(
        [
            ("run_id", pa.string()),
            ("check", pa.string()),
            ("subject", pa.string()),
            ("as_of_date", pa.date32()),
            ("severity", pa.string()),
            ("observed", pa.string()),
            ("expected", pa.string()),
            ("message", pa.string()),
        ]
    ),
    "runs": pa.schema(
        [
            ("run_id", pa.string()),
            ("slot", pa.string()),
            ("ny_date", pa.date32()),
            ("started_at_utc", TS),
            ("finished_at_utc", TS),
            ("stage", pa.string()),
            ("ok", pa.bool_()),
            ("duration_ms", pa.float64()),
            ("detail", pa.string()),
        ]
    ),
}

PRIMARY_KEYS: dict[str, list[str]] = {"observations": ["date", "instrument_id", "source"]}

TABLE_PATHS = {
    "observations": "curated/observations.parquet",
    "derived": "curated/derived.parquet",
    "manifest": "meta/manifest.parquet",
    "check_results": "meta/check_results.parquet",
    "runs": "meta/runs.parquet",
}


class SchemaError(ValueError):
    """A table violates its schema or primary key. Raised before anything is written."""


def empty_frame(table: str) -> pd.DataFrame:
    """An empty DataFrame with the table's columns and dtypes."""
    return SCHEMAS[table].empty_table().to_pandas()


def _to_arrow(table: str, df: pd.DataFrame) -> pa.Table:
    schema = SCHEMAS.get(table)
    if schema is None:  # derived: schema-free, but still parquet
        return pa.Table.from_pandas(df, preserve_index=False)
    missing = [n for n in schema.names if n not in df.columns]
    extra = [c for c in df.columns if c not in schema.names]
    if missing or extra:
        raise SchemaError(f"{table}: missing columns {missing}, unexpected columns {extra}")
    try:
        t = pa.Table.from_pandas(df[schema.names], schema=schema, preserve_index=False)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError) as e:
        raise SchemaError(f"{table}: dtype mismatch: {e}") from e
    pk = PRIMARY_KEYS.get(table)
    if pk:
        dupes = df.duplicated(subset=pk, keep=False)
        if dupes.any():
            sample = df.loc[dupes, pk].head(3).to_dict("records")
            raise SchemaError(f"{table}: {int(dupes.sum())} duplicate primary keys, e.g. {sample}")
        for col in pk:
            if df[col].isna().any():
                raise SchemaError(f"{table}: null in primary-key column {col}")
    return t


@dataclass(frozen=True)
class State:
    """Handle on a state directory."""

    root: Path

    def path(self, table: str) -> Path:
        return self.root / TABLE_PATHS[table]

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    def exists(self) -> bool:
        return self.path("observations").exists()

    def read(self, table: str) -> pd.DataFrame:
        """Read a table; returns an empty, correctly typed frame if it doesn't exist yet."""
        p = self.path(table)
        if not p.exists():
            return empty_frame(table) if table in SCHEMAS else pd.DataFrame()
        df = pq.read_table(p).to_pandas()
        return df

    def write(self, table: str, df: pd.DataFrame) -> None:
        """Validate then atomically replace a table (write temp file, then rename)."""
        arrow = _to_arrow(table, df)
        p = self.path(table)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".parquet.tmp")
        pq.write_table(arrow, tmp, compression="zstd")
        tmp.replace(p)

    def append(self, table: str, rows: pd.DataFrame) -> None:
        """Append rows to an append-only table."""
        if rows.empty:
            return
        current = self.read(table)
        combined = rows if current.empty else pd.concat([current, rows], ignore_index=True)
        self.write(table, combined)

    def prune_raw(self, today: dt.date, retention_days: int) -> int:
        """Delete raw/{source}/{date}/ folders older than ``retention_days``. Returns count."""
        cutoff = today - dt.timedelta(days=retention_days)
        removed = 0
        if not self.raw_dir.exists():
            return 0
        for src in self.raw_dir.iterdir():
            if not src.is_dir():
                continue
            for day in src.iterdir():
                try:
                    d = dt.date.fromisoformat(day.name)
                except ValueError:
                    continue
                if d < cutoff:
                    shutil.rmtree(day)
                    removed += 1
        if removed:
            log.info("pruned %d raw folders older than %s", removed, cutoff)
        return removed
