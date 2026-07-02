"""Time utilities: window boundaries, monotonic stamps, NTP discipline check.

The trading engine must never trust the wall clock blindly — window
boundaries are Unix-time-aligned and the arm gate refuses to trade if the
NTP-measured offset exceeds the configured bound (spec §6).
"""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass

NTP_EPOCH_DELTA = 2208988800  # seconds between 1900-01-01 and 1970-01-01
WINDOW_LEN_S = 300


def mono_ns() -> int:
    """Local monotonic receive-time stamp (ns). Used for latency & ordering."""
    return time.monotonic_ns()


def wall_ns() -> int:
    """Wall-clock epoch time (ns). Used for window boundary math."""
    return time.time_ns()


class Clock:
    """Injectable time source: real for live/shadow-live, virtual for replay.

    The engine must never call time.* directly for decision logic — replay
    determinism (spec §8 rule 4) depends on all decision-relevant time coming
    through here.
    """

    def mono_ns(self) -> int:
        return time.monotonic_ns()

    def wall_ns(self) -> int:
        return time.time_ns()

    def wall_s(self) -> float:
        return self.wall_ns() / 1e9


class VirtualClock(Clock):
    """Replay clock: advanced by the replayer to each frame's recorded stamps."""

    def __init__(self) -> None:
        self._mono_ns = 0
        self._wall_ns = 0

    def set(self, mono_ns_: int, wall_ns_: int) -> None:
        # Time never goes backwards even if a recording glitch says otherwise.
        self._mono_ns = max(self._mono_ns, mono_ns_)
        self._wall_ns = max(self._wall_ns, wall_ns_)

    def mono_ns(self) -> int:
        return self._mono_ns

    def wall_ns(self) -> int:
        return self._wall_ns


def window_start(ts_s: float, length_s: int = WINDOW_LEN_S) -> int:
    return int(ts_s) - (int(ts_s) % length_s)


def window_close(ts_s: float, length_s: int = WINDOW_LEN_S) -> int:
    return window_start(ts_s, length_s) + length_s


def seconds_to_close(ts_s: float, length_s: int = WINDOW_LEN_S) -> float:
    """τ: seconds until the current window closes (float, uses fractional time)."""
    return window_close(ts_s, length_s) - ts_s


def slug_for_window(ws: int) -> str:
    """Deterministic market slug (FACTS.md #1.2)."""
    return f"btc-updown-5m-{ws}"


@dataclass(slots=True)
class NtpSample:
    offset_s: float
    rtt_s: float
    checked_wall_ns: int


class _NtpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.reply: asyncio.Future[tuple[bytes, float]] = asyncio.get_running_loop().create_future()
        self._t0 = 0.0

    def sent(self) -> None:
        self._t0 = time.monotonic()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self.reply.done():
            self.reply.set_result((data, time.monotonic() - self._t0))

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)


async def ntp_offset(server: str, timeout_s: float = 3.0) -> NtpSample:
    """Single SNTP query. Returns clock offset (server − local) in seconds.

    Dependency-free (RFC 4330 subset): good enough for a 250 ms sanity gate;
    the VPS itself should run chrony/systemd-timesyncd for actual discipline.
    """
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_NtpProtocol, remote_addr=(server, 123))
    try:
        pkt = bytearray(48)
        pkt[0] = 0x1B  # LI=0, VN=3, Mode=3 (client)
        t1 = time.time()
        proto.sent()
        transport.sendto(bytes(pkt))
        data, rtt = await asyncio.wait_for(proto.reply, timeout_s)
        t4 = t1 + rtt
        if len(data) < 48:
            raise ValueError(f"short NTP reply: {len(data)} bytes")
        # Receive (t2) and transmit (t3) timestamps, 32.32 fixed point.
        t2 = _ntp_ts(data, 32)
        t3 = _ntp_ts(data, 40)
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        return NtpSample(offset_s=offset, rtt_s=rtt, checked_wall_ns=wall_ns())
    finally:
        transport.close()


def _ntp_ts(data: bytes, off: int) -> float:
    secs, frac = struct.unpack_from("!II", data, off)
    return float(secs) - NTP_EPOCH_DELTA + float(frac) / 2**32
