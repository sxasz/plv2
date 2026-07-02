"""End-to-end replay: identical code path, honest fills, settlement, and the
spec §10 requirement — same input ⇒ byte-identical decisions."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from polymkt_bot.config import Config
from polymkt_bot.exec.order_manager import OrderState
from polymkt_bot.sim.replayer import Replayer

from .engine_harness import EngineHarness
from .frames import UP, WS, meta_for, write_recording

pytestmark = pytest.mark.asyncio


async def run_replay(tmp: Path, tag: str) -> tuple[EngineHarness, bytes]:
    raw = tmp / "raw"
    if not raw.exists():
        write_recording(raw)
    h = EngineHarness(Config(), {WS: meta_for(WS), WS + 300: meta_for(WS + 300)}, tmp / tag)
    replayer = Replayer(raw, h.clock, feeds=h.feed_map())
    await replayer.arun(h.frame_hook)
    await asyncio.sleep(0.05)  # drain spawned storage tasks
    digest = hashlib.sha256(b"\n".join(h.decisions.records)).digest()
    return h, digest


async def test_sniper_fires_and_settles_profitably(tmp_path: Path) -> None:
    h, _ = await run_replay(tmp_path, "a")
    try:
        orders = list(h.om.orders.values())
        snipes = [o for o in orders if o.strategy == "mode_a_sniper"]
        assert len(snipes) == 1, f"expected exactly one snipe, got {orders}"
        snipe = snipes[0]
        # Filled honestly from visible depth at the recorded ask.
        assert snipe.state is OrderState.FILLED
        assert snipe.token_id == UP
        assert snipe.price == pytest.approx(0.94)
        assert snipe.filled_size == pytest.approx(snipe.size)
        # Buy fee charged in shares (proceeds convention).
        assert snipe.fee_shares > 0.0

        # Window settled UP (final 100400 ≥ K 100000) with positive PnL.
        book = h.ledger.sessions[f"btc-updown-5m-{WS}"]
        assert book.realized_pnl_usdc is not None
        assert book.realized_pnl_usdc > 0.0
        # PnL = redemption − cost; must be less than the gross win because
        # the share-denominated fee reduced redeemable shares.
        gross = snipe.size * (1.0 - snipe.price)
        assert book.realized_pnl_usdc < gross
    finally:
        h.close()


async def test_no_leakage_across_windows(tmp_path: Path) -> None:
    """Window rollover: session N's orders/exposure never leak into N+1."""
    h, _ = await run_replay(tmp_path, "b")
    try:
        slug_a = f"btc-updown-5m-{WS}"
        slug_b = f"btc-updown-5m-{WS + 300}"
        assert all(o.session_slug == slug_a for o in h.om.orders.values())
        assert not h.om.open_orders(slug_a)  # nothing open after close
        assert not h.om.open_orders(slug_b)
        assert h.ledger.sessions[slug_a].realized_pnl_usdc is not None
        assert slug_b not in h.ledger.sessions  # no fills ever landed in B
        # The new window is live as current, with its own K captured.
        assert h.engine.current is not None
        assert h.engine.current.slug == slug_b
        assert h.engine.current.k is not None
    finally:
        h.close()


async def test_replay_is_deterministic_byte_identical(tmp_path: Path) -> None:
    h1, d1 = await run_replay(tmp_path, "c1")
    h1.close()
    h2, d2 = await run_replay(tmp_path, "c2")
    h2.close()
    assert len(h1.decisions.records) == len(h2.decisions.records)
    assert d1 == d2  # byte-identical decision stream (spec §10)
