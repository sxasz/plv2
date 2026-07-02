"""RTDS Chainlink BTC/USD stream — the series the market resolves against.

Responsibilities (spec §4):
(a) capture the first oracle print at/after each 300 s boundary as K
    (the Price to Beat) and persist it,
(b) expose the latest print + age,
(c) feed every print to consumers (vol estimator, basis tracker, engine).

K capture is keyed on the *oracle* timestamp in the payload, not local
receive time, and is idempotent: the earliest qualifying print wins, so a
reconnect's historical dump can backfill a boundary we were dark for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import orjson

from ..clock import window_start
from .base import WsFeed

log = logging.getLogger(__name__)

TOPIC = "crypto_prices_chainlink"
SYMBOL = "btc/usd"


class OraclePrint:
    __slots__ = ("oracle_ts_ms", "recv_mono_ns", "recv_wall_ns", "value")

    def __init__(
        self, value: float, oracle_ts_ms: int, recv_mono_ns: int, recv_wall_ns: int
    ) -> None:
        self.value = value
        self.oracle_ts_ms = oracle_ts_ms
        self.recv_mono_ns = recv_mono_ns
        self.recv_wall_ns = recv_wall_ns


class KCapture:
    __slots__ = ("k", "oracle_ts_ms", "recv_wall_ns", "window_start")

    def __init__(self, window_start_: int, k: float, oracle_ts_ms: int, recv_wall_ns: int) -> None:
        self.window_start = window_start_
        self.k = k
        self.oracle_ts_ms = oracle_ts_ms
        self.recv_wall_ns = recv_wall_ns


class RtdsChainlinkFeed(WsFeed):
    name = "rtds_chainlink"
    ping_interval_s = 5.0  # FACTS.md #5.3
    ping_payload = "PING"

    def __init__(self, url: str, window_len_s: int = 300, **kw: object) -> None:
        super().__init__(url, **kw)  # type: ignore[arg-type]
        self.window_len_s = window_len_s
        self.latest: OraclePrint | None = None
        self._k: dict[int, KCapture] = {}
        self._last_print_before: OraclePrint | None = None
        self.on_print: Callable[[OraclePrint], None] | None = None
        self.on_k_capture: Callable[[KCapture], None] | None = None

    # -- WsFeed --------------------------------------------------------------
    async def on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self.send_json(
            {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": TOPIC,
                        "type": "*",
                        "filters": orjson.dumps({"symbol": SYMBOL}).decode(),
                    }
                ],
            }
        )

    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None:
        if not raw:
            # Server sends an empty text frame as a connection ack before any
            # subscription data; not an error, nothing to parse.
            return
        doc = orjson.loads(raw)
        if not isinstance(doc, dict) or doc.get("topic") != TOPIC:
            return
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return
        # Historical backfill dump on (re)connect: payload = {"data": [...], "symbol": ...}.
        # Live updates: payload = {"symbol":..., "timestamp":..., "value":...} directly.
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                self._handle_print(item, recv_mono_ns, recv_wall_ns)
        elif "value" in payload:
            self._handle_print(payload, recv_mono_ns, recv_wall_ns)

    def _handle_print(self, p: dict[str, Any], recv_mono_ns: int, recv_wall_ns: int) -> None:
        if p.get("symbol") not in (SYMBOL, None):
            return
        value = float(p["value"])
        oracle_ts_ms = int(p["timestamp"])
        pr = OraclePrint(value, oracle_ts_ms, recv_mono_ns, recv_wall_ns)
        if self.latest is None or oracle_ts_ms >= self.latest.oracle_ts_ms:
            self.latest = pr
        self._maybe_capture_k(pr)
        if self.on_print is not None:
            self.on_print(pr)

    # -- K capture -------------------------------------------------------------
    def _maybe_capture_k(self, pr: OraclePrint) -> None:
        ws = window_start(pr.oracle_ts_ms / 1000.0, self.window_len_s)
        existing = self._k.get(ws)
        if existing is None or pr.oracle_ts_ms < existing.oracle_ts_ms:
            cap = KCapture(ws, pr.value, pr.oracle_ts_ms, pr.recv_wall_ns)
            self._k[ws] = cap
            # Evict old windows to bound memory.
            if len(self._k) > 32:
                for key in sorted(self._k)[:-16]:
                    del self._k[key]
            log.info(
                "K captured",
                extra={
                    "ctx": {
                        "window_start": ws,
                        "k": pr.value,
                        "lag_ms": pr.oracle_ts_ms - ws * 1000,
                    }
                },
            )
            if self.on_k_capture is not None:
                self.on_k_capture(cap)

    def get_k(self, window_start_: int) -> KCapture | None:
        return self._k.get(window_start_)
