"""Shared value types used across feeds, model, execution and simulation."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

Side = Literal["BUY", "SELL"]
Outcome = Literal["UP", "DOWN"]


class TimeInForce(StrEnum):
    """CLOB order types (FACTS.md #3.3). FAK is the IOC-equivalent."""

    GTC = "GTC"
    FOK = "FOK"
    GTD = "GTD"
    FAK = "FAK"


@dataclass(slots=True)
class PriceLevel:
    price: float
    size: float


@dataclass(slots=True)
class BookTop:
    """Snapshot of one token's book as seen at decision time."""

    token_id: str
    bid: float | None
    bid_size: float
    ask: float | None
    ask_size: float
    bids_top3: list[PriceLevel] = field(default_factory=list)
    asks_top3: list[PriceLevel] = field(default_factory=list)
    updated_mono_ns: int = 0


@dataclass(frozen=True, slots=True)
class MarketMeta:
    """Per-window market metadata resolved via Gamma/CLOB (never hardcoded)."""

    window_start: int
    window_close: int
    slug: str
    condition_id: str
    up_token: str
    down_token: str
    tick_size: float
    min_order_size: float
    neg_risk: bool
    fee_rate_bps: int  # from CLOB /fee-rate (FACTS.md #2.3)
    fees_known: bool  # explicit: trading is blocked when False

    def token_for(self, outcome: Outcome) -> str:
        return self.up_token if outcome == "UP" else self.down_token

    def outcome_for(self, token_id: str) -> Outcome:
        if token_id == self.up_token:
            return "UP"
        if token_id == self.down_token:
            return "DOWN"
        raise KeyError(f"token {token_id} not in market {self.slug}")


_intent_counter = itertools.count(1)


@dataclass(slots=True)
class OrderIntent:
    """What a strategy wants to do; risk gates run before it becomes an order."""

    session_slug: str
    token_id: str
    outcome: Outcome
    side: Side
    price: float
    size: float
    tif: TimeInForce
    strategy: str
    reason: str
    model_p: float  # model probability for the outcome bought/sold
    edge_net: float  # expected edge after fees + slippage buffer
    intent_id: int = field(default_factory=lambda: next(_intent_counter))


@dataclass(slots=True)
class Fill:
    """A confirmed execution (user-channel trade event or shadow equivalent).

    Fee handling per FACTS.md #2.4: on BUY the fee is charged in shares
    (fee_shares > 0, you receive size - fee_shares), on SELL in collateral
    (fee_usdc > 0). Always populated from the actual fill event, never from
    locally computed expectations.
    """

    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    fee_shares: float
    fee_usdc: float
    exchange_ts_ms: int
    recv_mono_ns: int
    trade_id: str = ""

    @property
    def shares_delta(self) -> float:
        """Signed change to share inventory."""
        return (self.size - self.fee_shares) if self.side == "BUY" else -self.size

    @property
    def usdc_delta(self) -> float:
        """Signed change to collateral balance."""
        if self.side == "BUY":
            return -self.price * self.size
        return self.price * self.size - self.fee_usdc
