"""Risk manager: every gate from spec §6 as a hard, auditable check.

`check_entry` returns the full list of failed gates (for the decision log —
we record *why* we didn't trade, not just that we didn't). Any failure means
no trade. The kill switch cancels everything, flattens what it can, and
stops the engine.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..config import Config
from ..markets.session import MarketSession, Phase
from ..types import BookTop, OrderIntent

log = logging.getLogger(__name__)


@dataclass(slots=True)
class GateContext:
    """Everything the gates need to judge an entry intent."""

    session: MarketSession
    now_s: float
    book_top: BookTop  # book for the token being bought
    book_dirty: bool
    chainlink_age_ms: float
    binance_age_ms: float
    ntp_offset_ms: float | None  # None = never measured
    in_tie_band: bool
    open_exposure_usdc: float
    daily_realized_pnl_usdc: float


@dataclass(slots=True)
class GateResult:
    allowed: bool
    failed: list[str] = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.killed = False
        self.kill_reason = ""
        self.consecutive_losses = 0
        self.cooldown_until_window: int = 0
        self._on_kill: Callable[[str], Awaitable[None]] | None = None

    def set_kill_handler(self, handler: Callable[[str], Awaitable[None]]) -> None:
        self._on_kill = handler

    # -- outcome tracking -------------------------------------------------------
    def record_window_result(self, window_start: int, realized_pnl_usdc: float) -> None:
        if realized_pnl_usdc < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.risk.max_consecutive_losses:
                self.cooldown_until_window = window_start + self.cfg.risk.cooldown_windows * (
                    self.cfg.window.length_s
                )
                log.warning(
                    "cooldown engaged",
                    extra={
                        "ctx": {
                            "until_window": self.cooldown_until_window,
                            "losses": self.consecutive_losses,
                        }
                    },
                )
                self.consecutive_losses = 0
        elif realized_pnl_usdc > 0:
            self.consecutive_losses = 0

    async def check_daily_loss(self, daily_realized_pnl_usdc: float) -> None:
        if daily_realized_pnl_usdc <= -self.cfg.risk.max_daily_loss_usdc:
            await self.kill(f"daily loss limit: {daily_realized_pnl_usdc:.2f} USDC")

    async def kill(self, reason: str) -> None:
        if self.killed:
            return
        self.killed = True
        self.kill_reason = reason
        log.critical("KILL SWITCH", extra={"ctx": {"reason": reason}})
        if self._on_kill is not None:
            await self._on_kill(reason)

    # -- entry gates -------------------------------------------------------------
    def check_entry(self, intent: OrderIntent, ctx: GateContext) -> GateResult:
        c = self.cfg
        failed: list[str] = []

        def gate(ok: bool, name: str) -> None:
            if not ok:
                failed.append(name)

        gate(not self.killed, "killed")
        gate(ctx.session.meta.window_start >= self.cooldown_until_window, "cooldown")

        phase = ctx.session.phase(ctx.now_s)
        gate(phase in (Phase.MID, Phase.ARMED), f"phase:{phase.value}")
        gate(ctx.session.k is not None, "no_price_to_beat")
        gate(ctx.session.meta.fees_known, "fees_unknown")

        # Freshness (spec §6): stale oracle, stale Binance or dirty book = no trade.
        gate(ctx.chainlink_age_ms <= c.risk.max_cl_age_ms, "chainlink_stale")
        gate(ctx.binance_age_ms <= c.risk.max_bn_age_ms, "binance_stale")
        gate(not ctx.book_dirty, "book_dirty")

        # Clock guard.
        gate(
            ctx.ntp_offset_ms is not None and abs(ctx.ntp_offset_ms) <= c.run.max_ntp_offset_ms,
            "clock_unsynced",
        )

        # Tie-zone no-trade band (spec §2.3).
        gate(not ctx.in_tie_band, "tie_band")

        # Price and size caps.
        gate(intent.price <= c.sizing.absolute_max_price, "price_cap")
        stake = intent.price * intent.size
        gate(
            ctx.session.stake_used_usdc + stake <= c.sizing.max_stake_per_window + 1e-9,
            "window_stake_cap",
        )
        gate(
            ctx.open_exposure_usdc + stake <= c.risk.max_open_exposure_usdc + 1e-9,
            "exposure_cap",
        )
        gate(
            ctx.daily_realized_pnl_usdc - stake > -c.risk.max_daily_loss_usdc,
            "daily_loss_budget",
        )
        gate(intent.size >= ctx.session.meta.min_order_size, "below_min_size")

        # Liquidity gate: top-of-book must hold ≥ size × multiple.
        if intent.side == "BUY":
            top_size = ctx.book_top.ask_size if ctx.book_top.ask is not None else 0.0
        else:
            top_size = ctx.book_top.bid_size if ctx.book_top.bid is not None else 0.0
        gate(top_size >= intent.size * c.risk.min_liquidity_multiple, "thin_book")

        result = GateResult(allowed=not failed, failed=failed)
        return result
