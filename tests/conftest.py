"""Shared fixtures: canonical market meta, sessions, books, adapters."""

from __future__ import annotations

import pytest

from polymkt_bot.config import Config
from polymkt_bot.exec.fees import FeeModel
from polymkt_bot.exec.order_manager import ManagedOrder, PlaceResult
from polymkt_bot.markets.session import MarketSession
from polymkt_bot.model.fair_value import FairValue
from polymkt_bot.types import BookTop, MarketMeta, PriceLevel

WS = 1_782_936_300  # divisible by 300
UP = "111111"
DOWN = "222222"


@pytest.fixture
def meta() -> MarketMeta:
    return MarketMeta(
        window_start=WS,
        window_close=WS + 300,
        slug=f"btc-updown-5m-{WS}",
        condition_id="0xcond",
        up_token=UP,
        down_token=DOWN,
        tick_size=0.001,
        min_order_size=5.0,
        neg_risk=False,
        fee_rate_bps=720,  # ≈ published crypto peak $1.80/100 shares (test value)
        fees_known=True,
    )


@pytest.fixture
def session(meta: MarketMeta) -> MarketSession:
    return MarketSession(meta=meta, warmup_seconds=30, arm_seconds=20, min_seconds=2)


@pytest.fixture
def fee_model(meta: MarketMeta) -> FeeModel:
    return FeeModel(meta)


@pytest.fixture
def cfg() -> Config:
    return Config()


def make_top(
    token: str = UP,
    bid: float | None = 0.90,
    bid_size: float = 200.0,
    ask: float | None = 0.92,
    ask_size: float = 200.0,
) -> BookTop:
    return BookTop(
        token_id=token,
        bid=bid,
        bid_size=bid_size,
        ask=ask,
        ask_size=ask_size,
        bids_top3=[PriceLevel(bid, bid_size)] if bid is not None else [],
        asks_top3=[PriceLevel(ask, ask_size)] if ask is not None else [],
    )


def make_fair(p_up: float = 0.97, in_tie_band: bool = False) -> FairValue:
    return FairValue(
        s_hat=100_500.0,
        p_up=p_up,
        sigma_price_per_s=10.0,
        basis_std=5.0,
        denom=50.0,
        dist=200.0,
        in_tie_band=in_tie_band,
    )


class FakeAdapter:
    """Scriptable ExchangeAdapter for FSM tests."""

    def __init__(self) -> None:
        self.placed: list[ManagedOrder] = []
        self.canceled: list[ManagedOrder] = []
        self.cancel_all_calls: list[str] = []
        self.place_result = PlaceResult(ok=True, exchange_id="x1")

    async def sign(self, order: ManagedOrder) -> None:
        return

    async def place(self, order: ManagedOrder) -> PlaceResult:
        self.placed.append(order)
        res = self.place_result
        if res.ok and not res.exchange_id:
            res = PlaceResult(ok=True, exchange_id=f"x-{order.client_id}")
        return res

    async def cancel(self, order: ManagedOrder) -> bool:
        self.canceled.append(order)
        return True

    async def cancel_all(self, session_slug: str) -> None:
        self.cancel_all_calls.append(session_slug)
