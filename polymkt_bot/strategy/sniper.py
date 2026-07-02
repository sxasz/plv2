"""Mode A — late-window sniper (spec §2.3, primary strategy).

Active in [T−arm_seconds, T−min_seconds]. Fires an FAK (IOC) buy when the
model is confident enough (P ≥ p_min_snipe), the ask is cheap enough
(≤ max_snipe_price) and the net edge after fees and slippage buffer clears
min_edge_snipe. Sized to visible depth × participation, capped by stake.
Default plan: hold to resolution — with τ in seconds, exit costs would eat
the edge. The tie-band no-trade zone is enforced by the risk gates.
"""

from __future__ import annotations

from ..config import ModeAConfig, SizingConfig
from ..markets.session import Phase
from ..model.fair_value import edge_for_taker_buy
from ..types import OrderIntent, Outcome, TimeInForce
from .base import DecisionContext, Strategy, StrategyDecision


class SniperStrategy(Strategy):
    name = "mode_a_sniper"

    def __init__(self, cfg: ModeAConfig, sizing: SizingConfig) -> None:
        self.cfg = cfg
        self.sizing = sizing
        self._fired_windows: set[int] = set()

    def evaluate(self, ctx: DecisionContext) -> StrategyDecision:
        nothing = StrategyDecision()
        if not self.cfg.enabled or ctx.phase is not Phase.ARMED:
            return nothing
        if ctx.fair is None or ctx.fee_model is None or ctx.k is None:
            return nothing
        ws = ctx.session.meta.window_start
        if ws in self._fired_windows:
            return nothing  # one shot per window; re-entries need explicit design

        p_up = ctx.fair.p_up
        side: Outcome
        if p_up >= self.cfg.p_min_snipe:
            side, p_model, top = "UP", p_up, ctx.up_top
        elif (1.0 - p_up) >= self.cfg.p_min_snipe:
            side, p_model, top = "DOWN", 1.0 - p_up, ctx.down_top
        else:
            return nothing

        if top.ask is None or top.ask_size <= 0.0:
            return nothing
        ask = top.ask
        if ask > self.cfg.max_snipe_price:
            return nothing

        edge = edge_for_taker_buy(
            p_model, ask, ctx.fee_model.fee_per_share(ask), self.cfg.slippage_buffer
        )
        if edge < self.cfg.min_edge_snipe:
            return nothing

        size = self._size(ask, top.ask_size)
        if size <= 0.0:
            return nothing

        self._fired_windows.add(ws)
        if len(self._fired_windows) > 64:
            self._fired_windows = set(sorted(self._fired_windows)[-32:])
        intent = OrderIntent(
            session_slug=ctx.session.slug,
            token_id=ctx.session.meta.token_for(side),
            outcome=side,
            side="BUY",
            price=ask,  # explicit limit at the touch, never unbounded
            size=size,
            tif=TimeInForce.FAK,
            strategy=self.name,
            reason=(
                f"p={p_model:.4f} ask={ask:.3f} "
                f"fee={ctx.fee_model.fee_per_share(ask):.4f} edge={edge:.4f} "
                f"tau={ctx.tau_s:.1f}s dist={ctx.fair.dist:.2f}"
            ),
            model_p=p_model,
            edge_net=edge,
        )
        return StrategyDecision(intents=[intent])

    def _size(self, price: float, visible: float) -> float:
        by_depth = visible * self.sizing.depth_participation
        by_stake = self.sizing.stake_usdc / price
        by_cap = self.sizing.max_stake_per_window / price
        return round(min(by_depth, by_stake, by_cap), 2)

    def on_window_closed(self, session: object) -> None:
        return
