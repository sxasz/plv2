"""Window rollover with in-flight orders (spec §10): the boundary teardown
sequence, late-fill routing to the owning session, and cooldown wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from polymkt_bot.config import Config
from polymkt_bot.feeds.rtds_chainlink_ws import KCapture
from polymkt_bot.markets.session import SessionState
from polymkt_bot.types import Fill

from .engine_harness import EngineHarness
from .frames import DOWN, UP, WS, meta_for

pytestmark = pytest.mark.asyncio

SLUG_A = f"btc-updown-5m-{WS}"


def book_snapshot(token: str, bid: float, ask: float) -> str:
    return orjson.dumps(
        {
            "event_type": "book",
            "asset_id": token,
            "bids": [{"price": f"{bid:.3f}", "size": "500"}],
            "asks": [{"price": f"{ask:.3f}", "size": "400"}],
            "hash": "h",
        }
    ).decode()


async def make_active_harness(tmp: Path) -> EngineHarness:
    h = EngineHarness(Config(), {WS: meta_for(WS), WS + 300: meta_for(WS + 300)}, tmp)
    h.clock.set(int(10e9), int((WS + 10) * 1e9))
    await h.engine.manage_sessions(h.clock.wall_s())
    assert h.engine.current is not None and h.engine.current.slug == SLUG_A
    h.engine.current.set_k(100_000.0, WS * 1000 + 40)  # boundary print captured
    # Books arrive.
    h.clob.on_message(book_snapshot(UP, 0.90, 0.92), h.clock.mono_ns(), h.clock.wall_ns())
    h.clob.on_message(book_snapshot(DOWN, 0.07, 0.09), h.clock.mono_ns(), h.clock.wall_ns())
    return h


async def test_boundary_teardown_with_inflight_order(tmp_path: Path) -> None:
    h = await make_active_harness(tmp_path)
    try:
        sess_a = h.engine.current
        assert sess_a is not None

        # A resting GTC order is open when the window closes.
        from .test_order_fsm import make_intent

        intent = make_intent()
        order = h.om.draft(intent, decision_mono_ns=h.clock.mono_ns())
        await h.om.submit(order)
        h.clock.set(int(12e9), int((WS + 12) * 1e9))
        h.shadow.process_due(h.clock.mono_ns())  # arrives, rests? (FAK: fills)
        # Use state as-is; what matters is boundary behavior below.

        # T-0: window closes.
        h.clock.set(int(301e9), int((WS + 301) * 1e9))
        await h.engine.manage_sessions(h.clock.wall_s())
        assert sess_a.state in (SessionState.CLOSING, SessionState.SETTLING)
        assert order.cancel_requested or order.is_terminal

        # The boundary oracle print settles A and strikes B.
        h.engine._on_k_capture(
            KCapture(
                WS + 300,
                k=100_100.0,
                oracle_ts_ms=(WS + 300) * 1000 + 50,
                recv_wall_ns=h.clock.wall_ns(),
            )
        )
        await asyncio.sleep(0.05)
        assert sess_a.state is SessionState.DONE
        assert sess_a.outcome is not None

        sess_b = h.engine.current
        assert sess_b is not None and sess_b.meta.window_start == WS + 300
        assert sess_b.k == pytest.approx(100_100.0)
        assert sess_b is not sess_a
        assert not sess_b.open_order_ids  # nothing leaked (spec §3)
        assert sess_b.stake_used_usdc == 0.0
    finally:
        h.close()


async def test_late_fill_routes_to_owning_session(tmp_path: Path) -> None:
    """A fill that lands after the boundary must hit session A's book, never B's."""
    h = await make_active_harness(tmp_path)
    try:
        from .test_order_fsm import make_intent

        order = h.om.draft(make_intent(), decision_mono_ns=h.clock.mono_ns())
        await h.om.submit(order)  # SENT/ACKED, shadow arrival still pending

        # Boundary passes before any fill.
        h.clock.set(int(301e9), int((WS + 301) * 1e9))
        await h.engine.manage_sessions(h.clock.wall_s())

        # Late fill arrives via the (already-recorded) order's session slug.
        late = Fill(
            order_id=order.exchange_id or order.client_id,
            token_id=UP,
            side="BUY",
            price=0.94,
            size=3.0,
            fee_shares=0.05,
            fee_usdc=0.0,
            exchange_ts_ms=(WS + 300) * 1000 + 200,
            recv_mono_ns=h.clock.mono_ns(),
            trade_id="late-1",
        )
        h.om.apply_fill(late)
        assert SLUG_A in h.ledger.sessions
        assert h.ledger.sessions[SLUG_A].n_fills == 1
        assert f"btc-updown-5m-{WS + 300}" not in h.ledger.sessions
    finally:
        h.close()


async def test_losing_windows_feed_cooldown(tmp_path: Path) -> None:
    h = await make_active_harness(tmp_path)
    try:
        cfg = h.engine.cfg
        for i in range(cfg.risk.max_consecutive_losses):
            h.risk.record_window_result(WS + i * 300, -2.0)
        assert h.risk.cooldown_until_window > WS
    finally:
        h.close()
