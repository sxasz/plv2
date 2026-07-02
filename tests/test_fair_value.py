"""Fair-value model: P_up behavior, basis gating, vol estimator."""

from __future__ import annotations

import pytest

from polymkt_bot.model.fair_value import BasisTracker, FairValueModel, norm_cdf
from polymkt_bot.model.vol import EwmaVol


def warmed_model(lambda_lead: float = 1.0) -> FairValueModel:
    vol = EwmaVol(halflife_s=600, floor_per_s=1e-6)
    px, ts = 100_000.0, 0.0
    for i in range(120):  # alternating ±10$ prints, 1s apart
        px += 10.0 if i % 2 == 0 else -10.0
        ts += 1.0
        vol.update(px, ts)
    basis = BasisTracker(600.0)
    for i in range(20):
        basis.update(float(i), 100_000.0 + (i % 3), 100_000.0)
    model = FairValueModel(vol, basis, lambda_lead=lambda_lead)
    model.on_chainlink_print(100_000.0, binance_mid_now=100_000.0)
    return model


def test_pup_above_half_when_above_strike() -> None:
    model = warmed_model()
    fv = model.evaluate(k=99_900.0, binance_mid_now=100_000.0, tau_s=10.0)
    assert fv is not None
    assert fv.p_up > 0.5


def test_pup_monotonic_in_distance() -> None:
    model = warmed_model()
    ps = []
    for k in (100_100.0, 100_000.0, 99_900.0):
        fv = model.evaluate(k=k, binance_mid_now=100_000.0, tau_s=10.0)
        assert fv is not None
        ps.append(fv.p_up)
    assert ps[0] < ps[1] < ps[2]


def test_binance_lead_projection() -> None:
    model = warmed_model(lambda_lead=1.0)
    # Binance jumped +50 since the last oracle print → Ŝ projects +50.
    fv = model.evaluate(k=100_000.0, binance_mid_now=100_050.0, tau_s=10.0)
    assert fv is not None
    assert fv.s_hat == pytest.approx(100_050.0)
    fv0 = model.evaluate(k=100_000.0, binance_mid_now=100_000.0, tau_s=10.0)
    assert fv0 is not None
    assert fv.p_up > fv0.p_up


def test_no_basis_data_blocks_evaluation() -> None:
    vol = EwmaVol(600)
    for i in range(120):
        vol.update(100_000 + (i % 2) * 10, float(i))
    model = FairValueModel(vol, BasisTracker(600.0))  # empty basis → std inf
    model.on_chainlink_print(100_000.0, 100_000.0)
    assert model.evaluate(k=100_000.0, binance_mid_now=100_000.0, tau_s=10.0) is None


def test_clipping_and_tie_band() -> None:
    model = warmed_model()
    fv = model.evaluate(k=90_000.0, binance_mid_now=100_000.0, tau_s=5.0)
    assert fv is not None
    assert fv.p_up <= 0.999
    near = model.evaluate(k=100_000.4, binance_mid_now=100_000.0, tau_s=5.0)
    assert near is not None
    assert near.in_tie_band  # |dist| well inside k_tie·denom


def test_more_time_means_more_uncertainty() -> None:
    model = warmed_model()
    fv_short = model.evaluate(k=99_950.0, binance_mid_now=100_000.0, tau_s=2.0)
    fv_long = model.evaluate(k=99_950.0, binance_mid_now=100_000.0, tau_s=250.0)
    assert fv_short is not None and fv_long is not None
    assert fv_short.p_up > fv_long.p_up  # same lead shrinks with more τ


def test_norm_cdf_sanity() -> None:
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_cdf(3.0) > 0.99
    assert norm_cdf(-3.0) < 0.01


def test_vol_floor_on_dead_tape() -> None:
    vol = EwmaVol(600, floor_per_s=1e-6)
    for i in range(100):
        vol.update(100_000.0, float(i))  # constant price
    assert vol.sigma_rel_per_s == pytest.approx(1e-6)


def test_vol_ignores_out_of_order_prints() -> None:
    vol = EwmaVol(600)
    vol.update(100.0, 10.0)
    vol.update(101.0, 9.0)  # stale print — must not poison the estimate
    assert vol.n_samples == 0
