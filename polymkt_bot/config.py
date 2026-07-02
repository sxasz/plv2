"""Typed configuration, loaded from config.yaml + .env secrets.

Fee rates, tick sizes and min sizes are deliberately absent: they are queried
live from the CLOB per market (FACTS.md #2.3, #3.6) and trading is blocked
until they are known.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from dotenv import load_dotenv

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RunConfig:
    shadow_mode: bool = True
    data_dir: str = "data"
    ntp_server: str = "pool.ntp.org"
    max_ntp_offset_ms: float = 250.0
    status_file: str = "data/status.json"


@dataclass(frozen=True, slots=True)
class EndpointsConfig:
    clob_rest: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com"
    gamma_rest: str = "https://gamma-api.polymarket.com"
    rtds_ws: str = "wss://ws-live-data.polymarket.com"
    binance_ws: str = (
        "wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade"
    )


@dataclass(frozen=True, slots=True)
class SizingConfig:
    stake_usdc: float = 10.0
    max_stake_per_window: float = 25.0
    depth_participation: float = 0.5
    absolute_max_price: float = 0.97
    kelly_fraction: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_open_exposure_usdc: float = 50.0
    max_daily_loss_usdc: float = 30.0
    max_consecutive_losses: int = 4
    cooldown_windows: int = 6
    max_cl_age_ms: float = 3000.0
    max_bn_age_ms: float = 500.0
    min_liquidity_multiple: float = 1.5
    warmup_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class WindowConfig:
    arm_seconds: float = 20.0
    min_seconds: float = 2.0
    prefetch_seconds: float = 60.0
    length_s: int = 300


@dataclass(frozen=True, slots=True)
class ModelConfig:
    ewma_halflife_s: float = 600.0
    vol_floor_per_s: float = 1e-6
    lambda_lead: float = 1.0
    basis_window_s: float = 600.0
    k_tie: float = 1.0
    p_clip: float = 0.001
    tie_bonus: float = 0.005


@dataclass(frozen=True, slots=True)
class ModeAConfig:
    enabled: bool = True
    p_min_snipe: float = 0.93
    max_snipe_price: float = 0.95
    min_edge_snipe: float = 0.02
    slippage_buffer: float = 0.005


@dataclass(frozen=True, slots=True)
class ModeBConfig:
    enabled: bool = False
    impulse_threshold: float = 0.0007
    impulse_window_ms: float = 1500.0
    min_edge_scalp: float = 0.03
    scalp_timeout_s: float = 25.0
    scalp_stop_edge: float = -0.02
    maker_exit_offset: float = 0.01


@dataclass(frozen=True, slots=True)
class ModeCConfig:
    enabled: bool = False
    half_spread: float = 0.02
    max_inventory_usdc: float = 20.0


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    rtt_ms_p50: float = 80.0
    rtt_ms_p95: float = 200.0


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass(frozen=True, slots=True)
class Secrets:
    """Loaded from environment only. Never logged, never serialized."""

    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    funder_address: str = ""
    enable_live_trading: str = ""

    def __repr__(self) -> str:  # defence-in-depth: never leak into logs
        return "Secrets(<redacted>)"

    @property
    def live_interlock_ok(self) -> bool:
        return self.enable_live_trading == "YES_I_ACCEPT_REAL_LOSSES"


@dataclass(frozen=True, slots=True)
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    endpoints: EndpointsConfig = field(default_factory=EndpointsConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    mode_a: ModeAConfig = field(default_factory=ModeAConfig)
    mode_b: ModeBConfig = field(default_factory=ModeBConfig)
    mode_c: ModeCConfig = field(default_factory=ModeCConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    secrets: Secrets = field(default_factory=Secrets)


def _build(cls: type[_T], raw: dict[str, Any]) -> _T:
    """Construct a dataclass from a dict, rejecting unknown keys."""
    assert is_dataclass(cls)
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    return cls(**raw)


def _validate(cfg: Config) -> None:
    w, s, r = cfg.window, cfg.sizing, cfg.risk
    checks: list[tuple[bool, str]] = [
        (0 < w.min_seconds < w.arm_seconds < w.length_s, "min < arm < window length"),
        (0 < s.stake_usdc <= s.max_stake_per_window, "stake_usdc <= max_stake_per_window"),
        (s.max_stake_per_window <= r.max_open_exposure_usdc, "stake cap <= exposure cap"),
        (0.5 < cfg.mode_a.p_min_snipe < 1.0, "p_min_snipe in (0.5, 1)"),
        (cfg.mode_a.max_snipe_price <= s.absolute_max_price, "snipe price <= absolute max"),
        (0.0 < s.absolute_max_price < 1.0, "absolute_max_price in (0, 1)"),
        (0.0 < s.depth_participation <= 1.0, "depth_participation in (0, 1]"),
        (r.max_cl_age_ms > 0 and r.max_bn_age_ms > 0, "staleness limits positive"),
    ]
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        raise ValueError(f"config validation failed: {bad}")


def load_config(path: str | Path = "config.yaml", env_file: str | Path = ".env") -> Config:
    raw: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        with p.open("rb") as fh:
            raw = yaml.safe_load(fh) or {}

    load_dotenv(env_file, override=False)
    secrets = Secrets(
        private_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
        api_key=os.environ.get("POLYMARKET_API_KEY", ""),
        api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", ""),
        funder_address=os.environ.get("POLYMARKET_FUNDER_ADDRESS", ""),
        enable_live_trading=os.environ.get("ENABLE_LIVE_TRADING", ""),
    )

    cfg = Config(
        run=_build(RunConfig, raw.get("run", {})),
        endpoints=_build(EndpointsConfig, raw.get("endpoints", {})),
        sizing=_build(SizingConfig, raw.get("sizing", {})),
        risk=_build(RiskConfig, raw.get("risk", {})),
        window=_build(WindowConfig, raw.get("window", {})),
        model=_build(ModelConfig, raw.get("model", {})),
        mode_a=_build(ModeAConfig, raw.get("mode_a", {})),
        mode_b=_build(ModeBConfig, raw.get("mode_b", {})),
        mode_c=_build(ModeCConfig, raw.get("mode_c", {})),
        shadow=_build(ShadowConfig, raw.get("shadow", {})),
        dashboard=_build(DashboardConfig, raw.get("dashboard", {})),
        secrets=secrets,
    )
    _validate(cfg)
    return cfg
