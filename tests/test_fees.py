"""Fee math vs published examples (spec §10)."""

import pytest

from polymkt_bot.exec.fees import FeeModel
from polymkt_bot.types import MarketMeta


def test_peak_fee_matches_published_example(fee_model: FeeModel) -> None:
    # Published: crypto ≈ $1.80 per 100 shares at 50¢ (FACTS.md #2.2) → 720 bps.
    assert fee_model.taker_fee_usd(price=0.50, size=100) == pytest.approx(1.80)


def test_fee_curve_decays_toward_extremes(fee_model: FeeModel) -> None:
    # The bell curve is why late-window extreme entries are cheap (spec §2.2).
    assert fee_model.fee_per_share(0.50) == pytest.approx(0.018)
    assert fee_model.fee_per_share(0.90) == pytest.approx(0.00648)
    assert fee_model.fee_per_share(0.95) == pytest.approx(0.00342)
    assert fee_model.fee_per_share(0.99) < 0.001
    # Symmetric.
    assert fee_model.fee_per_share(0.10) == pytest.approx(fee_model.fee_per_share(0.90))


def test_buy_fee_in_shares_proceeds_convention(fee_model: FeeModel) -> None:
    assert fee_model.buy_fee_shares(0.50, 100) == pytest.approx(1.80)
    assert fee_model.maker_fee_usd(0.5, 100) == 0.0


def test_degenerate_prices_pay_zero(fee_model: FeeModel) -> None:
    assert fee_model.fee_per_share(0.0) == 0.0
    assert fee_model.fee_per_share(1.0) == 0.0


def test_unknown_fees_make_market_untradeable(meta: MarketMeta) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError):
        FeeModel(replace(meta, fees_known=False))
    with pytest.raises(ValueError):
        FeeModel(replace(meta, fee_rate_bps=-1, fees_known=True))
