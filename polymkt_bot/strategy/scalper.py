"""Mode B — mid-window lag scalp (spec §2.3, secondary; off until shadow-proven).

Trigger: a Binance impulse (|return| over impulse_window_ms ≥ threshold)
while the Polymarket top-of-book on the impulse side has NOT repriced since
before the impulse. Entry: FAK at the stale ask. Exit: maker sell at
model-fair + maker_exit_offset (makers pay zero — FACTS.md #2.5) with a time
stop; on model flip beyond scalp_stop_edge, cross out at the bid (paying the
taker fee — the round-trip math is spelled out in the decision reason); with
τ small and P favorable, convert to hold-to-resolution.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from ..config import ModeBConfig, SizingConfig
from ..exec.order_manager import ManagedOrder
from ..markets.session import MarketSession, Phase
from ..model.fair_value import edge_for_taker_buy
from ..types import BookTop, Fill, OrderIntent, Outcome, TimeInForce
from .base import DecisionContext, Strategy, StrategyDecision

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Snapshot:
    mono_ns: int
    binance_mid: float
    up_ask: float | None
    down_ask: float | None


@dataclass(slots=True)
class _OpenScalp:
    outcome: Outcome
    token_id: str
    entry_price: float
    shares: float
    entry_fee_usd: float
    opened_at_s: float
    exit_client_id: str = ""
    hold_to_resolution: bool = False


class ScalperStrategy(Strategy):
    name = "mode_b_scalp"

    def __init__(self, cfg: ModeBConfig, sizing: SizingConfig, tick_size_hint: float = 0.01):
        self.cfg = cfg
        self.sizing = sizing
        self.tick_size_hint = tick_size_hint
        self._history: deque[_Snapshot] = deque(maxlen=512)
        self._open: _OpenScalp | None = None
        self._pending_entry_client_id: str = ""

    # -- main tick -------------------------------------------------------------
    def evaluate(self, ctx: DecisionContext) -> StrategyDecision:
        nothing = StrategyDecision()
        if not self.cfg.enabled:
            return nothing
        self._record(ctx)
        if self._open is not None:
            return self._manage_exit(ctx)
        if ctx.phase is not Phase.MID:
            return nothing
        if self._pending_entry_client_id:
            return nothing  # entry in flight
        if ctx.fair is None or ctx.fee_model is None or ctx.k is None:
            return nothing
        return self._maybe_enter(ctx)

    def _record(self, ctx: DecisionContext) -> None:
        self._history.append(
            _Snapshot(
                mono_ns=int(ctx.now_s * 1e9),
                binance_mid=ctx.binance_mid,
                up_ask=ctx.up_top.ask,
                down_ask=ctx.down_top.ask,
            )
        )

    # -- entry -------------------------------------------------------------------
    def _maybe_enter(self, ctx: DecisionContext) -> StrategyDecision:
        nothing = StrategyDecision()
        ref = self._snapshot_before(ctx.now_s - self.cfg.impulse_window_ms / 1000.0)
        if ref is None or ref.binance_mid <= 0.0 or ctx.binance_mid <= 0.0:
            return nothing
        ret = (ctx.binance_mid - ref.binance_mid) / ref.binance_mid
        if abs(ret) < self.cfg.impulse_threshold:
            return nothing

        outcome: Outcome = "UP" if ret > 0 else "DOWN"
        top: BookTop = ctx.up_top if outcome == "UP" else ctx.down_top
        ref_ask = ref.up_ask if outcome == "UP" else ref.down_ask
        if top.ask is None or ref_ask is None or top.ask_size <= 0.0:
            return nothing
        if abs(top.ask - ref_ask) > 1e-12:
            return nothing  # book already repriced — the lag is gone

        assert ctx.fair is not None and ctx.fee_model is not None
        p_model = ctx.fair.p_up if outcome == "UP" else 1.0 - ctx.fair.p_up
        fee_in = ctx.fee_model.fee_per_share(top.ask)
        # Round trip must be viable: entry fee now + maker exit (zero fee)
        # later; require entry edge alone to clear min_edge_scalp.
        edge = edge_for_taker_buy(p_model, top.ask, fee_in, slippage_buffer=0.0)
        if edge < self.cfg.min_edge_scalp:
            return nothing

        size = round(
            min(
                top.ask_size * self.sizing.depth_participation,
                self.sizing.stake_usdc / top.ask,
            ),
            2,
        )
        if size <= 0.0:
            return nothing
        intent = OrderIntent(
            session_slug=ctx.session.slug,
            token_id=ctx.session.meta.token_for(outcome),
            outcome=outcome,
            side="BUY",
            price=top.ask,
            size=size,
            tif=TimeInForce.FAK,
            strategy=self.name,
            reason=(
                f"impulse ret={ret:+.5f} over {self.cfg.impulse_window_ms:.0f}ms, "
                f"stale ask={top.ask:.3f} (unchanged), p={p_model:.4f}, "
                f"fee_in={fee_in:.4f}, edge={edge:.4f}"
            ),
            model_p=p_model,
            edge_net=edge,
        )
        return StrategyDecision(intents=[intent])

    def _snapshot_before(self, cutoff_s: float) -> _Snapshot | None:
        cutoff_ns = int(cutoff_s * 1e9)
        result = None
        for snap in self._history:
            if snap.mono_ns <= cutoff_ns:
                result = snap
            else:
                break
        return result

    # -- exit management -----------------------------------------------------------
    def _manage_exit(self, ctx: DecisionContext) -> StrategyDecision:
        pos = self._open
        assert pos is not None
        nothing = StrategyDecision()
        if pos.hold_to_resolution:
            return nothing
        if ctx.fair is None or ctx.fee_model is None:
            return nothing
        p_model = ctx.fair.p_up if pos.outcome == "UP" else 1.0 - ctx.fair.p_up
        top = ctx.up_top if pos.outcome == "UP" else ctx.down_top
        held_for = ctx.now_s - pos.opened_at_s

        # Convert to hold when late and favorable: exiting would cost more
        # than the remaining resolution risk.
        if ctx.tau_s <= ctx.session.arm_seconds and p_model >= 0.9:
            pos.hold_to_resolution = True
            cancels = [pos.exit_client_id] if pos.exit_client_id else []
            pos.exit_client_id = ""
            return StrategyDecision(cancel_client_ids=cancels)

        # Model flipped hard: cross out at the bid, paying the taker fee.
        flip_edge = p_model - pos.entry_price
        if flip_edge <= self.cfg.scalp_stop_edge or held_for >= self.cfg.scalp_timeout_s:
            if top.bid is None or top.bid <= 0.0:
                return nothing
            fee_out = ctx.fee_model.fee_per_share(top.bid)
            cancels = [pos.exit_client_id] if pos.exit_client_id else []
            pos.exit_client_id = ""
            intent = OrderIntent(
                session_slug=ctx.session.slug,
                token_id=pos.token_id,
                outcome=pos.outcome,
                side="SELL",
                price=top.bid,
                size=pos.shares,
                tif=TimeInForce.FAK,
                strategy=self.name,
                reason=(
                    f"cross-out: p={p_model:.4f} entry={pos.entry_price:.3f} "
                    f"bid={top.bid:.3f} fee_out={fee_out:.4f} held={held_for:.1f}s; "
                    f"round-trip pnl/share ≈ {top.bid - pos.entry_price - fee_out:+.4f} "
                    f"(entry fee {pos.entry_fee_usd / max(pos.shares, 1e-9):.4f} already sunk)"
                ),
                model_p=p_model,
                edge_net=top.bid - pos.entry_price - fee_out,
            )
            return StrategyDecision(intents=[intent], cancel_client_ids=cancels)

        # Otherwise: keep a maker exit working at fair + offset (zero fee).
        if not pos.exit_client_id:
            tick = ctx.session.meta.tick_size or self.tick_size_hint
            raw_px = p_model + self.cfg.maker_exit_offset
            px = min(round(raw_px / tick) * tick, 0.99)
            # Must be passive: never price at or below the current bid.
            if top.bid is not None and px <= top.bid:
                px = min(top.bid + tick, 0.99)
            intent = OrderIntent(
                session_slug=ctx.session.slug,
                token_id=pos.token_id,
                outcome=pos.outcome,
                side="SELL",
                price=round(px, 6),
                size=pos.shares,
                tif=TimeInForce.GTC,
                strategy=self.name,
                reason=(
                    f"maker exit at fair+offset: p={p_model:.4f} px={px:.3f} "
                    f"maker_fee=0 (rebate ignored)"
                ),
                model_p=p_model,
                edge_net=px - pos.entry_price,
            )
            return StrategyDecision(intents=[intent])
        return nothing

    # -- fill/lifecycle hooks --------------------------------------------------------
    def on_fill(self, order: ManagedOrder, fill: Fill) -> None:
        if order.strategy != self.name:
            return
        if order.side == "BUY":
            if self._open is None:
                self._open = _OpenScalp(
                    outcome="UP",  # corrected below via token compare in engine wiring
                    token_id=order.token_id,
                    entry_price=fill.price,
                    shares=0.0,
                    entry_fee_usd=0.0,
                    opened_at_s=fill.exchange_ts_ms / 1000.0,
                )
            self._open.shares += fill.shares_delta
            self._open.entry_fee_usd += fill.fee_usdc + fill.fee_shares * fill.price
        elif order.side == "SELL" and self._open is not None:
            self._open.shares -= fill.size
            if self._open.shares <= 1e-9:
                self._open = None

    def register_entry_order(self, order: ManagedOrder, outcome: Outcome) -> None:
        """Engine tells us which outcome the entry order is for."""
        self._pending_entry_client_id = order.client_id
        if self._open is not None:
            self._open.outcome = outcome

    def register_exit_order(self, order: ManagedOrder) -> None:
        if self._open is not None and order.side == "SELL" and order.tif is TimeInForce.GTC:
            self._open.exit_client_id = order.client_id

    def note_entry_terminal(self, order: ManagedOrder) -> None:
        if order.client_id == self._pending_entry_client_id:
            self._pending_entry_client_id = ""

    def on_window_closed(self, session: MarketSession) -> None:
        # Any residual position is now hold-to-resolution by construction;
        # per-window state must not leak into the next session.
        self._open = None
        self._pending_entry_client_id = ""
        self._history.clear()
