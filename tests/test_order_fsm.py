"""Order FSM: transitions, ACK≠fill, cancel race, post-cancel fill (spec §10)."""

from __future__ import annotations

import pytest

from polymkt_bot.exec.order_manager import (
    IllegalTransition,
    ManagedOrder,
    OrderManager,
    OrderState,
    PlaceResult,
)
from polymkt_bot.types import Fill, OrderIntent, TimeInForce

from .conftest import UP, FakeAdapter


def make_intent(size: float = 10.0, price: float = 0.94) -> OrderIntent:
    return OrderIntent(
        session_slug="btc-updown-5m-1782936300",
        token_id=UP,
        outcome="UP",
        side="BUY",
        price=price,
        size=size,
        tif=TimeInForce.FAK,
        strategy="test",
        reason="test",
        model_p=0.97,
        edge_net=0.02,
    )


def make_fill(order: ManagedOrder, size: float, trade_id: str = "t1") -> Fill:
    return Fill(
        order_id=order.exchange_id or order.client_id,
        token_id=order.token_id,
        side=order.side,
        price=order.price,
        size=size,
        fee_shares=0.1,
        fee_usdc=0.0,
        exchange_ts_ms=1,
        recv_mono_ns=2,
        trade_id=trade_id,
    )


async def test_happy_path_ack_is_not_a_fill() -> None:
    om = OrderManager(FakeAdapter())
    order = om.draft(make_intent(), decision_mono_ns=1)
    assert order.state is OrderState.DRAFT
    assert await om.submit(order)
    # ACKED, but NOT filled — spec §3.
    assert order.state is OrderState.ACKED
    assert order.filled_size == 0.0

    om.apply_fill(make_fill(order, 4.0, "t1"))
    assert order.state is OrderState.PARTIAL
    om.apply_fill(make_fill(order, 6.0, "t2"))
    assert order.state is OrderState.FILLED
    assert order.filled_size == pytest.approx(10.0)


async def test_rejected_order() -> None:
    adapter = FakeAdapter()
    adapter.place_result = PlaceResult(ok=False, error="not enough balance")
    om = OrderManager(adapter)
    order = om.draft(make_intent(), decision_mono_ns=1)
    assert not await om.submit(order)
    assert order.state is OrderState.REJECTED


def test_illegal_transitions_raise() -> None:
    order = ManagedOrder(
        session_slug="s",
        token_id=UP,
        side="BUY",
        price=0.9,
        size=1.0,
        tif=TimeInForce.FAK,
        strategy="t",
    )
    with pytest.raises(IllegalTransition):
        order.transition(OrderState.ACKED)  # DRAFT → ACKED forbidden
    order.transition(OrderState.SIGNED)
    with pytest.raises(IllegalTransition):
        order.transition(OrderState.FILLED)  # SIGNED → FILLED forbidden


async def test_cancel_race_fill_wins() -> None:
    """Cancel requested, but the fill lands first: order is FILLED, and the
    late cancel confirmation must not clobber it."""
    om = OrderManager(FakeAdapter())
    order = om.draft(make_intent(), decision_mono_ns=1)
    await om.submit(order)
    await om.request_cancel(order)
    assert order.cancel_requested
    assert order.state is OrderState.ACKED  # request ≠ canceled (spec §3)

    om.apply_fill(make_fill(order, 10.0))
    assert order.state is OrderState.FILLED

    om.confirm_canceled(order)  # late confirmation loses the race
    assert order.state is OrderState.FILLED


async def test_fill_after_confirmed_cancel_hits_accounting() -> None:
    """The nastier race: exchange confirms cancel, then a fill arrives anyway.
    State stays CANCELED but the fill must reach accounting."""
    applied: list[Fill] = []
    om = OrderManager(FakeAdapter(), on_fill_applied=lambda o, f: applied.append(f))
    order = om.draft(make_intent(), decision_mono_ns=1)
    await om.submit(order)
    await om.request_cancel(order)
    om.confirm_canceled(order)
    assert order.state is OrderState.CANCELED

    om.apply_fill(make_fill(order, 3.0))
    assert order.state is OrderState.CANCELED  # terminal state preserved
    assert order.filled_size == pytest.approx(3.0)  # …but the fill is recorded
    assert len(applied) == 1  # …and accounting saw it


async def test_duplicate_trade_ids_deduplicated() -> None:
    om = OrderManager(FakeAdapter())
    order = om.draft(make_intent(), decision_mono_ns=1)
    await om.submit(order)
    om.apply_fill(make_fill(order, 4.0, "t1"))
    om.apply_fill(make_fill(order, 4.0, "t1"))  # MINED/CONFIRMED re-delivery
    assert order.filled_size == pytest.approx(4.0)


async def test_fill_for_unknown_order_is_incident_not_crash() -> None:
    om = OrderManager(FakeAdapter())
    fill = Fill(
        order_id="ghost",
        token_id=UP,
        side="BUY",
        price=0.9,
        size=1.0,
        fee_shares=0,
        fee_usdc=0,
        exchange_ts_ms=0,
        recv_mono_ns=0,
        trade_id="g1",
    )
    assert om.apply_fill(fill) is None


async def test_latency_samples_present() -> None:
    om = OrderManager(FakeAdapter())
    order = om.draft(make_intent(), decision_mono_ns=1)
    await om.submit(order)
    hops = om.latency_samples(order)
    assert "decision_to_sent" in hops and "sent_to_ack" in hops
    assert hops["sent_to_ack"] >= 0.0
