"""Typed configuration: load and validate config/*.yaml, failing fast on bad input."""

from __future__ import annotations

import datetime as dt
from functools import cached_property
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetClass = Literal["rates", "equities", "fx", "commodities", "credit", "vol"]
Units = Literal["pct_yield", "price", "fx_rate", "index_level", "bp_spread"]
ChangeMode = Literal["bp", "pct"]
CalendarName = Literal["nyse", "us_bond", "ecb_target", "fx_weekday"]
SourceName = Literal["fred", "yahoo", "nasdaq", "ecb"]


def repo_root() -> Path:
    """Return the repository root (the directory holding pyproject.toml)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bounds(_Strict):
    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> Bounds:
        if self.min >= self.max:
            raise ValueError(f"bounds.min ({self.min}) must be < bounds.max ({self.max})")
        return self


class Fallback(_Strict):
    source: SourceName
    source_symbol: str
    asset_type: Literal["etf", "stocks", "index"] = "etf"


class CrossCheck(_Strict):
    against: str
    kind: Literal["cross_source", "triangle"]


class Instrument(_Strict):
    id: str
    name: str
    asset_class: AssetClass
    source: SourceName
    source_symbol: str
    units: Units
    change_mode: ChangeMode
    calendar: CalendarName
    expected_lag_business_days: int = Field(ge=0, le=15)
    bounds: Bounds
    display_group: str | None
    display_order: int
    fallback: Fallback | None = None
    cross_check: CrossCheck | None = None
    is_future: bool = False
    roll_proxy: str | None = None  # ETF used to tell a futures roll jump from a real move

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not v.replace("_", "").isalnum() or v != v.upper():
            raise ValueError(f"instrument id must be UPPER_SNAKE alphanumeric, got {v!r}")
        return v


class Spread(_Strict):
    id: str
    name: str
    long: str
    short: str
    display_group: str
    display_order: int


class Cross(_Strict):
    id: str
    name: str
    left: str
    right: str


class Universe(_Strict):
    instruments: list[Instrument]
    spreads: list[Spread] = []
    crosses: list[Cross] = []

    @model_validator(mode="after")
    def _references(self) -> Universe:
        ids = [i.id for i in self.instruments]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate instrument ids: {sorted(dupes)}")
        derived_ids = [s.id for s in self.spreads] + [c.id for c in self.crosses]
        clash = set(ids) & set(derived_ids)
        if clash:
            raise ValueError(f"derived ids clash with instruments: {sorted(clash)}")
        known = set(ids) | set(derived_ids)
        for s in self.spreads:
            for leg in (s.long, s.short):
                if leg not in ids:
                    raise ValueError(f"spread {s.id} references unknown instrument {leg}")
        for c in self.crosses:
            for leg in (c.left, c.right):
                if leg not in ids:
                    raise ValueError(f"cross {c.id} references unknown instrument {leg}")
        for i in self.instruments:
            if i.roll_proxy and i.roll_proxy not in ids:
                raise ValueError(f"{i.id}.roll_proxy references unknown {i.roll_proxy}")
            if i.cross_check and i.cross_check.against not in known:
                raise ValueError(f"{i.id}.cross_check references unknown {i.cross_check.against}")
        return self

    @cached_property
    def by_id(self) -> dict[str, Instrument]:
        """Instruments keyed by id."""
        return {i.id: i for i in self.instruments}

    def by_source(self, source: str) -> list[Instrument]:
        """Instruments whose primary source is ``source``."""
        return [i for i in self.instruments if i.source == source]


class Paths(_Strict):
    state_dir: Path
    site_dir: Path
    logs_dir: Path
    universe: Path
    portfolio: Path


class Slot(_Strict):
    start: dt.time
    end: dt.time
    report: bool

    @model_validator(mode="after")
    def _ordered(self) -> Slot:
        if self.start >= self.end:
            raise ValueError("slot start must be before end (windows may not cross midnight)")
        return self


class Ingest(_Strict):
    backfill_start: dt.date
    lookback_business_days: int = Field(ge=1)
    http_timeout_s: float = Field(gt=0)
    retries: int = Field(ge=0)
    backoff_base_s: float = Field(ge=0)
    raw_retention_days: int = Field(ge=1)
    user_agent: str


class Derive(_Strict):
    zscore_window: int = Field(ge=20)
    zscore_min_obs: int = Field(ge=10)
    ffill_max_days: int = Field(ge=0, le=3)


class Freshness(_Strict):
    warn_business_days: int
    fail_business_days: int


class Completeness(_Strict):
    lookback_business_days: int
    fail_missing_sessions: int = Field(ge=1)
    row_count_tolerance: float = Field(gt=0, lt=1)


class Staleness(_Strict):
    warn_sessions: int = Field(ge=2)


class Outliers(_Strict):
    warn_z: float
    fail_z: float


class Tolerance(_Strict):
    warn: float = Field(gt=0)
    fail: float = Field(gt=0)


class Calibration(_Strict):
    start: dt.date
    warn_quantile: float = Field(gt=0.5, lt=1)
    fail_quantile: float = Field(gt=0.5, lt=1)
    safety_factor: float = Field(ge=1)
    min_price: float = Field(ge=0)
    exclude_roll_days: bool = True


class Checks(_Strict):
    freshness: Freshness
    completeness: Completeness
    staleness: Staleness
    outliers: Outliers
    spread_identity_tolerance_bp: float
    calibration: Calibration
    consistency: dict[str, Tolerance] = {}


class Shock(_Strict):
    name: str
    kind: Literal["rates_bp", "asset_class_pct", "instruments_pct", "usd_pct"]
    size: float
    target: str | None = None


class Site(_Strict):
    title: str
    tagline: str
    base_url: str
    repo_url: str
    stale_after_business_days: int
    report_archive_days: int
    hero: list[str] = []  # series ids shown as big tiles at the top of the Overview


class Settings(_Strict):
    paths: Paths
    display_timezone: str
    slots: dict[Literal["morning", "evening"], Slot]
    nyse_close_buffer: dt.time
    ingest: Ingest
    derive: Derive
    checks: Checks
    shocks: list[Shock]
    site: Site


class Config(BaseModel):
    """Everything the pipeline needs, resolved against a root directory."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    root: Path
    settings: Settings
    universe: Universe

    def path(self, p: Path) -> Path:
        """Resolve a configured relative path against the root."""
        return p if p.is_absolute() else self.root / p

    @property
    def state_dir(self) -> Path:
        return self.path(self.settings.paths.state_dir)

    @property
    def site_dir(self) -> Path:
        return self.path(self.settings.paths.site_dir)

    @property
    def logs_dir(self) -> Path:
        return self.path(self.settings.paths.logs_dir)


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError as e:
        raise ValueError(f"config file not found: {path}") from e
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return data


def load_config(root: Path | None = None, state_dir: Path | None = None) -> Config:
    """Load settings.yaml + universe.yaml from ``root/config``.

    Raises ``ValueError``/``pydantic.ValidationError`` on any invalid value, so a bad edit
    to config fails the run immediately instead of producing wrong numbers later.
    """
    root = root or repo_root()
    raw = _read_yaml(root / "config" / "settings.yaml")
    if state_dir is not None:
        raw.setdefault("paths", {})["state_dir"] = str(state_dir)
    calib = root / "config" / "calibration.yaml"
    if calib.exists():  # generated by `cleantape calibrate`; see docs/DECISIONS.md
        tol = _read_yaml(calib).get("consistency") or {}
        raw.setdefault("checks", {}).setdefault("consistency", {}).update(tol)
    settings = Settings.model_validate(raw)
    universe = Universe.model_validate(_read_yaml(root / settings.paths.universe))
    return Config(root=root, settings=settings, universe=universe)
