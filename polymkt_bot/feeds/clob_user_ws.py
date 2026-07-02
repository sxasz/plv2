"""CLOB user channel: authenticated order lifecycle + fill events.

An ACK from order placement is NOT a fill (spec §3). Fills are recognized
only here (or by REST trade reconciliation). Event schemas are
runtime-verify (FACTS.md #4.5): parsing is defensive and raw frames are
recorded for M1 validation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp
import orjson

from ..clock import mono_ns
from ..types import Fill
from .base import WsFeed

log = logging.getLogger(__name__)


class ClobUserFeed(WsFeed):
    name = "clob_user"
    ping_interval_s = 10.0
    ping_payload = "PING"

    def __init__(
        self,
        url: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        **kw: object,
    ) -> None:
        super().__init__(url, **kw)  # type: ignore[arg-type]
        self._auth = {"apiKey": api_key, "secret": api_secret, "passphrase": api_passphrase}
        self.markets: list[str] = []  # condition ids
        self.on_fill: Callable[[Fill], None] | None = None
        self.on_order_event: Callable[[dict[str, Any]], None] | None = None

    def track_market(self, condition_id: str) -> None:
        if condition_id not in self.markets:
            self.markets.append(condition_id)
            self.markets = self.markets[-8:]  # active + recent windows only

    async def on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await self.send_json({"type": "user", "markets": self.markets, "auth": self._auth})

    def on_message(self, raw: str, recv_mono_ns: int, recv_wall_ns: int) -> None:
        doc = orjson.loads(raw)
        events = doc if isinstance(doc, list) else [doc]
        for ev in events:
            if isinstance(ev, dict):
                self._handle(ev, recv_mono_ns)

    def _handle(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        et = ev.get("event_type", ev.get("type", ""))
        if et == "trade":
            self._handle_trade(ev, recv_mono_ns)
        elif et == "order":
            if self.on_order_event is not None:
                self.on_order_event(ev)

    def _handle_trade(self, ev: dict[str, Any], recv_mono_ns: int) -> None:
        """Map a user-channel trade event to Fill(s) for our orders.

        A trade event carries maker_orders and/or taker order details; fee
        fields are taken from the event itself (FACTS.md #2.4), never computed
        locally.
        """
        if self.on_fill is None:
            return
        try:
            for leg in self._extract_legs(ev):
                self.on_fill(leg)
        except (KeyError, TypeError, ValueError):
            log.exception("bad trade event", extra={"ctx": {"ev_keys": sorted(ev)}})

    def _extract_legs(self, ev: dict[str, Any]) -> list[Fill]:
        legs: list[Fill] = []
        ts_ms = int(ev.get("match_time", ev.get("timestamp", 0)) or 0)
        status = str(ev.get("status", ""))
        # Fills counted at first MATCHED status; later MINED/CONFIRMED updates
        # for the same trade id are deduplicated by the order manager.
        if status and status.upper() not in ("MATCHED", "MINED", "CONFIRMED"):
            return legs
        trade_id = str(ev.get("id", ""))
        own_order = str(ev.get("taker_order_id", "") or ev.get("order_id", ""))
        if own_order:
            legs.append(
                Fill(
                    order_id=own_order,
                    token_id=str(ev.get("asset_id", "")),
                    side="BUY" if str(ev.get("side", "")).upper() == "BUY" else "SELL",
                    price=float(ev.get("price", 0) or 0),
                    size=float(ev.get("size", 0) or 0),
                    fee_shares=float(ev.get("fee_rate_bps_charged_shares", 0) or 0),
                    fee_usdc=float(ev.get("fee", 0) or 0),
                    exchange_ts_ms=ts_ms,
                    recv_mono_ns=mono_ns(),
                    trade_id=trade_id,
                )
            )
        for mo in ev.get("maker_orders", []) or []:
            if not isinstance(mo, dict):
                continue
            legs.append(
                Fill(
                    order_id=str(mo.get("order_id", "")),
                    token_id=str(mo.get("asset_id", ev.get("asset_id", ""))),
                    side="BUY" if str(mo.get("side", "")).upper() == "BUY" else "SELL",
                    price=float(mo.get("price", 0) or 0),
                    size=float(mo.get("matched_amount", mo.get("size", 0)) or 0),
                    fee_shares=0.0,  # makers pay zero (FACTS.md #2.5)
                    fee_usdc=float(mo.get("fee", 0) or 0),
                    exchange_ts_ms=ts_ms,
                    recv_mono_ns=mono_ns(),
                    trade_id=trade_id,
                )
            )
        return legs
