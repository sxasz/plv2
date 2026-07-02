"""Config loading and validation."""

from pathlib import Path

import pytest

from polymkt_bot.config import Config, load_config


def test_defaults_are_valid() -> None:
    cfg = Config()
    assert cfg.run.shadow_mode is True
    assert cfg.mode_b.enabled is False  # off until shadow-proven (spec §2.3)
    assert cfg.mode_c.enabled is False


def test_repo_config_yaml_loads(tmp_path: Path) -> None:
    repo_cfg = Path(__file__).resolve().parent.parent / "config.yaml"
    cfg = load_config(repo_cfg, env_file=tmp_path / "nonexistent.env")
    assert cfg.sizing.stake_usdc == 10.0
    assert cfg.window.arm_seconds == 20.0
    assert cfg.run.shadow_mode is True
    assert cfg.secrets.live_interlock_ok is False


def test_unknown_key_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("sizing:\n  stake_usdc: 10\n  not_a_real_knob: 1\n")
    with pytest.raises(ValueError, match="not_a_real_knob"):
        load_config(bad, env_file=tmp_path / "nonexistent.env")


def test_invalid_relationship_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("sizing:\n  stake_usdc: 100.0\n  max_stake_per_window: 25.0\n")
    with pytest.raises(ValueError, match="validation failed"):
        load_config(bad, env_file=tmp_path / "nonexistent.env")


def test_secrets_never_repr() -> None:
    cfg = Config()
    assert "redacted" in repr(cfg.secrets)
