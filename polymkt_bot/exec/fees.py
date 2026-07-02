"""Fee math from live-queried params (FACTS.md §2). Nothing here is hardcoded:
the rate comes from the CLOB `/fee-rate` endpoint via MarketMeta, and a
market without known fee params is untradeable (risk gate).

Formula (FACTS.md #2.1): fee_per_share_usd(p) = rate × p × (1 − p),
rate = base_fee_bps / 10_000. Peak at p=0.5. Published example: crypto
category ⇒ $1.80 per 100 shares at 50¢ ⇒ base_fee ≈ 720 bps — used as a
sanity cross-check in verify_facts, never as a value.

Denomination (FACTS.md #2.4): fees are charged on proceeds — shares on BUY,
collateral on SELL. Whether the share-denominated buy fee equals
fee_usd/price or fee_usd/1.0 is runtime-verified from real fill events; the
shadow exchange takes it as a parameter and real accounting always uses the
fill event's own fee fields.
"""

from __future__ import annotations

from ..types import MarketMeta


class FeeModel:
    def __init__(self, meta: MarketMeta) -> None:
        if not meta.fees_known or meta.fee_rate_bps < 0:
            raise ValueError(f"fee params unknown for {meta.slug} — market is untradeable")
        self.rate = meta.fee_rate_bps / 10_000.0

    def fee_per_share(self, price: float) -> float:
        """Taker fee per share in USD at trade price p."""
        if not 0.0 < price < 1.0:
            return 0.0
        return self.rate * price * (1.0 - price)

    def taker_fee_usd(self, price: float, size: float) -> float:
        return self.fee_per_share(price) * size

    def buy_fee_shares(self, price: float, size: float) -> float:
        """Share-denominated buy fee under the proceeds convention:
        fee = size × rate × p × (1−p) shares (FACTS.md #2.4, runtime-verify)."""
        if not 0.0 < price < 1.0:
            return 0.0
        return size * self.rate * price * (1.0 - price)

    # Makers pay zero (FACTS.md #2.5); rebates are ignored (conservative).
    @staticmethod
    def maker_fee_usd(price: float, size: float) -> float:
        return 0.0
