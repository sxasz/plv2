"""Mode C — passive maker module (scaffold, spec §2.3 optional / M4).

Structurally the sounder business on these markets: quote both sides around
model fair, pay zero fees, earn rebates funded by the takers. This scaffold
defines the quoting logic and inventory caps but stays disabled until M2's
Edge Realization Report motivates activating and tuning it.

Known gaps before this can go live (tracked for M4):
- Polymarket has no post-only flag: a quote priced through the touch will
  TAKE and pay taker fees. Quotes must therefore always be priced strictly
  inside the touch, and re-priced (cancel/replace) as the book moves.
- Queue-position modeling and adverse-selection stats need M1 tape data.
- Rebate income is deliberately NOT modeled (conservative).
"""

from __future__ import annotations

import logging

from ..config import ModeCConfig, SizingConfig
from ..markets.session import MarketSession, Phase
from ..types import OrderIntent, TimeInForce
from .base import DecisionContext, Strategy, StrategyDecision

log = logging.getLogger(__name__)


class MakerStrategy(Strategy):
    name = "mode_c_maker"

    def __init__(self, cfg: ModeCConfig, sizing: SizingConfig) -> None:
        self.cfg = cfg
        self.sizing = sizing
        self._live_quotes: dict[str, str] = {}  # "UP:BUY" → client_id

    def evaluate(self, ctx: DecisionContext) -> StrategyDecision:
        if not self.cfg.enabled:
            return StrategyDecision()
        if ctx.phase not in (Phase.MID,):
            # Never quote into the armed/lockout endgame: adverse selection
            # against snipers is exactly the losing side of this market.
            return self._pull_all()
        if ctx.fair is None or ctx.fee_model is None:
            return self._pull_all()

        # Scaffold: quoting disabled pending M4 tuning. The intent plumbing
        # is exercised by tests; enabling requires explicit config + sign-off.
        log.debug("maker scaffold tick", extra={"ctx": {"p_up": ctx.fair.p_up}})
        return StrategyDecision()

    def _pull_all(self) -> StrategyDecision:
        cancels = list(self._live_quotes.values())
        self._live_quotes.clear()
        return StrategyDecision(cancel_client_ids=cancels)

    def quote_prices(self, p_up: float, tick: float) -> tuple[float, float]:
        """Bid/ask for the UP token around fair, clamped to be passive-safe."""
        bid = max(round((p_up - self.cfg.half_spread) / tick) * tick, tick)
        ask = min(round((p_up + self.cfg.half_spread) / tick) * tick, 1.0 - tick)
        return bid, ask

    def _unused_intent_shape(self, ctx: DecisionContext) -> OrderIntent:
        """Reference for the M4 implementation (kept typed & compiled)."""
        raise NotImplementedError

    def on_window_closed(self, session: MarketSession) -> None:
        self._live_quotes.clear()

    _ = TimeInForce.GTC  # quotes will be GTC cancel/replace
