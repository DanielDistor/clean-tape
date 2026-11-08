"""One-line-per-event logging with the run_id on every line, to console and logs/cleantape.log."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_RUN_ID = "-"
_FMT = "%(asctime)sZ %(levelname)-5s run=%(run_id)s %(name)s: %(message)s"


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _RUN_ID
        return True


def set_run_id(run_id: str) -> None:
    """Set the run id stamped on every subsequent log line."""
    global _RUN_ID
    _RUN_ID = run_id


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Configure the root ``cleantape`` logger (idempotent)."""
    logger = logging.getLogger("cleantape")
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    formatter = logging.Formatter(_FMT, datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime  # timestamps in UTC
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    logs_dir.mkdir(parents=True, exist_ok=True)
    handlers.append(logging.FileHandler(logs_dir / "cleantape.log", encoding="utf-8"))
    for h in handlers:
        h.setFormatter(formatter)
        h.addFilter(_RunIdFilter())
        logger.addHandler(h)
    # yfinance is chatty; keep its warnings but drop its info/debug noise.
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``cleantape`` logger."""
    return logging.getLogger(f"cleantape.{name}")


@contextmanager
def timed(logger: logging.Logger, stage: str) -> Iterator[dict[str, float]]:
    """Log start/end of a stage with its duration; yields a dict that receives ``ms``."""
    info: dict[str, float] = {}
    start = time.perf_counter()
    logger.info("stage=%s start", stage)
    try:
        yield info
    finally:
        info["ms"] = (time.perf_counter() - start) * 1000
        logger.info("stage=%s end duration_ms=%.0f", stage, info["ms"])
