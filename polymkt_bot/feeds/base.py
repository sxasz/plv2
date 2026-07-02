"""Common WebSocket feed machinery (spec §4).

Every feed gets: auto-reconnect with jittered exponential backoff, an
application-level ping task, a staleness watchdog exposing age_ms, raw-frame
recording, and an explicit resync hook invoked after every (re)connect so no
reconnect is ever silent.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..clock import mono_ns, wall_ns
from ..storage.recorder import Recorder

log = logging.getLogger(__name__)

BACKOFF_MIN_S = 0.5
BACKOFF_MAX_S = 30.0

IncidentSink = Callable[[str, str, str, int], Awaitable[None]]


class FeedHealth:
    """Freshness/connection state; risk gates read age_ms before every trade."""

    __slots__ = ("connected", "last_msg_mono_ns", "reconnects")

    def __init__(self) -> None:
        self.last_msg_mono_ns: int = 0
        self.connected: bool = False
        self.reconnects: int = 0

    @property
    def age_ms(self) -> float:
        if self.last_msg_mono_ns == 0:
            return float("inf")
        return (mono_ns() - self.last_msg_mono_ns) / 1e6

    def is_fresh(self, max_age_ms: float) -> bool:
        return self.connected and self.age_ms <= max_age_ms


class WsFeed(abc.ABC):
    """Reconnecting WS client. Subclasses implement subscribe + parse."""

    name: str = "feed"
    ping_interval_s: float = 10.0
    ping_payload: str | None = "PING"  # None => rely on protocol-level ping

    def __init__(
        self,
        url: str,
        recorder: Recorder | None = None,
        incident_sink: IncidentSink | None = None,
    ) -> None:
        self.url = url
        self.recorder = recorder
        self.health = FeedHealth()
        self._incident_sink = incident_sink
        self._stop = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    # -- subclass API --------------------------------------------------------
    @abc.abstractmethod
    async def on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send subscriptions. Called on every (re)connect, before messages flow."""

    @abc.abstractmethod
    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None:
        """Parse one text frame. Must be fast and never raise for bad payloads."""

    async def on_resync(self) -> None:  # noqa: B027 — optional hook
        """Called after (re)connect; override to restore state from snapshots."""

    # -- lifecycle -----------------------------------------------------------
    async def run(self) -> None:
        backoff = BACKOFF_MIN_S
        session = aiohttp.ClientSession()
        try:
            while not self._stop.is_set():
                try:
                    async with session.ws_connect(self.url, heartbeat=25.0) as ws:
                        self._ws = ws
                        self.health.connected = True
                        backoff = BACKOFF_MIN_S
                        log.info("connected", extra={"ctx": {"feed": self.name}})
                        await self.on_connected(ws)
                        await self.on_resync()
                        ping_task = asyncio.create_task(self._pinger(ws))
                        try:
                            await self._read_loop(ws)
                        finally:
                            ping_task.cancel()
                except asyncio.CancelledError:
                    raise
                except (aiohttp.ClientError, ConnectionError, OSError, TimeoutError) as exc:
                    log.warning("feed error", extra={"ctx": {"feed": self.name, "err": repr(exc)}})
                self._ws = None
                if self.health.connected:
                    self.health.connected = False
                    self.health.reconnects += 1
                    await self._incident("reconnect", "connection lost")
                if self._stop.is_set():
                    break
                delay = backoff * (1.0 + random.random() * 0.25)
                backoff = min(backoff * 2.0, BACKOFF_MAX_S)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            await session.close()

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def reconnect(self) -> None:
        """Force-close the current connection so `run`'s loop reopens it fresh.

        Some subscribe-style feeds only accept a subscription update in-place
        once per connection and reject further changes; a clean reconnect
        re-runs `on_connected`, which (re)subscribes everything from scratch.
        """
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                t_mono, t_wall = mono_ns(), wall_ns()
                self.health.last_msg_mono_ns = t_mono
                raw: str = msg.data
                if self.recorder is not None:
                    self.recorder.record_raw_frame(self.name, raw)
                if raw == "PONG":
                    continue
                try:
                    self.on_message(raw, t_mono, t_wall)
                except Exception:
                    # …but they are loud, counted, and never mask trading state:
                    # consumers gate on parsed-state freshness, not frame arrival.
                    log.exception(
                        "parse error", extra={"ctx": {"feed": self.name, "raw": raw[:400]}}
                    )
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                break

    async def _pinger(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(self.ping_interval_s)
            if self.ping_payload is not None and not ws.closed:
                try:
                    await ws.send_str(self.ping_payload)
                except (aiohttp.ClientError, ConnectionError):
                    return

    async def _incident(self, kind: str, detail: str) -> None:
        if self._incident_sink is not None:
            await self._incident_sink(self.name, kind, detail, wall_ns())

    # -- helpers for subclasses ----------------------------------------------
    async def send_json(self, doc: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise ConnectionError(f"{self.name}: not connected")
        await self._ws.send_str(_dumps(doc))


def _dumps(doc: dict[str, Any]) -> str:
    import orjson

    return orjson.dumps(doc).decode()
