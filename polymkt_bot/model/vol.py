"""EWMA per-second realized volatility over irregularly-sampled prices.

σ is estimated on log returns of the Chainlink stream (fallback: Binance),
normalized to per-second variance, with a configurable half-life
(spec §2.1). A floor keeps P_up well-defined in dead tape.
"""

from __future__ import annotations

import math


class EwmaVol:
    def __init__(self, halflife_s: float, floor_per_s: float = 1e-6) -> None:
        if halflife_s <= 0:
            raise ValueError("halflife must be positive")
        self.halflife_s = halflife_s
        self.floor = floor_per_s
        self._var_per_s: float = 0.0  # EWMA of r^2/dt
        self._last_px: float = 0.0
        self._last_ts_s: float = 0.0
        self.n_samples = 0

    def update(self, price: float, ts_s: float) -> None:
        if price <= 0.0:
            return
        if self._last_px > 0.0:
            dt = ts_s - self._last_ts_s
            if dt <= 0.0:
                return  # out-of-order or duplicate print
            r = math.log(price / self._last_px)
            inst_var = (r * r) / dt
            w = 0.5 ** (dt / self.halflife_s)
            if self.n_samples == 0:
                self._var_per_s = inst_var
            else:
                self._var_per_s = w * self._var_per_s + (1.0 - w) * inst_var
            self.n_samples += 1
        self._last_px = price
        self._last_ts_s = ts_s

    @property
    def sigma_rel_per_s(self) -> float:
        """Relative (fractional) 1-second vol, floored."""
        return max(math.sqrt(self._var_per_s), self.floor)

    def sigma_price_per_s(self, spot: float) -> float:
        """Vol in price units for the current spot level."""
        return self.sigma_rel_per_s * spot

    @property
    def warmed_up(self) -> bool:
        # ~5 minutes of prints at 1/s; enough for a first σ that isn't noise.
        return self.n_samples >= 60
