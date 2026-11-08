from __future__ import annotations

from pathlib import Path

import pytest

from cleantape.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(ROOT)


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> Config:
    """Real config, but state lives in a temporary directory."""
    return load_config(ROOT, state_dir=tmp_path / "state")
