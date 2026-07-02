"""Shadow exchange: honest simulated fills (spec §8 — the GO/NO-GO gate).

Rules implemented exactly as specified, because optimistic dry-runs are how
bots that "backtest great" lose real money:

1. A simulated taker fill executes only against recorded opposite-side depth
   as it stood at signal_time + measured order RTT. Fill only up to the
   visible size at each level within the limit price; deeper size fails
   (FAK kills the remainder, FOK kills everything).
2. All fills pay the live fee formula. Buy fees reduce shares (proceeds
   convention, FACTS.md #2.4) — switchable once M1 observes real fill events.
3. Simulated maker orders fill only if the trade tape shows executions
   through their price after placement time, with a conservative queue model:
   we are last in queue behind the visible size at placement.
4. No lookahead: the exchange acts only when `process_due(now)` is called
   with monotonically increasing time — the replayer drives it frame by
   frame; live shadow drives it from feed events and the engine ticker.
5. Never marks at mid: fills come from touched liquidity only.

Determinism: RTT sampling uses a seeded RNG; with the same recorded input
the same fills happen (replayer determinism test relies on this).
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from ..exec.fees import FeeModel
from ..exec.order_manager import ManagedOrder, PlaceResult
from ..feeds.polymarket_clob_ws import OrderBook, TradePrint
from ..types import Fill, TimeInForce

log = logging.getLogger(__name__)

BookProvider = Callable[[str], OrderBook | None]
FeeModelProvider = Callable[[str], FeeModel | None]
FillSink = Callable[[Fill], None]
OrderSink = Callable[[ManagedOrder], None]


@dataclass(slots=True)
class _PendingPlace:
    due_mono_ns: int
    order: ManagedOrder


@dataclass(slots=True)
class _PendingCancel:
    due_mono_ns: int
    order: ManagedOrder


@dataclass(slots=True)
class _RestingMaker:
    order: ManagedOrder
    queue_ahead: float  # visible size at our level when we joined (conservative)
    active_from_mono_ns: int
    remaining: float = field(default=0.0)


class RttSampler:
    """Samples order round-trip time. Prefers live-measured observations
    (reservoir) over the configured lognormal prior (spec §8 rule 1)."""

    def __init__(self, p50_ms: float, p95_ms: float, seed: int = 20260701) -> None:
        self.rng = random.Random(seed)
        self._reservoir: list[float] = []
        # Lognormal from p50/p95: median=exp(mu), p95=exp(mu+1.645 sigma).
        self._mu = math.log(max(p50_ms, 1.0))
        self._sigma = max(math.log(max(p95_ms, p50_ms + 1e-9) / max(p50_ms, 1.0)) / 1.645, 0.01)

    def observe(self, rtt_ms: float) -> None:
        if rtt_ms > 0:
            self._reservoir.append(rtt_ms)
            if len(self._reservoir) > 2048:
                del self._reservoir[: len(self._reservoir) - 1024]

    def sample_ms(self) -> float:
        if len(self._reservoir) >= 30:
            return self.rng.choice(self._reservoir)
        return math.exp(self._mu + self._sigma * self.rng.gauss(0.0, 1.0))


class ShadowExchange:
    """Implements the ExchangeAdapter protocol against mirrored live books."""

    def __init__(
        self,
        books: BookProvider,
        fee_models: FeeModelProvider,
        rtt: RttSampler,
        now_mono_ns: Callable[[], int],
        fee_buy_in_shares: bool = True,
    ) -> None:
        self._books = books
        self._fee_models = fee_models
        self._rtt = rtt
        self._now = now_mono_ns
        self.fee_buy_in_shares = fee_buy_in_shares
        self._pending_places: list[_PendingPlace] = []
        self._pending_cancels: list[_PendingCancel] = []
        self._resting: dict[str, _RestingMaker] = {}  # client_id → maker order
        self._trade_seq = 0
        # Wired by the engine:
        self.on_fill: FillSink | None = None
        self.on_cancel_confirmed: OrderSink | None = None
        self.on_expired: OrderSink | None = None

    # -- ExchangeAdapter -------------------------------------------------------
    async def sign(self, order: ManagedOrder) -> None:
        return  # no-op: signing cost is captured in the RTT distribution

    async def place(self, order: ManagedOrder) -> PlaceResult:
        due = self._now() + int(self._rtt.sample_ms() * 1e6)
        self._pending_places.append(_PendingPlace(due_mono_ns=due, order=order))
        return PlaceResult(ok=True, exchange_id=f"shadow-{order.client_id}")

    async def cancel(self, order: ManagedOrder) -> bool:
        due = self._now() + int(self._rtt.sample_ms() * 1e6)
        self._pending_cancels.append(_PendingCancel(due_mono_ns=due, order=order))
        return True

    async def cancel_all(self, session_slug: str) -> None:
        for resting in list(self._resting.values()):
            if resting.order.session_slug == session_slug:
                await self.cancel(resting.order)
        for pending in list(self._pending_places):
            if pending.order.session_slug == session_slug:
                await self.cancel(pending.order)

    # -- time & tape driving -----------------------------------------------------
    def process_due(self, now_mono_ns: int) -> None:
        """Execute everything whose RTT has elapsed. Called on every event/tick."""
        if self._pending_places:
            due = [p for p in self._pending_places if p.due_mono_ns <= now_mono_ns]
            if due:
                self._pending_places = [
                    p for p in self._pending_places if p.due_mono_ns > now_mono_ns
                ]
                for p in sorted(due, key=lambda x: x.due_mono_ns):
                    self._arrive(p.order, p.due_mono_ns)
        if self._pending_cancels:
            due_c = [c for c in self._pending_cancels if c.due_mono_ns <= now_mono_ns]
            if due_c:
                self._pending_cancels = [
                    c for c in self._pending_cancels if c.due_mono_ns > now_mono_ns
                ]
                for c in sorted(due_c, key=lambda x: x.due_mono_ns):
                    self._arrive_cancel(c.order)

    def on_trade(self, tp: TradePrint) -> None:
        """Trade tape drives conservative maker fills (rule 3)."""
        for resting in list(self._resting.values()):
            o = resting.order
            if o.token_id != tp.token_id or tp.recv_mono_ns < resting.active_from_mono_ns:
                continue
            through = (
                tp.price >= o.price - 1e-12 if o.side == "SELL" else tp.price <= o.price + 1e-12
            )
            if not through:
                continue
            traded = tp.size
            if resting.queue_ahead > 0.0:
                consumed = min(resting.queue_ahead, traded)
                resting.queue_ahead -= consumed
                traded -= consumed
            if traded <= 0.0:
                continue
            fill_size = min(traded, resting.remaining)
            if fill_size <= 0.0:
                continue
            resting.remaining -= fill_size
            self._emit_fill(o, price=o.price, size=fill_size, taker=False)
            if resting.remaining <= 1e-9:
                del self._resting[o.client_id]

    # -- internals -------------------------------------------------------------
    def _arrive(self, order: ManagedOrder, arrival_mono_ns: int) -> None:
        """Order reaches the exchange after its RTT."""
        book = self._books(order.token_id)
        if book is None or book.dirty:
            # No book → treat as rejected-at-exchange (kills the order).
            if self.on_expired is not None:
                self.on_expired(order)
            return
        if order.tif in (TimeInForce.FAK, TimeInForce.FOK):
            self._taker_fill(order, book)
        elif order.tif is TimeInForce.GTC:
            self._rest_maker(order, book, arrival_mono_ns)
        else:  # GTD not used by current strategies
            self._rest_maker(order, book, arrival_mono_ns)

    def _taker_fill(self, order: ManagedOrder, book: OrderBook) -> None:
        levels = (
            book.top_levels("SELL", n=64) if order.side == "BUY" else book.top_levels("BUY", n=64)
        )
        fills: list[tuple[float, float]] = []
        remaining = order.size
        for lv in levels:
            in_limit = (
                lv.price <= order.price + 1e-12
                if order.side == "BUY"
                else (lv.price >= order.price - 1e-12)
            )
            if not in_limit or remaining <= 0.0:
                break
            take = min(lv.size, remaining)  # rule 1: only visible size
            if take > 0.0:
                fills.append((lv.price, take))
                remaining -= take
        if order.tif is TimeInForce.FOK and remaining > 1e-9:
            fills = []  # all-or-nothing
        if not fills:
            if self.on_expired is not None:
                self.on_expired(order)
            return
        for price, size in fills:
            self._emit_fill(order, price=price, size=size, taker=True)
        if remaining > 1e-9 and self.on_expired is not None:
            self.on_expired(order)  # FAK remainder killed

    def _rest_maker(self, order: ManagedOrder, book: OrderBook, arrival_mono_ns: int) -> None:
        # A "maker" order priced through the touch would actually TAKE on the
        # real exchange; be honest about that.
        top = book.top()
        crosses = (
            top.ask is not None and order.price >= top.ask
            if order.side == "BUY"
            else top.bid is not None and order.price <= top.bid
        )
        if crosses:
            self._taker_fill(order, book)
            return
        levels = book.bids if order.side == "BUY" else book.asks
        ahead = 0.0
        for px_str, size in levels.items():
            if abs(float(px_str) - order.price) < 1e-12:
                ahead = size  # conservative: we join behind everything visible
                break
        self._resting[order.client_id] = _RestingMaker(
            order=order,
            queue_ahead=ahead,
            active_from_mono_ns=arrival_mono_ns,
            remaining=order.size,
        )

    def _arrive_cancel(self, order: ManagedOrder) -> None:
        resting = self._resting.pop(order.client_id, None)
        still_pending = [p for p in self._pending_places if p.order.client_id == order.client_id]
        for p in still_pending:
            self._pending_places.remove(p)
        if resting is not None or still_pending or not order.is_terminal:
            if self.on_cancel_confirmed is not None:
                self.on_cancel_confirmed(order)

    def _emit_fill(self, order: ManagedOrder, price: float, size: float, taker: bool) -> None:
        fee_model = self._fee_models(order.token_id)
        if fee_model is None:
            log.error(
                "shadow fill without fee model — dropping order",
                extra={"ctx": {"id": order.client_id}},
            )
            if self.on_expired is not None:
                self.on_expired(order)
            return
        fee_shares = 0.0
        fee_usdc = 0.0
        if taker:
            if order.side == "BUY" and self.fee_buy_in_shares:
                fee_shares = fee_model.buy_fee_shares(price, size)
            else:
                fee_usdc = fee_model.taker_fee_usd(price, size)
        self._trade_seq += 1
        fill = Fill(
            order_id=f"shadow-{order.client_id}",
            token_id=order.token_id,
            side=order.side,
            price=price,
            size=size,
            fee_shares=fee_shares,
            fee_usdc=fee_usdc,
            exchange_ts_ms=0,  # engine stamps decision-relative times in the log
            recv_mono_ns=self._now(),
            trade_id=f"sh-t{self._trade_seq}",
        )
        if self.on_fill is not None:
            self.on_fill(fill)
