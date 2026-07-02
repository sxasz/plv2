"""Session lifecycle, phases, tie resolution, K idempotency."""

from __future__ import annotations

import pytest

from polymkt_bot.clock import seconds_to_close, slug_for_window, window_start
from polymkt_bot.markets.session import MarketSession, Phase, SessionState

from .conftest import WS


def test_window_math() -> None:
    assert window_start(WS + 123.4) == WS
    assert seconds_to_close(WS + 123.4) == pytest.approx(176.6)
    assert slug_for_window(WS) == f"btc-updown-5m-{WS}"


def test_phases(session: MarketSession) -> None:
    assert session.phase(WS + 5) is Phase.WARMUP
    assert session.phase(WS + 100) is Phase.MID
    assert session.phase(WS + 281) is Phase.ARMED
    assert session.phase(WS + 299) is Phase.LOCKOUT
    assert session.phase(WS + 300) is Phase.CLOSED


def test_tie_resolves_up(session: MarketSession) -> None:
    session.set_k(100_000.0, WS * 1000)
    assert session.settle(100_000.0) == "UP"  # close == open → UP (FACTS 1.3)


def test_down_resolution(session: MarketSession) -> None:
    session.set_k(100_000.0, WS * 1000)
    assert session.settle(99_999.99) == "DOWN"


def test_k_capture_idempotent_earliest_wins(session: MarketSession) -> None:
    session.set_k(100_010.0, WS * 1000 + 500)
    session.set_k(100_020.0, WS * 1000 + 900)  # later print: ignored
    assert session.k == 100_010.0
    session.set_k(100_005.0, WS * 1000 + 100)  # earlier print (backfill): wins
    assert session.k == 100_005.0


def test_settle_without_k_raises(session: MarketSession) -> None:
    with pytest.raises(RuntimeError):
        session.settle(100_000.0)


def test_state_machine_transitions(session: MarketSession) -> None:
    session.transition(SessionState.ACTIVE)
    session.transition(SessionState.CLOSING)
    session.transition(SessionState.SETTLING)
    session.transition(SessionState.DONE)
    with pytest.raises(ValueError):
        session.transition(SessionState.ACTIVE)  # DONE is terminal


def test_illegal_state_jump(session: MarketSession) -> None:
    with pytest.raises(ValueError):
        session.transition(SessionState.SETTLING)  # PENDING → SETTLING forbidden
