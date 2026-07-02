"""Append-only JSONL recording of raw frames, decisions and order events.

Design (spec §7):
- Every raw WS frame is recorded with feed name, local monotonic + wall
  receive stamps (ns) and the verbatim payload. These files are the input to
  the deterministic replayer, so they must be lossless and strictly ordered
  per feed.
- Writes go through a bounded queue to a single writer task — the hot path
  only does an enqueue. If the queue fills (disk stall), we drop to a counter
  and log an incident rather than block the event loop; a recorder gap marks
  affected windows as unusable for replay rather than silently corrupting them.
- Files rotate hourly; a companion `gzip_old.sh`-style compaction is left to
  cron (compressing in-process would steal CPU from the hot path).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import orjson

from ..clock import mono_ns, wall_ns

log = logging.getLogger(__name__)

QUEUE_MAX = 65536


class Recorder:
    def __init__(self, data_dir: str | Path, name: str = "raw") -> None:
        self.dir = Path(data_dir) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._task: asyncio.Task[None] | None = None
        self._cur_hour: int = -1
        self._fh: Any = None
        self.dropped = 0
        self.written = 0

    # -- hot path -----------------------------------------------------------
    def record(self, kind: str, feed: str, payload: Any) -> None:
        """Enqueue one record. Non-blocking; drops (counted) if queue is full."""
        doc = {
            "kind": kind,
            "feed": feed,
            "mono_ns": mono_ns(),
            "wall_ns": wall_ns(),
            "payload": payload,
        }
        try:
            self._q.put_nowait(orjson.dumps(doc))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 1000 == 1:
                log.error("recorder queue full, dropped=%d", self.dropped)

    def record_raw_frame(self, feed: str, frame: str | bytes) -> None:
        payload = frame.decode() if isinstance(frame, bytes) else frame
        self.record("frame", feed, payload)

    # -- writer -------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._writer(), name=f"recorder:{self.dir.name}")

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._q.put(None)
        await self._task
        self._task = None

    async def _writer(self) -> None:
        loop = asyncio.get_running_loop()
        buf: list[bytes] = []
        while True:
            item = await self._q.get()
            buf.append(item if item is not None else b"")
            # Drain whatever else is queued to batch the file I/O.
            stop = item is None
            while not stop:
                try:
                    nxt = self._q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    stop = True
                    break
                buf.append(nxt)
            lines = [b for b in buf if b]
            if lines:
                await loop.run_in_executor(None, self._write_batch, lines)
                self.written += len(lines)
            buf.clear()
            if stop:
                if self._fh is not None:
                    self._fh.close()
                    self._fh = None
                return

    def _write_batch(self, lines: list[bytes]) -> None:
        hour = wall_ns() // (3600 * 10**9)
        if hour != self._cur_hour or self._fh is None:
            if self._fh is not None:
                self._fh.close()
            path = self.dir / f"{hour * 3600}.jsonl"
            self._fh = path.open("ab")
            self._cur_hour = int(hour)
        self._fh.write(b"\n".join(lines) + b"\n")
        self._fh.flush()
