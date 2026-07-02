"""Binance combined stream: btcusdt@bookTicker + btcusdt@aggTrade (FACTS.md §6).

Binance is the fast leading signal only — never the strike, never resolution.
"""

from __future__ import annotations

from collections.abc import Callable

import aiohttp
import orjson

from .base import WsFeed


class BinanceState:
    __slots__ = (
        "ask",
        "ask_size",
        "bid",
        "bid_size",
        "last_trade_px",
        "updated_mono_ns",
        "updated_wall_ns",
    )

    def __init__(self) -> None:
        self.bid: float = 0.0
        self.bid_size: float = 0.0
        self.ask: float = 0.0
        self.ask_size: float = 0.0
        self.last_trade_px: float = 0.0
        self.updated_mono_ns: int = 0
        self.updated_wall_ns: int = 0

    @property
    def mid(self) -> float:
        if self.bid > 0.0 and self.ask > 0.0:
            return (self.bid + self.ask) / 2.0
        return self.last_trade_px


class BinanceFeed(WsFeed):
    name = "binance"
    ping_payload = None  # server pings; aiohttp heartbeat answers

    def __init__(self, url: str, **kw: object) -> None:
        super().__init__(url, **kw)  # type: ignore[arg-type]
        self.state = BinanceState()
        self.on_tick: Callable[[BinanceState, int], None] | None = None

    async def on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Combined-stream URL carries the subscription; nothing to send.
        return

    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None:
        doc = orjson.loads(raw)
        data = doc.get("data")
        if not isinstance(data, dict):
            return
        stream = doc.get("stream", "")
        st = self.state
        if stream.endswith("@bookTicker"):
            st.bid = float(data["b"])
            st.bid_size = float(data["B"])
            st.ask = float(data["a"])
            st.ask_size = float(data["A"])
        elif stream.endswith("@aggTrade"):
            st.last_trade_px = float(data["p"])
        else:
            return
        st.updated_mono_ns = recv_mono_ns
        st.updated_wall_ns = recv_wall_ns
        if self.on_tick is not None:
            self.on_tick(st, recv_mono_ns)
