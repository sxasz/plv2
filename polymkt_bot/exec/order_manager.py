"""Order lifecycle FSM and manager (spec §3, non-negotiable):

    DRAFT → SIGNED → SENT → ACKED → (PARTIAL)* → FILLED | CANCELED | REJECTED | EXPIRED

Rules enforced here:
- An order is filled only when a fill event arrives (user channel or REST
  reconciliation). An ACK is not a fill.
- A cancel request does not mean canceled: `cancel_requested` is a flag, the
  CANCELED state only comes from confirmation, and a fill that races the
  cancel wins. A fill that lands after confirmed cancellation is applied to
  accounting anyway and raised as an incident (never dropped).
- Orders carry their owning session slug forever, so late fills reconcile
  into the correct window and never leak into session N+1.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ..clock import mono_ns, wall_ns
from ..types import Fill, OrderIntent, Side, TimeInForce

log = logging.getLogger(__name__)


class OrderState(StrEnum):
    DRAFT = "DRAFT"
    SIGNED = "SIGNED"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL_STATES = {
    OrderState.FILLED,
    OrderState.CANCELED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
}

VALID_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.DRAFT: {OrderState.SIGNED, OrderState.REJECTED},
    OrderState.SIGNED: {OrderState.SENT, OrderState.REJECTED, OrderState.EXPIRED},
    OrderState.SENT: {
        OrderState.ACKED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
        # fill event may beat the HTTP ack:
        OrderState.PARTIAL,
        OrderState.FILLED,
    },
    OrderState.ACKED: {
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.PARTIAL: {
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.EXPIRED,
    },
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}

_client_ids = itertools.count(1)


@dataclass(slots=True)
class ManagedOrder:
    session_slug: str
    token_id: str
    side: Side
    price: float
    size: float
    tif: TimeInForce
    strategy: str
    client_id: str = field(default_factory=lambda: f"c{next(_client_ids)}")
    exchange_id: str = ""
    state: OrderState = OrderState.DRAFT
    filled_size: float = 0.0
    fee_shares: float = 0.0
    fee_usdc: float = 0.0
    cancel_requested: bool = False
    seen_trade_ids: set[str] = field(default_factory=set)
    created_wall_ns: int = field(default_factory=wall_ns)
    decision_mono_ns: int = 0  # when the triggering decision was made
    signed_mono_ns: int = 0
    sent_mono_ns: int = 0
    acked_mono_ns: int = 0
    terminal_mono_ns: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining(self) -> float:
        return max(self.size - self.filled_size, 0.0)

    def transition(self, new: OrderState) -> None:
        if new not in VALID_TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.client_id}: {self.state.value} -> {new.value}")
        self.state = new
        now = mono_ns()
        if new is OrderState.SIGNED:
            self.signed_mono_ns = now
        elif new is OrderState.SENT:
            self.sent_mono_ns = now
        elif new is OrderState.ACKED:
            self.acked_mono_ns = now
        if new in TERMINAL_STATES:
            self.terminal_mono_ns = now


class IllegalTransition(RuntimeError):
    pass


@dataclass(slots=True)
class PlaceResult:
    ok: bool
    exchange_id: str = ""
    error: str = ""


class ExchangeAdapter(Protocol):
    """Implemented by ShadowExchange (M2) and LiveClobExchange (M3)."""

    async def sign(self, order: ManagedOrder) -> None:
        """Sign the order (EIP-712 for live; no-op for shadow)."""

    async def place(self, order: ManagedOrder) -> PlaceResult:
        """Submit. Returns ack/reject. Fills arrive asynchronously."""

    async def cancel(self, order: ManagedOrder) -> bool:
        """Request cancel. True = accepted (NOT confirmation of cancellation)."""

    async def cancel_all(self, session_slug: str) -> None: ...


FillCallback = Callable[[ManagedOrder, Fill], None]
IncidentCallback = Callable[[str, str], Awaitable[None]]


class OrderManager:
    def __init__(
        self,
        adapter: ExchangeAdapter,
        on_fill_applied: FillCallback | None = None,
        incident: IncidentCallback | None = None,
    ) -> None:
        self.adapter = adapter
        self.on_fill_applied = on_fill_applied
        self._incident = incident
        self.orders: dict[str, ManagedOrder] = {}  # client_id → order
        self._by_exchange_id: dict[str, ManagedOrder] = {}

    # -- entry ----------------------------------------------------------------
    def draft(self, intent: OrderIntent, decision_mono_ns: int) -> ManagedOrder:
        order = ManagedOrder(
            session_slug=intent.session_slug,
            token_id=intent.token_id,
            side=intent.side,
            price=intent.price,
            size=intent.size,
            tif=intent.tif,
            strategy=intent.strategy,
            decision_mono_ns=decision_mono_ns,
        )
        self.orders[order.client_id] = order
        return order

    async def submit(self, order: ManagedOrder) -> bool:
        try:
            await self.adapter.sign(order)
            order.transition(OrderState.SIGNED)
            order.transition(OrderState.SENT)
            res = await self.adapter.place(order)
        except IllegalTransition:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            log.error(
                "order submit failed", extra={"ctx": {"id": order.client_id, "err": repr(exc)}}
            )
            if not order.is_terminal:
                order.transition(OrderState.REJECTED)
            return False
        if order.is_terminal or order.state in (OrderState.PARTIAL,):
            # A fill event raced the ack — nothing more to do.
            return True
        if res.ok:
            order.exchange_id = res.exchange_id
            if res.exchange_id:
                self._by_exchange_id[res.exchange_id] = order
            order.transition(OrderState.ACKED)
            return True
        order.transition(OrderState.REJECTED)
        log.info("order rejected", extra={"ctx": {"id": order.client_id, "err": res.error}})
        return False

    # -- cancel ---------------------------------------------------------------
    async def request_cancel(self, order: ManagedOrder) -> None:
        if order.is_terminal:
            return
        order.cancel_requested = True
        await self.adapter.cancel(order)
        # State changes only on confirmation (confirm_canceled / fill event).

    def confirm_canceled(self, order: ManagedOrder) -> None:
        """Exchange confirmed cancellation. A racing fill may already have won."""
        if order.state in (
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        ):
            return
        order.transition(OrderState.CANCELED)

    async def cancel_all_for_session(self, session_slug: str) -> None:
        for order in self.open_orders(session_slug):
            order.cancel_requested = True
        await self.adapter.cancel_all(session_slug)

    # -- fills ----------------------------------------------------------------
    def apply_fill(self, fill: Fill) -> ManagedOrder | None:
        order = self._by_exchange_id.get(fill.order_id) or self.orders.get(fill.order_id)
        if order is None:
            log.error("fill for unknown order", extra={"ctx": {"order_id": fill.order_id}})
            self._fire_incident("unknown_fill", f"order_id={fill.order_id}")
            return None
        if fill.trade_id and fill.trade_id in order.seen_trade_ids:
            return order  # duplicate (e.g. MATCHED then MINED status updates)
        if fill.trade_id:
            order.seen_trade_ids.add(fill.trade_id)

        late_after_cancel = order.state is OrderState.CANCELED
        order.filled_size = min(order.filled_size + fill.size, order.size)
        order.fee_shares += fill.fee_shares
        order.fee_usdc += fill.fee_usdc

        if late_after_cancel:
            # Cancel race lost on the wrong side: exchange filled us after
            # confirming cancel. State stays CANCELED (terminal) but the fill
            # MUST hit accounting, and loudly.
            log.error(
                "fill after confirmed cancel",
                extra={"ctx": {"id": order.client_id, "size": fill.size}},
            )
            self._fire_incident("fill_after_cancel", order.client_id)
        elif order.filled_size >= order.size - 1e-9:
            order.transition(OrderState.FILLED)
        elif order.state in (OrderState.SENT, OrderState.ACKED, OrderState.PARTIAL):
            order.transition(OrderState.PARTIAL)

        if self.on_fill_applied is not None:
            self.on_fill_applied(order, fill)
        return order

    def expire_unfilled(self, order: ManagedOrder) -> None:
        """IOC/FAK remainder killed by the exchange, or GTD expiry."""
        if not order.is_terminal:
            order.transition(OrderState.EXPIRED)

    # -- queries ----------------------------------------------------------------
    def open_orders(self, session_slug: str | None = None) -> list[ManagedOrder]:
        return [
            o
            for o in self.orders.values()
            if not o.is_terminal and (session_slug is None or o.session_slug == session_slug)
        ]

    def latency_samples(self, order: ManagedOrder) -> dict[str, float]:
        """Per-hop latency in microseconds (spec §5)."""
        out: dict[str, float] = {}
        if order.decision_mono_ns and order.sent_mono_ns:
            out["decision_to_sent"] = (order.sent_mono_ns - order.decision_mono_ns) / 1e3
        if order.sent_mono_ns and order.acked_mono_ns:
            out["sent_to_ack"] = (order.acked_mono_ns - order.sent_mono_ns) / 1e3
        return out

    def _fire_incident(self, kind: str, detail: str) -> None:
        if self._incident is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # sync test context: incident already logged above
        task: asyncio.Task[None] = asyncio.ensure_future(self._incident(kind, detail))
        task.add_done_callback(lambda t: t.exception())
