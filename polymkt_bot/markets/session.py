"""MarketSession: one object per 5-minute window (spec §3).

Owns per-window state and phase logic. The window boundary is treated as the
most dangerous race in the system: teardown is explicit, late fills are
routed back to the *owning* session via the order manager's order→session
mapping, and nothing (orders, exposure, callbacks) may leak into the next
session — enforced by tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..types import MarketMeta, Outcome


class SessionState(StrEnum):
    PENDING = "PENDING"  # metadata prefetched, window not open yet
    ACTIVE = "ACTIVE"  # window open, feeds mirrored
    CLOSING = "CLOSING"  # T-0 hit: cancel-all + reconcile in flight
    SETTLING = "SETTLING"  # awaiting resolution/redemption
    DONE = "DONE"


class Phase(StrEnum):
    WARMUP = "WARMUP"  # first warmup_seconds: no entries
    MID = "MID"  # Mode B territory
    ARMED = "ARMED"  # [T-arm, T-min]: Mode A territory
    LOCKOUT = "LOCKOUT"  # inside T-min: no new entries
    CLOSED = "CLOSED"


_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.PENDING: {SessionState.ACTIVE, SessionState.DONE},
    SessionState.ACTIVE: {SessionState.CLOSING},
    SessionState.CLOSING: {SessionState.SETTLING},
    SessionState.SETTLING: {SessionState.DONE},
    SessionState.DONE: set(),
}


@dataclass(slots=True)
class MarketSession:
    meta: MarketMeta
    warmup_seconds: float
    arm_seconds: float
    min_seconds: float
    state: SessionState = SessionState.PENDING
    k: float | None = None
    k_oracle_ts_ms: int = 0
    outcome: Outcome | None = None
    close_oracle_price: float | None = None
    open_order_ids: set[str] = field(default_factory=set)
    stake_used_usdc: float = 0.0
    entry_edges: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.meta.slug

    def transition(self, new: SessionState) -> None:
        if new not in _TRANSITIONS[self.state]:
            raise ValueError(f"illegal session transition {self.state} -> {new} ({self.slug})")
        self.state = new

    def set_k(self, k: float, oracle_ts_ms: int) -> None:
        """First capture wins; later duplicates are ignored (idempotent)."""
        if self.k is None or oracle_ts_ms < self.k_oracle_ts_ms:
            self.k = k
            self.k_oracle_ts_ms = oracle_ts_ms

    def phase(self, now_s: float) -> Phase:
        if now_s >= self.meta.window_close:
            return Phase.CLOSED
        into = now_s - self.meta.window_start
        remaining = self.meta.window_close - now_s
        if remaining <= self.min_seconds:
            return Phase.LOCKOUT
        if remaining <= self.arm_seconds:
            return Phase.ARMED
        if into < self.warmup_seconds:
            return Phase.WARMUP
        return Phase.MID

    def tau(self, now_s: float) -> float:
        return max(self.meta.window_close - now_s, 0.0)

    def settle(self, close_price: float) -> Outcome:
        """Apply the resolution rule: close ≥ open → UP (tie resolves UP)."""
        if self.k is None:
            raise RuntimeError(f"cannot settle {self.slug}: K never captured")
        self.close_oracle_price = close_price
        self.outcome = "UP" if close_price >= self.k else "DOWN"
        return self.outcome
