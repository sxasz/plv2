"""Live CLOB exchange adapter (M3) — wraps the official py-clob-client.

SAFETY INTERLOCK: this class refuses to construct unless
  1. config.run.shadow_mode is False, AND
  2. ENABLE_LIVE_TRADING in .env equals the explicit acknowledgement string.
Per the milestone plan (spec §12), it must not be enabled before the M2
Edge Realization Report is signed off.

Implementation notes:
- py-clob-client is synchronous (requests); calls run in the default
  executor so the event loop never blocks (spec §10). The extra thread-hop
  latency is measured by the sent→ack histogram; if it matters, M4 replaces
  this with a raw aiohttp + py-order-utils signer and a pre-signed ladder
  (price is baked into the EIP-712 signature — FACTS.md #3.5).
- Fee rate for the signed order comes from the client's own /fee-rate
  resolution (FACTS.md #2.3); we pass none and let it resolve.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config import Config
from .order_manager import ManagedOrder, PlaceResult

log = logging.getLogger(__name__)

_ACK_STRING = "YES_I_ACCEPT_REAL_LOSSES"


class LiveTradingBlocked(RuntimeError):
    pass


class LiveClobExchange:
    """ExchangeAdapter implementation for real orders at minimum stakes."""

    def __init__(self, cfg: Config) -> None:
        if cfg.run.shadow_mode:
            raise LiveTradingBlocked("shadow_mode is enabled in config")
        if cfg.secrets.enable_live_trading != _ACK_STRING:
            raise LiveTradingBlocked(
                "ENABLE_LIVE_TRADING interlock not set — live trading requires the "
                "M2 GO decision and an explicit acknowledgement in .env"
            )
        if not cfg.secrets.private_key or not cfg.secrets.api_key:
            raise LiveTradingBlocked("missing POLYMARKET_* credentials in .env")
        self.cfg = cfg
        self._client: Any = None
        self._signed: dict[str, Any] = {}  # client_id → signed order

    def _ensure_client(self) -> Any:
        if self._client is None:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.cfg.secrets.api_key,
                api_secret=self.cfg.secrets.api_secret,
                api_passphrase=self.cfg.secrets.api_passphrase,
            )
            self._client = ClobClient(
                host=self.cfg.endpoints.clob_rest,
                chain_id=137,  # Polygon (FACTS.md #3.1)
                key=self.cfg.secrets.private_key,
                creds=creds,
                funder=self.cfg.secrets.funder_address or None,
                signature_type=2 if self.cfg.secrets.funder_address else 0,
            )
        return self._client

    async def sign(self, order: ManagedOrder) -> None:
        client = self._ensure_client()

        def _sign() -> Any:
            from py_clob_client.clob_types import OrderArgs

            return client.create_order(
                OrderArgs(
                    token_id=order.token_id,
                    price=order.price,
                    size=order.size,
                    side=order.side,
                )
            )

        self._signed[order.client_id] = await asyncio.get_running_loop().run_in_executor(
            None, _sign
        )

    async def place(self, order: ManagedOrder) -> PlaceResult:
        client = self._ensure_client()
        signed = self._signed.pop(order.client_id, None)
        if signed is None:
            return PlaceResult(ok=False, error="order was not signed")
        tif = order.tif

        def _post() -> Any:
            from py_clob_client.clob_types import OrderType as ClobOrderType

            return client.post_order(signed, orderType=ClobOrderType(tif.value))

        try:
            resp = await asyncio.get_running_loop().run_in_executor(None, _post)
        except Exception as exc:  # py-clob-client raises library-specific errors
            log.error("post_order failed", extra={"ctx": {"err": repr(exc)}})
            return PlaceResult(ok=False, error=repr(exc))
        if isinstance(resp, dict) and resp.get("success"):
            return PlaceResult(ok=True, exchange_id=str(resp.get("orderID", "")))
        return PlaceResult(ok=False, error=str(resp))

    async def cancel(self, order: ManagedOrder) -> bool:
        if not order.exchange_id:
            return False
        client = self._ensure_client()

        def _cancel() -> Any:
            return client.cancel(order_id=order.exchange_id)

        try:
            await asyncio.get_running_loop().run_in_executor(None, _cancel)
            return True
        except Exception as exc:
            log.error("cancel failed", extra={"ctx": {"err": repr(exc)}})
            return False

    async def cancel_all(self, session_slug: str) -> None:
        # Cancel-all is global on the CLOB; per-session isolation is handled
        # by only ever having one active window's orders open (spec §3).
        client = self._ensure_client()

        def _cancel_all() -> Any:
            return client.cancel_all()

        try:
            await asyncio.get_running_loop().run_in_executor(None, _cancel_all)
        except Exception as exc:
            log.error("cancel_all failed", extra={"ctx": {"err": repr(exc)}})
