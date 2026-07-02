"""Strategy interface and the decision context every strategy sees.

The engine builds one DecisionContext per evaluation tick and logs it in
full (spec §7): inputs, gates and the resulting action must let any trade be
replayed and audited after the fact.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from ..exec.fees import FeeModel
from ..exec.order_manager import ManagedOrder
from ..markets.session import MarketSession, Phase
from ..model.fair_value import FairValue
from ..types import BookTop, Fill, OrderIntent


@dataclass(slots=True)
class DecisionContext:
    session: MarketSession
    now_s: float
    tau_s: float
    phase: Phase
    k: float | None
    s_cl: float
    binance_mid: float
    fair: FairValue | None
    up_top: BookTop
    down_top: BookTop
    fee_model: FeeModel | None
    chainlink_age_ms: float
    binance_age_ms: float
    book_dirty: bool


@dataclass(slots=True)
class StrategyDecision:
    """What a strategy wants this tick: new orders and/or cancels."""

    intents: list[OrderIntent] = field(default_factory=list)
    cancel_client_ids: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.intents and not self.cancel_client_ids


class Strategy(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def evaluate(self, ctx: DecisionContext) -> StrategyDecision:
        """Return desired actions (risk-gated downstream). Must not mutate ctx."""

    def on_fill(self, order: ManagedOrder, fill: Fill) -> None:  # noqa: B027
        """Notification of a confirmed fill on an order this strategy created."""

    def on_window_closed(self, session: MarketSession) -> None:  # noqa: B027
        """Hook for per-window state reset."""
