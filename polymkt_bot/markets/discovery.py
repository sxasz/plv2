"""Market discovery: deterministic slug → Gamma/CLOB metadata (spec §3).

Primary path is the deterministic slug `btc-updown-5m-{window_start}`
(FACTS.md #1.2); fallback is a Gamma event lookup. The next window's
metadata is prefetched at T−prefetch_seconds so the rollover hot path never
touches REST. Fee rate / tick size / neg-risk come from the CLOB per token —
a market whose fee params could not be fetched has fees_known=False and is
unarmable.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from ..clock import slug_for_window
from ..types import MarketMeta
from .rest import RestClient

log = logging.getLogger(__name__)


def _parse_json_field(value: Any) -> list[Any]:
    """Gamma stringifies list fields (e.g. clobTokenIds, outcomes)."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = orjson.loads(value)
            return parsed if isinstance(parsed, list) else []
        except orjson.JSONDecodeError:
            return []
    return []


class MarketDiscovery:
    def __init__(self, rest: RestClient, window_len_s: int = 300) -> None:
        self.rest = rest
        self.window_len_s = window_len_s
        self._cache: dict[int, MarketMeta] = {}

    async def resolve(self, window_start: int) -> MarketMeta | None:
        cached = self._cache.get(window_start)
        if cached is not None:
            return cached
        slug = slug_for_window(window_start)
        market = await self._fetch_gamma_market(slug)
        if market is None:
            log.warning("market not found", extra={"ctx": {"slug": slug}})
            return None
        meta = await self._build_meta(window_start, slug, market)
        if meta is not None:
            self._cache[window_start] = meta
            if len(self._cache) > 16:
                for key in sorted(self._cache)[:-8]:
                    del self._cache[key]
        return meta

    async def _fetch_gamma_market(self, slug: str) -> dict[str, Any] | None:
        markets = await self.rest.gamma_markets_by_slug(slug)
        if markets:
            return markets[0]
        # Fallback: the slug may be an event slug wrapping a single market.
        events = await self.rest.gamma_events_by_slug(slug)
        for ev in events:
            mkts = ev.get("markets")
            if isinstance(mkts, list) and mkts:
                return mkts[0]  # type: ignore[no-any-return]
        return None

    async def _build_meta(
        self, window_start: int, slug: str, market: dict[str, Any]
    ) -> MarketMeta | None:
        token_ids = [str(t) for t in _parse_json_field(market.get("clobTokenIds"))]
        outcomes = [str(o) for o in _parse_json_field(market.get("outcomes"))]
        condition_id = str(market.get("conditionId", market.get("condition_id", "")))
        if len(token_ids) != 2 or len(outcomes) != 2 or not condition_id:
            log.error(
                "malformed market object",
                extra={"ctx": {"slug": slug, "keys": sorted(market)}},
            )
            return None
        up_idx = _up_index(outcomes)
        if up_idx is None:
            log.error("cannot identify UP outcome", extra={"ctx": {"outcomes": outcomes}})
            return None
        up_token = token_ids[up_idx]
        down_token = token_ids[1 - up_idx]

        # Live params from CLOB — never hardcoded, never from config.
        tick = await self.rest.get_tick_size(up_token)
        neg_risk = await self.rest.get_neg_risk(up_token)
        fee_bps = await self.rest.get_fee_rate_bps(up_token)
        fee_bps_down = await self.rest.get_fee_rate_bps(down_token)
        fees_known = fee_bps is not None and fee_bps_down is not None
        if fees_known and fee_bps != fee_bps_down:
            log.error(
                "fee rate differs across outcome tokens — refusing",
                extra={"ctx": {"up": fee_bps, "down": fee_bps_down}},
            )
            fees_known = False

        min_size = _float_field(market, "orderMinSize", default=0.0)
        if tick is None:
            tick = _float_field(market, "orderPriceMinTickSize", default=0.0)
            fees_known = fees_known and tick > 0.0

        return MarketMeta(
            window_start=window_start,
            window_close=window_start + self.window_len_s,
            slug=slug,
            condition_id=condition_id,
            up_token=up_token,
            down_token=down_token,
            tick_size=tick or 0.0,
            min_order_size=min_size,
            neg_risk=bool(neg_risk) if neg_risk is not None else False,
            fee_rate_bps=fee_bps if fee_bps is not None else -1,
            fees_known=fees_known and (tick or 0.0) > 0.0,
        )


def _up_index(outcomes: list[str]) -> int | None:
    lowered = [o.strip().lower() for o in outcomes]
    if "up" in lowered:
        return lowered.index("up")
    if "yes" in lowered:  # defensive: some binary markets use Yes/No
        return lowered.index("yes")
    return None


def _float_field(doc: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(doc.get(key, default) or default)
    except (TypeError, ValueError):
        return default
