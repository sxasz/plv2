"""Fair-value model: P_up projection and fee-aware edge (spec §2.1–2.2).

    Ŝ  = S_cl + λ·(B_now − B_at_last_cl_print)
    P  = Φ((Ŝ − K) / (σ_price·√τ + ε_basis))

ε_basis is the rolling std of the Binance↔Chainlink basis — the uncertainty
about where the *oracle* will print given where Binance is. The tie rule
(close == open → UP) earns a small structural bonus toward UP when |Ŝ−K| is
inside oracle tick noise.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .vol import EwmaVol


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class BasisTracker:
    """Rolling Binance-mid ↔ Chainlink basis (price units)."""

    def __init__(self, window_s: float = 600.0) -> None:
        self.window_s = window_s
        self._samples: deque[tuple[float, float]] = deque()  # (ts_s, basis)

    def update(self, ts_s: float, chainlink_px: float, binance_mid: float) -> None:
        if chainlink_px <= 0.0 or binance_mid <= 0.0:
            return
        self._samples.append((ts_s, chainlink_px - binance_mid))
        cutoff = ts_s - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def n(self) -> int:
        return len(self._samples)

    @property
    def mean(self) -> float:
        if not self._samples:
            return 0.0
        return sum(b for _, b in self._samples) / len(self._samples)

    @property
    def std(self) -> float:
        n = len(self._samples)
        if n < 8:
            return float("inf")  # not enough data to trust the basis → block ties
        m = self.mean
        var = sum((b - m) ** 2 for _, b in self._samples) / (n - 1)
        return math.sqrt(var)


@dataclass(slots=True)
class FairValue:
    """One evaluation of the model at a decision tick."""

    s_hat: float
    p_up: float
    sigma_price_per_s: float
    basis_std: float
    denom: float
    dist: float  # Ŝ − K
    in_tie_band: bool


class FairValueModel:
    def __init__(
        self,
        vol: EwmaVol,
        basis: BasisTracker,
        lambda_lead: float = 1.0,
        p_clip: float = 0.001,
        tie_bonus: float = 0.005,
        k_tie: float = 1.0,
        oracle_tick_noise: float = 1.0,  # $ — refined from recorded data in M2
    ) -> None:
        self.vol = vol
        self.basis = basis
        self.lambda_lead = lambda_lead
        self.p_clip = p_clip
        self.tie_bonus = tie_bonus
        self.k_tie = k_tie
        self.oracle_tick_noise = oracle_tick_noise
        # Binance mid at the moment of the last Chainlink print (for Ŝ).
        self._b_at_last_cl: float = 0.0
        self._s_cl: float = 0.0

    def on_chainlink_print(self, value: float, binance_mid_now: float) -> None:
        self._s_cl = value
        if binance_mid_now > 0.0:
            self._b_at_last_cl = binance_mid_now

    def evaluate(self, k: float, binance_mid_now: float, tau_s: float) -> FairValue | None:
        """P_up for the active window; None if inputs are unusable."""
        if self._s_cl <= 0.0 or k <= 0.0 or tau_s < 0.0:
            return None
        s_hat = self._s_cl
        if self._b_at_last_cl > 0.0 and binance_mid_now > 0.0:
            s_hat += self.lambda_lead * (binance_mid_now - self._b_at_last_cl)

        sigma_px = self.vol.sigma_price_per_s(s_hat)
        basis_std = self.basis.std
        eps_basis = basis_std if math.isfinite(basis_std) else float("inf")
        denom = sigma_px * math.sqrt(max(tau_s, 1e-3)) + eps_basis
        dist = s_hat - k
        if not math.isfinite(denom) or denom <= 0.0:
            return None

        p = norm_cdf(dist / denom)
        in_tie_noise = abs(dist) < self.oracle_tick_noise
        if in_tie_noise:
            p += self.tie_bonus  # tie resolves UP (FACTS.md #1.3)
        p = min(max(p, self.p_clip), 1.0 - self.p_clip)

        # No-trade tie band: |Ŝ−K| < k_tie·(σ√τ + basis_std)  (spec §2.3)
        in_tie_band = abs(dist) < self.k_tie * denom

        return FairValue(
            s_hat=s_hat,
            p_up=p,
            sigma_price_per_s=sigma_px,
            basis_std=basis_std,
            denom=denom,
            dist=dist,
            in_tie_band=in_tie_band,
        )


def edge_for_taker_buy(
    p_model: float, ask: float, fee_per_share: float, slippage_buffer: float
) -> float:
    """Net expected edge per share for buying at the ask (spec §2.2)."""
    return p_model - ask - fee_per_share - slippage_buffer
