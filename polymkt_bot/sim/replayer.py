"""Deterministic replay of recorded raw frames (spec §8 rule 4).

Frames are fed strictly by recorded receive time through the *same* feed
parsers the live engine uses — no separate backtest parsing path, no
lookahead. The recorder's single writer queue guarantees per-file mono_ns
ordering; files are replayed in name (hour) order and monotonicity is
asserted, not assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

import orjson

from ..clock import VirtualClock
from ..markets.discovery import MarketDiscovery
from ..types import MarketMeta

log = logging.getLogger(__name__)


class FrameConsumer(Protocol):
    """What the replayer needs from a feed: the live parser + health stamps."""

    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None: ...


class Replayer:
    def __init__(
        self,
        raw_dir: str | Path,
        clock: VirtualClock,
        feeds: dict[str, FrameConsumer],
        on_frame_done: Callable[[int, int], None] | None = None,
    ) -> None:
        """`feeds` maps recorded feed names → live feed objects.
        `on_frame_done(mono_ns, wall_ns)` is the engine's evaluation hook,
        called after each frame is applied (this is where shadow RTTs elapse
        and strategies tick)."""
        self.raw_dir = Path(raw_dir)
        self.clock = clock
        self.feeds = feeds
        self.on_frame_done = on_frame_done
        self.frames_replayed = 0
        self.frames_skipped = 0
        self.order_regressions = 0
        self._last_mono = 0

    def _files(self) -> list[Path]:
        return sorted(self.raw_dir.glob("*.jsonl"))

    def _frames(self) -> Iterator[dict[str, Any]]:
        for path in self._files():
            with path.open("rb") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield orjson.loads(line)
                    except orjson.JSONDecodeError:
                        self.frames_skipped += 1

    def _apply(self, doc: dict[str, Any]) -> tuple[int, int] | None:
        """Apply one frame through the live parser. Returns (mono, wall)."""
        if doc.get("kind") != "frame":
            return None
        feed = self.feeds.get(str(doc.get("feed", "")))
        if feed is None:
            self.frames_skipped += 1
            return None
        mono = int(doc["mono_ns"])
        wall = int(doc["wall_ns"])
        if mono < self._last_mono:
            self.order_regressions += 1
            mono = self._last_mono  # clamp: time never goes backwards
        self._last_mono = mono
        self.clock.set(mono, wall)
        health = getattr(feed, "health", None)
        if health is not None:
            health.connected = True
            health.last_msg_mono_ns = mono
        feed.on_message(str(doc["payload"]), mono, wall)
        self.frames_replayed += 1
        return mono, wall

    def run(self) -> None:
        """Synchronous, single-pass, deterministic replay."""
        self._last_mono = 0
        for doc in self._frames():
            applied = self._apply(doc)
            if applied is not None and self.on_frame_done is not None:
                self.on_frame_done(*applied)
        self._log_complete()

    async def arun(
        self, on_frame_async: Callable[[int, int], Awaitable[None]] | None = None
    ) -> None:
        """Async variant: awaits the engine hook after each frame, so session
        rollover (which touches discovery/storage) stays strictly ordered with
        the frame stream — still deterministic, still no lookahead."""
        self._last_mono = 0
        for doc in self._frames():
            applied = self._apply(doc)
            if applied is not None:
                if self.on_frame_done is not None:
                    self.on_frame_done(*applied)
                if on_frame_async is not None:
                    await on_frame_async(*applied)
        self._log_complete()

    def _log_complete(self) -> None:
        log.info(
            "replay complete",
            extra={
                "ctx": {
                    "frames": self.frames_replayed,
                    "skipped": self.frames_skipped,
                    "regressions": self.order_regressions,
                }
            },
        )


class StaticDiscovery(MarketDiscovery):
    """Replay-time discovery: serves recorded MarketMeta, no network.

    Metadata is recorded at discovery time in live/shadow runs (the engine
    writes `market_meta` records); replays load them here so the identical
    engine code path runs without REST.
    """

    def __init__(self, metas: dict[int, MarketMeta]) -> None:
        # Deliberately no super().__init__: no RestClient exists in replay.
        self._metas = dict(metas)
        self.window_len_s = 300

    async def resolve(self, window_start: int) -> MarketMeta | None:
        return self._metas.get(window_start)

    @classmethod
    def from_recording(cls, decisions_dir: str | Path) -> StaticDiscovery:
        metas: dict[int, MarketMeta] = {}
        for path in sorted(Path(decisions_dir).glob("*.jsonl")):
            with path.open("rb") as fh:
                for line in fh:
                    try:
                        doc = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue
                    if doc.get("kind") == "market_meta":
                        p = doc["payload"]
                        meta = MarketMeta(**p)
                        metas[meta.window_start] = meta
        return cls(metas)
