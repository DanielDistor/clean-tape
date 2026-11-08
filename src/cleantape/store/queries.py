"""DuckDB views over the parquet state (parquet is the source of truth; no .duckdb file)."""

from __future__ import annotations

import duckdb
import pandas as pd

from cleantape.config import Universe
from cleantape.store.state import TABLE_PATHS, State
from cleantape.transform.derive import series_catalog

_Q_LATEST = """
SELECT series_id, name, date, round(value, 4) AS value, round(chg_1d, 2) AS chg_1d,
       round(z_1d, 2) AS z
FROM latest WHERE display_group IS NOT NULL
ORDER BY display_group, display_order"""

_Q_2S10S = """
SELECT date, round(value, 1) AS s2s10s_bp
FROM derived WHERE series_id = 'S2S10S'
ORDER BY date DESC LIMIT 10"""

_Q_BIG_Z = """
SELECT date, series_id, round(chg_1d, 2) AS chg_1d, round(z_1d, 1) AS z
FROM derived WHERE z_1d IS NOT NULL
ORDER BY abs(z_1d) DESC LIMIT 10"""

_Q_EURUSD = """
SELECT y.date, y.value AS yahoo, e.value AS ecb,
       round((y.value / e.value - 1) * 1e4, 1) AS diff_bp
FROM observations y
JOIN observations e ON y.date = e.date AND e.instrument_id = 'EURUSD_ECB'
WHERE y.instrument_id = 'EURUSD'
ORDER BY y.date DESC LIMIT 10"""

_Q_MANIFEST = """
SELECT source, status, count(*) AS drops, max(received_at_utc) AS last_received
FROM manifest GROUP BY ALL ORDER BY source, status"""

EXAMPLE_QUERIES = {
    "Latest value and move for every displayed series": _Q_LATEST,
    "2s10s over the last 10 sessions (bp)": _Q_2S10S,
    "Biggest absolute z-score moves since 2020": _Q_BIG_Z,
    "Yahoo vs ECB EUR/USD on the same dates": _Q_EURUSD,
    "Ingest history: drops per source and status": _Q_MANIFEST,
}


def connect(state: State, universe: Universe) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with one view per parquet table plus `catalog` and `latest`."""
    con = duckdb.connect(":memory:")
    for name, rel in TABLE_PATHS.items():
        p = state.root / rel
        if p.exists():
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{p.as_posix()}')")
    cat = pd.DataFrame([m.__dict__ for m in series_catalog(universe).values()])
    con.register("catalog_df", cat)
    con.execute("CREATE VIEW catalog AS SELECT * FROM catalog_df")
    if (state.root / TABLE_PATHS["derived"]).exists():
        con.execute("""
            CREATE VIEW latest AS
            SELECT d.*, c.name, c.display_group, c.display_order, c.asset_class, c.units
            FROM derived d JOIN catalog c USING (series_id)
            QUALIFY row_number() OVER (PARTITION BY series_id ORDER BY date DESC) = 1
        """)
    return con


def query(state: State, universe: Universe, sql: str) -> pd.DataFrame:
    """Run SQL against the views and return a DataFrame."""
    return connect(state, universe).execute(sql).df()
