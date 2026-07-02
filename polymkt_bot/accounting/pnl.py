"""Position and PnL accounting, reconciled from fill events only (spec §7).

Fees come from the fill events themselves (shares on buys, collateral on
sells — FACTS.md #2.4), never from locally computed expectations. Redemption
credits winning shares at $1.00 when a session settles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..types import Fill, MarketMeta, Outcome

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TokenPosition:
    token_id: str
    shares: float = 0.0
    cash_flow_usdc: float = 0.0  # signed collateral flow (buys negative)
    fees_usdc: float = 0.0
    fees_shares: float = 0.0

    def apply(self, fill: Fill) -> None:
        self.shares += fill.shares_delta
        self.cash_flow_usdc += fill.usdc_delta
        self.fees_usdc += fill.fee_usdc
        self.fees_shares += fill.fee_shares


@dataclass(slots=True)
class SessionBook:
    """All positions for one window session."""

    slug: str
    window_start: int
    positions: dict[str, TokenPosition] = field(default_factory=dict)
    realized_pnl_usdc: float | None = None  # set at settlement
    n_fills: int = 0

    def apply(self, fill: Fill) -> None:
        pos = self.positions.setdefault(fill.token_id, TokenPosition(fill.token_id))
        pos.apply(fill)
        self.n_fills += 1

    def settle(self, meta: MarketMeta, outcome: Outcome) -> float:
        """Redeem winners at $1, losers at $0; realized = cash flows + redemption."""
        winning_token = meta.token_for(outcome)
        total = 0.0
        for pos in self.positions.values():
            redemption = pos.shares if pos.token_id == winning_token else 0.0
            total += pos.cash_flow_usdc + max(redemption, 0.0)
            if pos.shares < -1e-9:
                # Short shares of the winner: they redeem against us only if
                # we somehow sold shares we didn't own — CLOB doesn't allow
                # naked shorts, so this signals broken accounting.
                log.error(
                    "negative share balance at settlement",
                    extra={
                        "ctx": {"slug": self.slug, "token": pos.token_id[:16], "shares": pos.shares}
                    },
                )
        self.realized_pnl_usdc = total
        return total

    @property
    def fees_usdc(self) -> float:
        return sum(p.fees_usdc for p in self.positions.values())

    @property
    def fees_shares(self) -> float:
        return sum(p.fees_shares for p in self.positions.values())

    def exposure_usdc(self, mark: dict[str, float] | None = None) -> float:
        """Capital at risk: cost basis of open share inventory (conservative:
        marks at cost since these tokens go to 0 or 1 within minutes)."""
        return max(-sum(p.cash_flow_usdc for p in self.positions.values()), 0.0)


class Ledger:
    """Cross-session aggregates used by risk gates and the dashboard."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionBook] = {}
        self._daily_realized: dict[int, float] = {}  # utc_day → pnl

    def book_for(self, slug: str, window_start: int) -> SessionBook:
        book = self.sessions.get(slug)
        if book is None:
            book = SessionBook(slug=slug, window_start=window_start)
            self.sessions[slug] = book
        return book

    def apply_fill(self, slug: str, window_start: int, fill: Fill) -> None:
        self.book_for(slug, window_start).apply(fill)

    def settle_session(self, meta: MarketMeta, outcome: Outcome) -> float:
        book = self.book_for(meta.slug, meta.window_start)
        pnl = book.settle(meta, outcome)
        day = meta.window_start // 86400
        self._daily_realized[day] = self._daily_realized.get(day, 0.0) + pnl
        return pnl

    def daily_realized_pnl(self, now_s: float) -> float:
        return self._daily_realized.get(int(now_s) // 86400, 0.0)

    def open_exposure_usdc(self) -> float:
        return sum(b.exposure_usdc() for b in self.sessions.values() if b.realized_pnl_usdc is None)
