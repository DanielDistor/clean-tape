from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from cleantape.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_real_config_loads(cfg: Config) -> None:
    ids = cfg.universe.by_id
    assert "DGS10" in ids and "EURUSD_ECB" in ids
    assert ids["DGS10"].change_mode == "bp"
    assert cfg.settings.slots["evening"].report is True


def _copy_config(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


def test_bad_bounds_fail_fast(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    uni = root / "config" / "universe.yaml"
    uni.write_text(
        uni.read_text().replace(
            "bounds: {min: -1.0, max: 20.0}", "bounds: {min: 20.0, max: -1.0}", 1
        )
    )
    with pytest.raises(ValidationError, match=r"bounds\.min"):
        load_config(root)


def test_unknown_spread_leg_fails(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    uni = root / "config" / "universe.yaml"
    uni.write_text(uni.read_text().replace("long: DGS10, short: DGS2", "long: DGS99, short: DGS2"))
    with pytest.raises(ValidationError, match="unknown instrument DGS99"):
        load_config(root)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    s = root / "config" / "settings.yaml"
    s.write_text(s.read_text() + "\nunexpected_key: 1\n")
    with pytest.raises(ValidationError):
        load_config(root)


def test_invalid_yaml_is_a_clear_error(tmp_path: Path) -> None:
    root = _copy_config(tmp_path)
    (root / "config" / "settings.yaml").write_text("paths: [unclosed")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_config(root)
