"""Risk gates: tie band, staleness, caps, phases (spec §10)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from polymkt_bot.config import Config
from polymkt_bot.markets.session import MarketSession
from polymkt_bot.risk.risk_manager import GateContext, RiskManager
from polymkt_bot.types import OrderIntent, TimeInForce

from .conftest import UP, WS, make_top


def make_intent(price: float = 0.94, size: float = 10.0) -> OrderIntent:
    return OrderIntent(
        session_slug=f"btc-updown-5m-{WS}",
        token_id=UP,
        outcome="UP",
        side="BUY",
        price=price,
        size=size,
        tif=TimeInForce.FAK,
        strategy="test",
        reason="",
        model_p=0.97,
        edge_net=0.03,
    )


def ok_ctx(session: MarketSession, **overrides: object) -> GateContext:
    session.set_k(100_000.0, WS * 1000)
    base = GateContext(
        session=session,
        now_s=WS + 290.0,  # ARMED phase (10s to close)
        book_top=make_top(ask=0.94, ask_size=100.0),
        book_dirty=False,
        chainlink_age_ms=100.0,
        binance_age_ms=50.0,
        ntp_offset_ms=10.0,
        in_tie_band=False,
        open_exposure_usdc=0.0,
        daily_realized_pnl_usdc=0.0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_clean_entry_passes(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    res = rm.check_entry(make_intent(), ok_ctx(session))
    assert res.allowed, res.failed


@pytest.mark.parametrize(
    ("override", "expected_gate"),
    [
        ({"chainlink_age_ms": 5000.0}, "chainlink_stale"),
        ({"binance_age_ms": 800.0}, "binance_stale"),
        ({"book_dirty": True}, "book_dirty"),
        ({"ntp_offset_ms": 400.0}, "clock_unsynced"),
        ({"ntp_offset_ms": None}, "clock_unsynced"),
        ({"in_tie_band": True}, "tie_band"),
        ({"now_s": WS + 10.0}, "phase:WARMUP"),
        ({"now_s": WS + 299.5}, "phase:LOCKOUT"),
        ({"open_exposure_usdc": 45.0}, "exposure_cap"),
    ],
)
def test_single_gate_failures(
    cfg: Config, session: MarketSession, override: dict[str, object], expected_gate: str
) -> None:
    rm = RiskManager(cfg)
    res = rm.check_entry(make_intent(), ok_ctx(session, **override))
    assert not res.allowed
    assert expected_gate in res.failed


def test_missing_k_blocks(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    ctx = ok_ctx(session)
    session.k = None  # never captured
    res = rm.check_entry(make_intent(), ctx)
    assert "no_price_to_beat" in res.failed


def test_price_cap_and_thin_book(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    res = rm.check_entry(make_intent(price=0.98), ok_ctx(session))
    assert "price_cap" in res.failed
    res = rm.check_entry(
        make_intent(size=80.0),
        ok_ctx(session),  # top holds 100 < 80×1.5
    )
    assert "thin_book" in res.failed


def test_window_stake_cap(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    session.stake_used_usdc = 20.0
    res = rm.check_entry(make_intent(price=0.94, size=10.0), ok_ctx(session))
    assert "window_stake_cap" in res.failed  # 20 + 9.4 > 25


def test_kill_switch_blocks_everything(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    rm.killed = True
    res = rm.check_entry(make_intent(), ok_ctx(session))
    assert "killed" in res.failed


def test_consecutive_losses_trigger_cooldown(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    for i in range(cfg.risk.max_consecutive_losses):
        rm.record_window_result(WS + i * 300, -1.0)
    assert rm.cooldown_until_window > WS
    res = rm.check_entry(make_intent(), ok_ctx(session))
    assert "cooldown" in res.failed


async def test_daily_loss_kill(cfg: Config) -> None:
    rm = RiskManager(cfg)
    fired: list[str] = []

    async def on_kill(reason: str) -> None:
        fired.append(reason)

    rm.set_kill_handler(on_kill)
    await rm.check_daily_loss(-cfg.risk.max_daily_loss_usdc - 1)
    assert rm.killed and fired


def test_below_min_size_blocks(cfg: Config, session: MarketSession) -> None:
    rm = RiskManager(cfg)
    res = rm.check_entry(make_intent(size=1.0), ok_ctx(session))
    assert "below_min_size" in res.failed  # meta.min_order_size = 5
