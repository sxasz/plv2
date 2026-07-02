"""CLOB market-channel book mirror with integrity checking (spec §4).

The mirror is *pessimistic*: any parse anomaly, crossed book, unknown event
shape or reconnect marks the affected token's book dirty, which blocks
trading (risk gate) until a fresh full `book` snapshot (WS or REST) restores
it. No silent resyncs: every dirty/clean transition is an incident.

Exact event schemas are FACTS.md #4.3/#4.6 (runtime-verify): parsing is
centralized here and defensive, and every raw frame is recorded so M1 can
validate the mapping against reality.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

import aiohttp
import orjson

from ..types import BookTop, PriceLevel
from .base import WsFeed

log = logging.getLogger(__name__)


class RestBookSource(Protocol):
    async def get_book(self, token_id: str) -> tuple[list[Any], list[Any]] | None:
        """Return (bids, asks) as lists of {price,size} or None on failure."""


class OrderBook:
    """One token's price-level book. Prices keyed as strings to avoid FP drift."""

    __slots__ = ("asks", "bids", "dirty", "last_hash", "token_id", "updated_mono_ns")

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[str, float] = {}
        self.asks: dict[str, float] = {}
        self.dirty = True  # dirty until first full snapshot
        self.last_hash: str = ""
        self.updated_mono_ns = 0

    def apply_snapshot(
        self, bids: list[Any], asks: list[Any], hash_: str, recv_mono_ns: int
    ) -> None:
        self.bids = {lv["price"]: float(lv["size"]) for lv in bids}
        self.asks = {lv["price"]: float(lv["size"]) for lv in asks}
        self.last_hash = hash_
        self.updated_mono_ns = recv_mono_ns
        self.dirty = self._crossed()

    def apply_delta(self, price: str, side: str, size: float, recv_mono_ns: int) -> None:
        levels = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0.0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.updated_mono_ns = recv_mono_ns
        if self._crossed():
            self.dirty = True

    def _crossed(self) -> bool:
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] >= ba[0]

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        px = max(self.bids, key=float)
        return float(px), self.bids[px]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        px = min(self.asks, key=float)
        return float(px), self.asks[px]

    def top_levels(self, side: str, n: int = 3) -> list[PriceLevel]:
        levels = self.bids if side.upper() == "BUY" else self.asks
        keys = sorted(levels, key=float, reverse=(side.upper() == "BUY"))[:n]
        return [PriceLevel(price=float(k), size=levels[k]) for k in keys]

    def top(self) -> BookTop:
        bb, ba = self.best_bid(), self.best_ask()
        return BookTop(
            token_id=self.token_id,
            bid=bb[0] if bb else None,
            bid_size=bb[1] if bb else 0.0,
            ask=ba[0] if ba else None,
            ask_size=ba[1] if ba else 0.0,
            bids_top3=self.top_levels("BUY"),
            asks_top3=self.top_levels("SELL"),
            updated_mono_ns=self.updated_mono_ns,
        )


class TradePrint:
    __slots__ = ("price", "recv_mono_ns", "side", "size", "token_id", "ts_ms")

    def __init__(
        self, token_id: str, price: float, size: float, side: str, ts_ms: int, recv_mono_ns: int
    ) -> None:
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side
        self.ts_ms = ts_ms
        self.recv_mono_ns = recv_mono_ns


class ClobMarketFeed(WsFeed):
    name = "clob_market"
    ping_interval_s = 10.0  # FACTS.md #4.4
    ping_payload = "PING"

    def __init__(
        self,
        url: str,
        rest: RestBookSource | None = None,
        **kw: object,
    ) -> None:
        super().__init__(url, **kw)  # type: ignore[arg-type]
        self.rest = rest
        self.books: dict[str, OrderBook] = {}
        self.on_book_update: Callable[[OrderBook, int], None] | None = None
        self.on_trade: Callable[[TradePrint], None] | None = None
        self.on_tick_size_change: Callable[[str, float], None] | None = None

    # -- subscription management ----------------------------------------------
    def track(self, token_ids: list[str]) -> None:
        for tid in token_ids:
            self.books.setdefault(tid, OrderBook(tid))

    def untrack(self, token_ids: list[str]) -> None:
        for tid in token_ids:
            self.books.pop(tid, None)

    async def resubscribe(self) -> None:
        """(Re)send the subscription for the currently tracked tokens."""
        if self.books and self.health.connected:
            await self.send_json({"assets_ids": list(self.books), "type": "market"})
            # Books are dirty until the server's fresh snapshots arrive.
            for book in self.books.values():
                book.dirty = True

    async def on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        for book in self.books.values():
            book.dirty = True
        if self.books:
            await self.send_json({"assets_ids": list(self.books), "type": "market"})

    async def on_resync(self) -> None:
        await self.rest_resync()

    async def rest_resync(self) -> None:
        """Cross-check/restore books from REST snapshots (never on the hot path)."""
        if self.rest is None:
            return
        from ..clock import mono_ns

        for tid, book in list(self.books.items()):
            snap = await self.rest.get_book(tid)
            if snap is not None:
                bids, asks = snap
                book.apply_snapshot(bids, asks, hash_="rest", recv_mono_ns=mono_ns())
                log.info("book restored from REST", extra={"ctx": {"token": tid[:16]}})

    # -- parsing ---------------------------------------------------------------
    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None:
        doc = orjson.loads(raw)
        events = doc if isinstance(doc, list) else [doc]
        for ev in events:
            if isinstance(ev, dict):
                self._handle_event(ev, recv_mono_ns)

    def _handle_event(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        et = ev.get("event_type", "")
        if et == "book":
            self._on_book(ev, recv_mono_ns)
        elif et == "price_change":
            self._on_price_change(ev, recv_mono_ns)
        elif et == "tick_size_change":
            tid = str(ev.get("asset_id", ""))
            new_tick = float(ev.get("new_tick_size", 0) or 0)
            if tid in self.books and self.on_tick_size_change is not None and new_tick > 0:
                self.on_tick_size_change(tid, new_tick)
        elif et == "last_trade_price":
            self._on_trade(ev, recv_mono_ns)
        # Unknown event types are recorded (raw frames) but not acted upon.

    def _on_book(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        tid = str(ev.get("asset_id", ""))
        book = self.books.get(tid)
        if book is None:
            return
        try:
            book.apply_snapshot(
                ev.get("bids", []) or [],
                ev.get("asks", []) or [],
                hash_=str(ev.get("hash", "")),
                recv_mono_ns=recv_mono_ns,
            )
        except (KeyError, TypeError, ValueError):
            book.dirty = True
            log.exception("bad book snapshot", extra={"ctx": {"token": tid[:16]}})
            return
        if self.on_book_update is not None:
            self.on_book_update(book, recv_mono_ns)

    def _on_price_change(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        tid = str(ev.get("asset_id", ""))
        book = self.books.get(tid)
        if book is None:
            return
        changes = ev.get("changes")
        if changes is None and "price" in ev:
            changes = [ev]  # older flat schema
        if not isinstance(changes, list):
            book.dirty = True
            return
        try:
            for ch in changes:
                book.apply_delta(
                    price=str(ch["price"]),
                    side=str(ch.get("side", "")),
                    size=float(ch["size"]),
                    recv_mono_ns=recv_mono_ns,
                )
        except (KeyError, TypeError, ValueError):
            book.dirty = True
            log.exception("bad price_change", extra={"ctx": {"token": tid[:16]}})
            return
        if self.on_book_update is not None:
            self.on_book_update(book, recv_mono_ns)

    def _on_trade(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        tid = str(ev.get("asset_id", ""))
        if tid not in self.books or self.on_trade is None:
            return
        try:
            tp = TradePrint(
                token_id=tid,
                price=float(ev["price"]),
                size=float(ev.get("size", 0) or 0),
                side=str(ev.get("side", "")),
                ts_ms=int(ev.get("timestamp", 0) or 0),
                recv_mono_ns=recv_mono_ns,
            )
        except (KeyError, TypeError, ValueError):
            log.exception("bad last_trade_price", extra={"ctx": {"token": tid[:16]}})
            return
        self.on_trade(tp)
