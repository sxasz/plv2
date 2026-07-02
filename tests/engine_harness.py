"""Builds a full engine wired to a VirtualClock + shadow exchange, no network.

Used by the rollover and replay-determinism tests — the same code path as
`main.run_replay`, with a stub decisions recorder that captures payload bytes
for byte-identical comparison.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from polymkt_bot.accounting.pnl import Ledger
from polymkt_bot.clock import VirtualClock
from polymkt_bot.config import Config
from polymkt_bot.engine import Engine
from polymkt_bot.exec.fees import FeeModel
from polymkt_bot.exec.order_manager import OrderManager
from polymkt_bot.feeds.binance_ws import BinanceFeed
from polymkt_bot.feeds.polymarket_clob_ws import ClobMarketFeed
from polymkt_bot.feeds.rtds_chainlink_ws import RtdsChainlinkFeed
from polymkt_bot.main import build_strategies
from polymkt_bot.risk.risk_manager import RiskManager
from polymkt_bot.sim.replayer import StaticDiscovery
from polymkt_bot.sim.shadow_exchange import RttSampler, ShadowExchange
from polymkt_bot.storage.database import Database
from polymkt_bot.types import MarketMeta


class CapturingRecorder:
    """Recorder stand-in: captures payload bytes (deterministic, no clock)."""

    def __init__(self) -> None:
        self.records: list[bytes] = []

    def record(self, kind: str, feed: str, payload: Any) -> None:
        self.records.append(orjson.dumps({"kind": kind, "payload": payload}))

    def record_raw_frame(self, feed: str, frame: str | bytes) -> None:
        return


class EngineHarness:
    def __init__(self, cfg: Config, metas: dict[int, MarketMeta], tmp: Path) -> None:
        self.clock = VirtualClock()
        self.binance = BinanceFeed("wss://example.invalid")
        self.rtds = RtdsChainlinkFeed("wss://example.invalid")
        self.clob = ClobMarketFeed("wss://example.invalid/ws/market")
        self.ledger = Ledger()
        self.risk = RiskManager(cfg)
        self.db = Database(tmp / "test.sqlite")
        self.db.connect()
        self.decisions = CapturingRecorder()
        fee_models: dict[str, FeeModel] = {}
        rtt = RttSampler(cfg.shadow.rtt_ms_p50, cfg.shadow.rtt_ms_p95, seed=7)
        self.shadow = ShadowExchange(
            books=self.clob.books.get,
            fee_models=fee_models.get,
            rtt=rtt,
            now_mono_ns=self.clock.mono_ns,
        )
        self.om = OrderManager(self.shadow)
        self.engine = Engine(
            cfg,
            self.clock,
            self.binance,
            self.rtds,
            self.clob,
            StaticDiscovery(metas),
            self.om,
            self.risk,
            self.ledger,
            self.db,
            self.decisions,  # type: ignore[arg-type]
            build_strategies(cfg),
            shadow=self.shadow,
            replay_mode=True,
            fee_models=fee_models,
        )

    def feed_map(self) -> dict[str, Any]:
        return {"binance": self.binance, "rtds_chainlink": self.rtds, "clob_market": self.clob}

    async def frame_hook(self, mono_ns: int, wall_ns: int) -> None:
        import asyncio

        await self.engine.manage_sessions(self.clock.wall_s())
        self.engine._pump(mono_ns)
        # Same deterministic yield as main.run_replay.
        await asyncio.sleep(0)

    def close(self) -> None:
        self.db.close()
