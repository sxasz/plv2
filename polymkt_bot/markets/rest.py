"""Thin async REST client for Gamma + CLOB metadata/snapshot endpoints.

REST is used only for: market discovery, fee/tick/neg-risk params, book
snapshot resync, and order/balance reconciliation. Never for prices on the
hot path (spec §4).
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import orjson

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10.0)


class RestClient:
    def __init__(self, clob_base: str, gamma_base: str) -> None:
        self.clob = clob_base.rstrip("/")
        self.gamma = gamma_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, url: str, params: dict[str, str] | None = None) -> Any | None:
        if self._session is None:
            raise RuntimeError("RestClient not started")
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("rest non-200", extra={"ctx": {"url": url, "status": resp.status}})
                    return None
                return orjson.loads(await resp.read())
        except (TimeoutError, aiohttp.ClientError, orjson.JSONDecodeError) as exc:
            log.warning("rest error", extra={"ctx": {"url": url, "err": repr(exc)}})
            return None

    # -- Gamma ----------------------------------------------------------------
    async def gamma_markets_by_slug(self, slug: str) -> list[dict[str, Any]]:
        doc = await self._get(f"{self.gamma}/markets", params={"slug": slug})
        return doc if isinstance(doc, list) else []

    async def gamma_events_by_slug(self, slug: str) -> list[dict[str, Any]]:
        doc = await self._get(f"{self.gamma}/events", params={"slug": slug})
        return doc if isinstance(doc, list) else []

    # -- CLOB -----------------------------------------------------------------
    async def get_book(self, token_id: str) -> tuple[list[Any], list[Any]] | None:
        doc = await self._get(f"{self.clob}/book", params={"token_id": token_id})
        if isinstance(doc, dict) and "bids" in doc and "asks" in doc:
            return doc["bids"] or [], doc["asks"] or []
        return None

    async def get_tick_size(self, token_id: str) -> float | None:
        doc = await self._get(f"{self.clob}/tick-size", params={"token_id": token_id})
        if isinstance(doc, dict) and "minimum_tick_size" in doc:
            return float(doc["minimum_tick_size"])
        return None

    async def get_neg_risk(self, token_id: str) -> bool | None:
        doc = await self._get(f"{self.clob}/neg-risk", params={"token_id": token_id})
        if isinstance(doc, dict) and "neg_risk" in doc:
            return bool(doc["neg_risk"])
        return None

    async def get_fee_rate_bps(self, token_id: str) -> int | None:
        """Per-token taker fee rate in bps — FACTS.md #2.3. Never hardcoded."""
        doc = await self._get(f"{self.clob}/fee-rate", params={"token_id": token_id})
        if isinstance(doc, dict) and "base_fee" in doc:
            return int(doc["base_fee"])
        return None

    async def get_clob_market(self, condition_id: str) -> dict[str, Any] | None:
        doc = await self._get(f"{self.clob}/markets/{condition_id}")
        return doc if isinstance(doc, dict) else None

    async def get_server_time(self) -> float | None:
        doc = await self._get(f"{self.clob}/time")
        try:
            return float(doc)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
