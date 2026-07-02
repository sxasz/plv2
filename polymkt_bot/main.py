"""Entrypoint: record | shadow | replay | live.

    python -m polymkt_bot.main --mode record            # M1: recorders only
    python -m polymkt_bot.main --mode shadow            # M2: shadow-live
    python -m polymkt_bot.main --mode replay --raw-dir data/raw
    python -m polymkt_bot.main --mode live              # M3: interlocked

Live mode is refused unless the config + .env interlocks are both set
(spec §12: no live trading before the M2 GO decision).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .accounting.pnl import Ledger
from .clock import Clock, VirtualClock
from .config import Config, load_config
from .engine import Engine
from .exec.fees import FeeModel
from .exec.order_manager import ManagedOrder, OrderManager, PlaceResult
from .feeds.binance_ws import BinanceFeed
from .feeds.polymarket_clob_ws import ClobMarketFeed
from .feeds.rtds_chainlink_ws import RtdsChainlinkFeed
from .logging_setup import setup_logging
from .markets.discovery import MarketDiscovery
from .markets.rest import RestClient
from .risk.risk_manager import RiskManager
from .sim.replayer import Replayer, StaticDiscovery
from .sim.shadow_exchange import RttSampler, ShadowExchange
from .storage.database import Database
from .storage.recorder import Recorder
from .strategy.base import Strategy
from .strategy.maker import MakerStrategy
from .strategy.scalper import ScalperStrategy
from .strategy.sniper import SniperStrategy

log = logging.getLogger(__name__)


class NullAdapter:
    """Record-only mode: no orders, ever."""

    async def sign(self, order: ManagedOrder) -> None:
        return

    async def place(self, order: ManagedOrder) -> PlaceResult:
        return PlaceResult(ok=False, error="record mode: order submission disabled")

    async def cancel(self, order: ManagedOrder) -> bool:
        return True

    async def cancel_all(self, session_slug: str) -> None:
        return


def build_strategies(cfg: Config) -> list[Strategy]:
    return [
        SniperStrategy(cfg.mode_a, cfg.sizing),
        ScalperStrategy(cfg.mode_b, cfg.sizing),
        MakerStrategy(cfg.mode_c, cfg.sizing),
    ]


async def run_online(cfg: Config, mode: str) -> None:
    """record / shadow / live — real feeds, real clock."""
    clock = Clock()
    data_dir = Path(cfg.run.data_dir)
    raw_rec = Recorder(data_dir, "raw")
    dec_rec = Recorder(data_dir, "decisions")
    db = Database(data_dir / "bot.sqlite")
    db.connect()

    rest = RestClient(cfg.endpoints.clob_rest, cfg.endpoints.gamma_rest)
    await rest.start()
    discovery = MarketDiscovery(rest, cfg.window.length_s)

    binance = BinanceFeed(cfg.endpoints.binance_ws, recorder=raw_rec, incident_sink=db.incident)
    rtds = RtdsChainlinkFeed(
        cfg.endpoints.rtds_ws, cfg.window.length_s, recorder=raw_rec, incident_sink=db.incident
    )
    clob = ClobMarketFeed(
        f"{cfg.endpoints.clob_ws}/ws/market",
        rest=rest,
        recorder=raw_rec,
        incident_sink=db.incident,
    )

    ledger = Ledger()
    risk = RiskManager(cfg)

    fee_models: dict[str, FeeModel] = {}  # shared engine <-> shadow
    shadow: ShadowExchange | None = None
    strategies: list[Strategy] = []
    if mode == "record":
        om = OrderManager(NullAdapter())
    elif mode == "shadow":
        rtt = RttSampler(cfg.shadow.rtt_ms_p50, cfg.shadow.rtt_ms_p95)
        shadow = ShadowExchange(
            books=clob.books.get,
            fee_models=fee_models.get,
            rtt=rtt,
            now_mono_ns=clock.mono_ns,
        )
        om = OrderManager(shadow)
        strategies = build_strategies(cfg)
    elif mode == "live":
        from .exec.live_exchange import LiveClobExchange

        adapter = LiveClobExchange(cfg)  # raises LiveTradingBlocked unless interlocked
        om = OrderManager(adapter)
        strategies = build_strategies(cfg)
        log.warning("LIVE MODE: real orders will be submitted at configured stakes")
    else:
        raise ValueError(mode)

    engine = Engine(
        cfg,
        clock,
        binance,
        rtds,
        clob,
        discovery,
        om,
        risk,
        ledger,
        db,
        dec_rec,
        strategies,
        shadow=shadow,
        fee_models=fee_models,
    )

    await raw_rec.start()
    await dec_rec.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    feed_tasks = [
        asyncio.create_task(binance.run(), name="binance"),
        asyncio.create_task(rtds.run(), name="rtds"),
        asyncio.create_task(clob.run(), name="clob"),
    ]
    engine_task = asyncio.create_task(engine.run(), name="engine")

    await stop_event.wait()
    log.info("shutdown requested")
    await engine.stop()  # graceful: cancel-all + final state (spec §6)
    for feed in (binance, rtds, clob):
        await feed.stop()
    for task in [*feed_tasks, engine_task]:
        task.cancel()
    await asyncio.gather(*feed_tasks, engine_task, return_exceptions=True)
    await raw_rec.stop()
    await dec_rec.stop()
    await rest.stop()
    db.close()


async def run_replay(cfg: Config, raw_dir: str, decisions_dir: str | None) -> None:
    """Deterministic replay through the identical engine + shadow path."""
    clock = VirtualClock()
    data_dir = Path(cfg.run.data_dir)
    dec_rec = Recorder(data_dir, "replay_decisions")
    db = Database(data_dir / "replay.sqlite")
    db.connect()

    binance = BinanceFeed(cfg.endpoints.binance_ws)
    rtds = RtdsChainlinkFeed(cfg.endpoints.rtds_ws, cfg.window.length_s)
    clob = ClobMarketFeed(f"{cfg.endpoints.clob_ws}/ws/market")

    meta_dir = decisions_dir or (Path(raw_dir).parent / "decisions")
    discovery = StaticDiscovery.from_recording(meta_dir)
    ledger = Ledger()
    risk = RiskManager(cfg)
    fee_models: dict[str, FeeModel] = {}
    rtt = RttSampler(cfg.shadow.rtt_ms_p50, cfg.shadow.rtt_ms_p95)
    shadow = ShadowExchange(
        books=clob.books.get, fee_models=fee_models.get, rtt=rtt, now_mono_ns=clock.mono_ns
    )
    om = OrderManager(shadow)
    engine = Engine(
        cfg,
        clock,
        binance,
        rtds,
        clob,
        discovery,
        om,
        risk,
        ledger,
        db,
        dec_rec,
        build_strategies(cfg),
        shadow=shadow,
        replay_mode=True,
        fee_models=fee_models,
    )

    await dec_rec.start()
    replayer = Replayer(
        raw_dir,
        clock,
        feeds={"binance": binance, "rtds_chainlink": rtds, "clob_market": clob},
    )

    async def on_frame(mono_ns: int, wall_ns: int) -> None:
        await engine.manage_sessions(clock.wall_s())
        engine._pump(mono_ns)
        # Deterministic yield: run tasks spawned by this frame (order submits,
        # storage writes) before the next frame is applied.
        await asyncio.sleep(0)

    await replayer.arun(on_frame)
    await dec_rec.stop()
    db.close()
    log.info(
        "replay summary",
        extra={
            "ctx": {
                "frames": replayer.frames_replayed,
                "pnl": ledger.daily_realized_pnl(clock.wall_s()),
            }
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket 5m BTC up/down bot")
    parser.add_argument("--mode", choices=["record", "shadow", "replay", "live"], default="record")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--raw-dir", default="data/raw", help="replay input")
    parser.add_argument(
        "--decisions-dir", default=None, help="replay: dir with recorded market_meta"
    )
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(args.config)

    if args.mode == "live" and cfg.run.shadow_mode:
        log.error("live mode requires run.shadow_mode=false in config.yaml")
        sys.exit(2)

    try:
        import uvloop

        runner: object = uvloop
        uvloop.install()
        log.info("uvloop installed")
    except ImportError:
        runner = None
        log.info("uvloop unavailable, using default loop")
    _ = runner

    if args.mode == "replay":
        asyncio.run(run_replay(cfg, args.raw_dir, args.decisions_dir))
    else:
        asyncio.run(run_online(cfg, args.mode))


if __name__ == "__main__":
    main()
