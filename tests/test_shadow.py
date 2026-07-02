"""Shadow exchange honesty (spec §8): RTT-delayed depth-limited taker fills,
tape-confirmed conservative maker fills, no free liquidity anywhere."""

from __future__ import annotations

import pytest

from polymkt_bot.exec.fees import FeeModel
from polymkt_bot.exec.order_manager import ManagedOrder, OrderManager, OrderState
from polymkt_bot.feeds.polymarket_clob_ws import OrderBook, TradePrint
from polymkt_bot.sim.shadow_exchange import RttSampler, ShadowExchange
from polymkt_bot.types import Fill, MarketMeta, TimeInForce

from .conftest import UP


class Harness:
    def __init__(self, meta: MarketMeta) -> None:
        self.book = OrderBook(UP)
        self.book.apply_snapshot(
            [{"price": "0.90", "size": "100"}],
            [{"price": "0.94", "size": "50"}, {"price": "0.95", "size": "100"}],
            "h",
            0,
        )
        self.now = 1_000_000_000  # 1s in ns
        fee_model = FeeModel(meta)
        rtt = RttSampler(80.0, 200.0, seed=42)
        rtt.sample_ms = lambda: 50.0  # type: ignore[method-assign] — deterministic tests
        self.shadow = ShadowExchange(
            books=lambda tid: self.book if tid == UP else None,
            fee_models=lambda tid: fee_model,
            rtt=rtt,
            now_mono_ns=lambda: self.now,
        )
        self.om = OrderManager(self.shadow)
        self.fills: list[Fill] = []
        self.shadow.on_fill = self._on_fill
        self.shadow.on_cancel_confirmed = self.om.confirm_canceled
        self.shadow.on_expired = self.om.expire_unfilled

    def _on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        self.om.apply_fill(fill)

    def advance_ms(self, ms: float) -> None:
        self.now += int(ms * 1e6)
        self.shadow.process_due(self.now)

    def order(
        self,
        side: str = "BUY",
        price: float = 0.94,
        size: float = 10.0,
        tif: TimeInForce = TimeInForce.FAK,
    ) -> ManagedOrder:
        o = ManagedOrder(
            session_slug="s",
            token_id=UP,
            side=side,  # type: ignore[arg-type]
            price=price,
            size=size,
            tif=tif,
            strategy="t",
        )
        self.om.orders[o.client_id] = o
        return o


async def test_no_fill_before_rtt_elapses(meta: MarketMeta) -> None:
    h = Harness(meta)
    o = h.order()
    await h.om.submit(o)
    h.advance_ms(49.0)  # RTT is 50ms — too early
    assert not h.fills
    h.advance_ms(2.0)
    assert h.fills  # now it lands
    assert o.state is OrderState.FILLED


async def test_taker_fill_limited_to_visible_depth(meta: MarketMeta) -> None:
    """100 wanted at 0.94, but only 50 visible → 50 filled, remainder killed."""
    h = Harness(meta)
    o = h.order(price=0.94, size=100.0)
    await h.om.submit(o)
    h.advance_ms(60.0)
    assert sum(f.size for f in h.fills) == pytest.approx(50.0)
    assert o.state is OrderState.EXPIRED  # FAK remainder killed after partial
    assert o.filled_size == pytest.approx(50.0)


async def test_taker_walks_levels_within_limit(meta: MarketMeta) -> None:
    h = Harness(meta)
    o = h.order(price=0.95, size=100.0)  # limit covers both ask levels
    await h.om.submit(o)
    h.advance_ms(60.0)
    assert sum(f.size for f in h.fills) == pytest.approx(100.0)
    prices = sorted({f.price for f in h.fills})
    assert prices == [0.94, 0.95]
    assert o.state is OrderState.FILLED


async def test_fok_all_or_nothing(meta: MarketMeta) -> None:
    h = Harness(meta)
    o = h.order(price=0.94, size=60.0, tif=TimeInForce.FOK)  # only 50 visible
    await h.om.submit(o)
    h.advance_ms(60.0)
    assert not h.fills
    assert o.state is OrderState.EXPIRED


async def test_buy_fee_charged_in_shares(meta: MarketMeta) -> None:
    h = Harness(meta)
    o = h.order(price=0.94, size=10.0)
    await h.om.submit(o)
    h.advance_ms(60.0)
    fill = h.fills[0]
    fee_model = FeeModel(meta)
    assert fill.fee_shares == pytest.approx(fee_model.buy_fee_shares(0.94, 10.0))
    assert fill.fee_usdc == 0.0
    # Net share delta reflects the shares withheld as fee.
    assert fill.shares_delta == pytest.approx(10.0 - fill.fee_shares)


async def test_dirty_book_kills_order(meta: MarketMeta) -> None:
    h = Harness(meta)
    h.book.dirty = True
    o = h.order()
    await h.om.submit(o)
    h.advance_ms(60.0)
    assert not h.fills
    assert o.state is OrderState.EXPIRED


async def test_maker_fills_only_from_tape_conservative_queue(meta: MarketMeta) -> None:
    """GTC sell at 0.95: 100 shares visible ahead. Tape must trade >100 through
    0.95 before we see anything (rule 3: last in queue)."""
    h = Harness(meta)
    o = h.order(side="SELL", price=0.95, size=10.0, tif=TimeInForce.GTC)
    await h.om.submit(o)
    h.advance_ms(60.0)  # order now resting
    assert not h.fills

    def tape(price: float, size: float, at_ms: float) -> None:
        h.shadow.on_trade(TradePrint(UP, price, size, "BUY", 0, h.now))
        h.advance_ms(at_ms)

    tape(0.94, 500.0, 1.0)  # below our price: irrelevant
    assert not h.fills
    tape(0.95, 60.0, 1.0)  # eats queue ahead (60 of 100)
    assert not h.fills
    tape(0.95, 45.0, 1.0)  # 40 finishes the queue, 5 reach us
    assert sum(f.size for f in h.fills) == pytest.approx(5.0)
    assert o.state is OrderState.PARTIAL
    tape(0.96, 10.0, 1.0)  # trades through: 5 more fill us fully
    assert o.state is OrderState.FILLED
    # Maker pays zero fees (FACTS 2.5).
    assert all(f.fee_usdc == 0.0 and f.fee_shares == 0.0 for f in h.fills)


async def test_maker_priced_through_touch_takes_instead(meta: MarketMeta) -> None:
    """No post-only on Polymarket: a 'maker' sell below the bid TAKES."""
    h = Harness(meta)
    o = h.order(side="SELL", price=0.89, size=10.0, tif=TimeInForce.GTC)
    await h.om.submit(o)
    h.advance_ms(60.0)
    assert h.fills and h.fills[0].price == pytest.approx(0.90)  # crossed at bid
    assert h.fills[0].fee_usdc > 0.0  # and paid taker fees in collateral


async def test_cancel_race_tape_fill_wins(meta: MarketMeta) -> None:
    h = Harness(meta)
    o = h.order(side="SELL", price=0.95, size=5.0, tif=TimeInForce.GTC)
    await h.om.submit(o)
    h.advance_ms(60.0)
    await h.om.request_cancel(o)  # cancel in flight (50ms)
    h.shadow.on_trade(TradePrint(UP, 0.96, 200.0, "BUY", 0, h.now))  # fill first
    h.advance_ms(60.0)  # cancel arrives after
    assert o.state is OrderState.FILLED  # the fill won the race
