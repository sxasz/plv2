"""Synthetic raw-frame generator for replay tests.

Produces a realistic ~5.5 minute recording: Chainlink prints at 1 Hz,
Binance bookTicker at 2.5 Hz, CLOB book snapshot + a late repricing that
leaves a fat, cheap UP ask for the sniper — everything the engine needs to
discover a market, capture K, warm the vol estimator, arm, fire, get an
honest shadow fill and settle the window.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from polymkt_bot.types import MarketMeta

WS = 1_782_936_300
UP = "111111"
DOWN = "222222"
K_PRICE = 100_000.0
FINAL_PRICE = 100_400.0  # far above K → UP resolves, P_up pins high


def meta_for(ws: int) -> MarketMeta:
    return MarketMeta(
        window_start=ws,
        window_close=ws + 300,
        slug=f"btc-updown-5m-{ws}",
        condition_id=f"0xcond{ws}",
        up_token=UP,
        down_token=DOWN,
        tick_size=0.001,
        min_order_size=5.0,
        neg_risk=False,
        fee_rate_bps=720,
        fees_known=True,
    )


def _frame(feed: str, t_s: float, payload: Any) -> dict[str, Any]:
    return {
        "kind": "frame",
        "feed": feed,
        "mono_ns": int(t_s * 1e9),
        "wall_ns": int((WS + t_s) * 1e9),
        "payload": json.dumps(payload, separators=(",", ":")),
    }


def _price_at(t_s: float) -> float:
    """Flat near K for most of the window, ramping up in the last minute."""
    if t_s < 240.0:
        return K_PRICE + (2.0 if int(t_s) % 2 == 0 else -2.0)
    ramp = min((t_s - 240.0) / 55.0, 1.0)
    return K_PRICE + ramp * (FINAL_PRICE - K_PRICE)


def build_frames() -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []

    def book_event(token: str, bid: float, ask: float, t_s: float) -> dict[str, Any]:
        return {
            "event_type": "book",
            "asset_id": token,
            "bids": [{"price": f"{bid:.3f}", "size": "500"}],
            "asks": [{"price": f"{ask:.3f}", "size": "400"}],
            "hash": f"h{t_s}",
        }

    # Initial books shortly after the window opens.
    frames.append(_frame("clob_market", 0.5, book_event(UP, 0.49, 0.51, 0.5)))
    frames.append(_frame("clob_market", 0.6, book_event(DOWN, 0.48, 0.50, 0.6)))

    t = 0.05
    while t < 301.0:
        px = _price_at(t)
        frames.append(
            _frame(
                "rtds_chainlink",
                t,
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "update",
                    "timestamp": int((WS + t) * 1000),
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp": int((WS + t) * 1000),
                        "value": round(px, 2),
                    },
                },
            )
        )
        t += 1.0

    t = 0.2
    while t < 301.0:
        px = _price_at(t)
        frames.append(
            _frame(
                "binance",
                t,
                {
                    "stream": "btcusdt@bookTicker",
                    "data": {
                        "u": int(t * 10),
                        "s": "BTCUSDT",
                        "b": f"{px - 1:.2f}",
                        "B": "5",
                        "a": f"{px + 1:.2f}",
                        "A": "5",
                    },
                },
            )
        )
        t += 0.4

    # The market maker on Polymarket rationally reprices UP as BTC ramps —
    # the last reprice (just before T-arm) leaves an ask at 0.94 standing
    # through the armed window: the sniper's target.
    frames.append(_frame("clob_market", 250.0, book_event(UP, 0.80, 0.83, 250.0)))
    frames.append(_frame("clob_market", 279.5, book_event(UP, 0.92, 0.94, 279.5)))
    frames.append(_frame("clob_market", 279.6, book_event(DOWN, 0.05, 0.07, 279.6)))

    frames.sort(key=lambda fr: fr["mono_ns"])
    return frames


def write_recording(raw_dir: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    frames = build_frames()
    with (raw_dir / "0.jsonl").open("w") as fh:
        for fr in frames:
            fh.write(json.dumps(fr, separators=(",", ":")) + "\n")


def write_meta_recording(dec_dir: Path) -> None:
    dec_dir.mkdir(parents=True, exist_ok=True)
    with (dec_dir / "0.jsonl").open("w") as fh:
        for ws in (WS, WS + 300):
            doc = {
                "kind": "market_meta",
                "feed": "engine",
                "mono_ns": 0,
                "wall_ns": 0,
                "payload": asdict(meta_for(ws)),
            }
            fh.write(json.dumps(doc, separators=(",", ":")) + "\n")
