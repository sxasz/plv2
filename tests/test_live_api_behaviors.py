"""Regression tests for live-API behaviors discovered during the M1 VPS
deployment (2026-07-02). These lock in the fixes so they can't silently
regress; see FACTS.md §4/§5 and commit 3d645c5.

1. RTDS sends an empty text frame on connect (a bare ack) — must not crash.
2. RTDS backfill dump payload is {"data": [...], "symbol": ...}, not a list —
   prints inside must be parsed (they can backfill a missed K boundary).
3. CLOB market channel accepts only ONE in-place subscribe update per
   connection ("INVALID OPERATION" after that) — track() must report which
   tokens are new so the engine can force a clean reconnect instead.
"""

from __future__ import annotations

import orjson
import pytest

from polymkt_bot.feeds.polymarket_clob_ws import ClobMarketFeed
from polymkt_bot.feeds.rtds_chainlink_ws import RtdsChainlinkFeed

from .conftest import DOWN, UP, WS


def make_rtds() -> RtdsChainlinkFeed:
    return RtdsChainlinkFeed("wss://example.invalid")


class TestRtdsRealFrameShapes:
    def test_empty_connect_ack_frame_is_ignored(self) -> None:
        feed = make_rtds()
        feed.on_message("", 1, 1)  # must not raise
        assert feed.latest is None

    def test_non_json_garbage_never_kills_consumer_state(self) -> None:
        feed = make_rtds()
        with pytest.raises(orjson.JSONDecodeError):
            # The feed base class catches+logs parse errors in live use; the
            # parser itself is allowed to raise on true garbage…
            feed.on_message("INVALID OPERATION", 1, 1)
        assert feed.latest is None  # …but state stays untouched.

    def test_backfill_dump_shape_is_parsed(self) -> None:
        """payload={"data":[...]} — the real reconnect backfill format."""
        feed = make_rtds()
        prints: list[float] = []
        feed.on_print = lambda pr: prints.append(pr.value)
        doc = {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": (WS + 2) * 1000,
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"symbol": "btc/usd", "timestamp": (WS - 1) * 1000, "value": 99_990.0},
                    {"symbol": "btc/usd", "timestamp": (WS + 1) * 1000, "value": 100_010.0},
                ],
            },
        }
        feed.on_message(orjson.dumps(doc).decode(), 5, 5)
        assert prints == [99_990.0, 100_010.0]
        # The backfill recovered the boundary print → K captured for WS.
        cap = feed.get_k(WS)
        assert cap is not None and cap.k == pytest.approx(100_010.0)

    def test_live_update_shape_still_parsed(self) -> None:
        feed = make_rtds()
        doc = {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": (WS + 3) * 1000,
            "payload": {"symbol": "btc/usd", "timestamp": (WS + 3) * 1000, "value": 100_123.0},
        }
        feed.on_message(orjson.dumps(doc).decode(), 6, 6)
        assert feed.latest is not None and feed.latest.value == pytest.approx(100_123.0)


class TestClobSubscribeLimit:
    def test_track_reports_only_new_tokens(self) -> None:
        """The engine reconnects only when track() returns new tokens —
        because a live connection rejects a second in-place subscribe."""
        feed = ClobMarketFeed("wss://example.invalid/ws/market")
        assert feed.track([UP, DOWN]) == [UP, DOWN]
        assert feed.track([UP]) == []  # already tracked → no reconnect needed
        assert feed.track([UP, "333333"]) == ["333333"]

    async def test_reconnect_is_safe_when_disconnected(self) -> None:
        feed = ClobMarketFeed("wss://example.invalid/ws/market")
        await feed.reconnect()  # no live socket: must be a harmless no-op
