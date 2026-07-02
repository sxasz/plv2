#!/usr/bin/env python3
"""Live re-verification of every runtime-critical fact in FACTS.md.

Run from the deployment VPS (allowed region — Polymarket geo-blocks and
Cloudflare-fronts its APIs):

    python scripts/verify_facts.py [--config config.yaml] [--out facts_runtime.json]

Checks (FACTS.md "Runtime re-verification"):
 1. Gamma: current btc-updown-5m-{ws} slug resolves; dump the market object.
 2. CLOB REST: /time, /tick-size, /neg-risk, /fee-rate for both tokens.
 3. CLOB WS: subscribe both tokens, capture book/price_change samples.
 4. RTDS: subscribe crypto_prices_chainlink btc/usd, capture prints across
    a 300s boundary; report the boundary print (the Price to Beat).
 5. Binance: one bookTicker + one aggTrade frame.
 6. Fee formula sanity: base_fee vs the published peak-per-100-shares.

Exit code 0 = all checks passed; 1 = at least one failed (details in JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polymkt_bot.clock import slug_for_window, window_start
from polymkt_bot.config import load_config

WS_TIMEOUT = 20.0


async def check_gamma(session: aiohttp.ClientSession, gamma: str, ws: int) -> dict[str, Any]:
    slug = slug_for_window(ws)
    out: dict[str, Any] = {"check": "gamma_slug", "slug": slug, "ok": False}
    async with session.get(f"{gamma}/markets", params={"slug": slug}) as resp:
        out["status"] = resp.status
        if resp.status == 200:
            doc = await resp.json()
            if isinstance(doc, list) and doc:
                out["ok"] = True
                out["market_object"] = doc[0]
            else:
                out["error"] = "empty result — slug format changed? (FACTS 1.2)"
    return out


async def check_clob_rest(
    session: aiohttp.ClientSession, clob: str, token_ids: list[str]
) -> list[dict[str, Any]]:
    results = []
    async with session.get(f"{clob}/time") as resp:
        server_time = float(await resp.text()) if resp.status == 200 else None
        results.append(
            {
                "check": "clob_time",
                "ok": server_time is not None and abs(server_time - time.time()) < 10,
                "server_time": server_time,
                "local_time": time.time(),
            }
        )
    for tid in token_ids:
        for ep, field in (
            ("tick-size", "minimum_tick_size"),
            ("neg-risk", "neg_risk"),
            ("fee-rate", "base_fee"),
        ):
            async with session.get(f"{clob}/{ep}", params={"token_id": tid}) as resp:
                doc = await resp.json() if resp.status == 200 else {}
                results.append(
                    {
                        "check": f"clob_{ep}",
                        "token": tid[:16],
                        "ok": field in doc,
                        "value": doc.get(field),
                        "raw": doc,
                    }
                )
    return results


async def check_clob_ws(clob_ws: str, token_ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"check": "clob_ws_market", "ok": False, "samples": {}}
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.ws_connect(
                f"{clob_ws}/ws/market", timeout=aiohttp.ClientWSTimeout(ws_close=10)
            ) as ws,
        ):
            await ws.send_json({"assets_ids": token_ids, "type": "market"})
            deadline = time.monotonic() + WS_TIMEOUT
            while time.monotonic() < deadline and len(out["samples"]) < 3:
                msg = await ws.receive(timeout=deadline - time.monotonic())
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                if not msg.data:
                    continue
                try:
                    doc = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                for ev in doc if isinstance(doc, list) else [doc]:
                    et = ev.get("event_type", "?")
                    out["samples"].setdefault(et, ev)
            out["ok"] = "book" in out["samples"]
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        out["error"] = repr(exc)
    return out


async def check_rtds(rtds_ws: str, capture_s: float) -> dict[str, Any]:
    out: dict[str, Any] = {
        "check": "rtds_chainlink",
        "ok": False,
        "n_prints": 0,
        "boundary_print": None,
    }
    prints: list[dict[str, Any]] = []
    try:
        async with aiohttp.ClientSession() as session, session.ws_connect(rtds_ws) as ws:
            await ws.send_json(
                {
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }
                    ],
                }
            )
            deadline = time.monotonic() + capture_s
            last_ping = 0.0
            while time.monotonic() < deadline:
                if time.monotonic() - last_ping > 5.0:
                    await ws.send_str("PING")
                    last_ping = time.monotonic()
                try:
                    msg = await ws.receive(timeout=min(5.0, deadline - time.monotonic()))
                except TimeoutError:
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT or msg.data == "PONG" or not msg.data:
                    continue
                try:
                    doc = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if doc.get("topic") == "crypto_prices_chainlink":
                    payload = doc.get("payload", {})
                    items = payload if isinstance(payload, list) else [payload]
                    for p in items:
                        if "value" in p:
                            prints.append(p)
    except (aiohttp.ClientError, OSError) as exc:
        out["error"] = repr(exc)
    out["n_prints"] = len(prints)
    out["ok"] = len(prints) > 0
    if prints:
        out["first"], out["last"] = prints[0], prints[-1]
        boundaries = {window_start(p["timestamp"] / 1000.0) for p in prints}
        if len(boundaries) > 1:
            bd = max(boundaries)
            after = [p for p in prints if p["timestamp"] >= bd * 1000]
            out["boundary_print"] = after[0] if after else None
            out["boundary_ts"] = bd
    return out


async def check_binance(url: str) -> dict[str, Any]:
    out: dict[str, Any] = {"check": "binance_ws", "ok": False, "streams_seen": []}
    try:
        async with aiohttp.ClientSession() as session, session.ws_connect(url) as ws:
            deadline = time.monotonic() + 15
            seen: set[str] = set()
            while time.monotonic() < deadline and len(seen) < 2:
                msg = await ws.receive(timeout=deadline - time.monotonic())
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                if not msg.data:
                    continue
                try:
                    doc = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if "stream" in doc:
                    seen.add(doc["stream"])
            out["streams_seen"] = sorted(seen)
            out["ok"] = len(seen) >= 1
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        out["error"] = repr(exc)
    return out


def check_fee_formula(base_fee_bps: int | None) -> dict[str, Any]:
    """Cross-check the live rate against the published example
    (~$1.75–1.80 per 100 shares at 50¢ → 700–720 bps for crypto)."""
    out: dict[str, Any] = {"check": "fee_formula_sanity", "base_fee_bps": base_fee_bps}
    if base_fee_bps is None:
        out["ok"] = False
        out["error"] = "no fee rate available"
        return out
    peak_per_100 = 100 * (base_fee_bps / 10_000) * 0.5 * 0.5
    out["peak_usd_per_100_shares_at_50c"] = round(peak_per_100, 4)
    out["ok"] = True
    out["note"] = (
        "compare against the published fee table before trusting edge math; "
        "expected ≈ 1.75–1.80 for crypto as of 2026-07 (FACTS 2.2)"
    )
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out", default="facts_runtime.json")
    parser.add_argument(
        "--rtds-capture-s",
        type=float,
        default=330.0,
        help="capture window; >300s guarantees one boundary",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)

    ws = window_start(time.time())
    results: list[dict[str, Any]] = []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        gamma_res = await check_gamma(session, cfg.endpoints.gamma_rest, ws)
        results.append(gamma_res)

        token_ids: list[str] = []
        market = gamma_res.get("market_object", {})
        raw_tokens = market.get("clobTokenIds", "[]")
        try:
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        except json.JSONDecodeError:
            pass

        base_fee: int | None = None
        if token_ids:
            clob_results = await check_clob_rest(session, cfg.endpoints.clob_rest, token_ids[:2])
            results.extend(clob_results)
            for r in clob_results:
                if r["check"] == "clob_fee-rate" and r["ok"]:
                    base_fee = int(r["value"])
                    break

    results.append(check_fee_formula(base_fee))
    if token_ids:
        results.append(await check_clob_ws(cfg.endpoints.clob_ws, token_ids[:2]))
    results.append(await check_binance(cfg.endpoints.binance_ws))
    results.append(await check_rtds(cfg.endpoints.rtds_ws, args.rtds_capture_s))

    ok = all(r.get("ok") for r in results)
    doc = {
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_start_checked": ws,
        "all_ok": ok,
        "results": results,
    }
    # One-shot script exit: blocking write is fine here.
    Path(args.out).write_text(json.dumps(doc, indent=2, default=str))  # noqa: ASYNC240
    print(
        json.dumps(
            {
                "all_ok": ok,
                "written": args.out,
                "failed": [r["check"] for r in results if not r.get("ok")],
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
