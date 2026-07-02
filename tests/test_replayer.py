"""Replayer mechanics: ordering, clamping, feed-level determinism, K capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymkt_bot.clock import VirtualClock
from polymkt_bot.feeds.binance_ws import BinanceFeed
from polymkt_bot.feeds.rtds_chainlink_ws import RtdsChainlinkFeed
from polymkt_bot.sim.replayer import Replayer

from .frames import WS, write_recording


def make_feeds() -> dict[str, object]:
    return {
        "binance": BinanceFeed("wss://example.invalid"),
        "rtds_chainlink": RtdsChainlinkFeed("wss://example.invalid"),
    }


def test_feed_level_replay_and_k_capture(tmp_path: Path) -> None:
    write_recording(tmp_path / "raw")
    clock = VirtualClock()
    feeds = make_feeds()
    replayer = Replayer(tmp_path / "raw", clock, feeds)  # type: ignore[arg-type]
    replayer.run()
    assert replayer.frames_replayed > 900
    rtds = feeds["rtds_chainlink"]
    assert isinstance(rtds, RtdsChainlinkFeed)
    # The first print at/after each boundary became K.
    # First print at/after the boundary: t=0.05 → _price_at gives K+2.
    cap = rtds.get_k(WS)
    assert cap is not None and cap.k == pytest.approx(100_002.0)
    cap_next = rtds.get_k(WS + 300)
    assert cap_next is not None
    bn = feeds["binance"]
    assert isinstance(bn, BinanceFeed)
    assert bn.state.mid > 100_000.0  # final ramp applied


def test_out_of_order_frames_clamped(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    rows = [
        {
            "kind": "frame",
            "feed": "binance",
            "mono_ns": 2_000_000,
            "wall_ns": 2_000_000,
            "payload": json.dumps(
                {"stream": "btcusdt@bookTicker", "data": {"b": "1", "B": "1", "a": "2", "A": "1"}}
            ),
        },
        {
            "kind": "frame",
            "feed": "binance",
            "mono_ns": 1_000_000,
            "wall_ns": 1_000_000,
            "payload": json.dumps(
                {"stream": "btcusdt@bookTicker", "data": {"b": "3", "B": "1", "a": "4", "A": "1"}}
            ),
        },
    ]
    with (raw / "0.jsonl").open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    clock = VirtualClock()
    replayer = Replayer(raw, clock, make_feeds())  # type: ignore[arg-type]
    replayer.run()
    assert replayer.order_regressions == 1
    assert clock.mono_ns() == 2_000_000  # clamped, never went backwards


def test_unknown_feed_skipped(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    with (raw / "0.jsonl").open("w") as fh:
        fh.write(
            json.dumps(
                {"kind": "frame", "feed": "mystery", "mono_ns": 1, "wall_ns": 1, "payload": "{}"}
            )
            + "\n"
        )
    replayer = Replayer(raw, VirtualClock(), make_feeds())  # type: ignore[arg-type]
    replayer.run()
    assert replayer.frames_replayed == 0
    assert replayer.frames_skipped == 1
