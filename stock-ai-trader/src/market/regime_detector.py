"""
Regime Detector - Market regime classification using HMM + VIX + SPY 200 EMA + credit spreads.

Classifies market conditions into DEFENSIVE, NEUTRAL, or AGGRESSIVE regimes
to guide position sizing and strategy selection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    DEFENSIVE = "DEFENSIVE"    # High volatility, bearish — reduce exposure
    NEUTRAL = "NEUTRAL"        # Normal conditions — standard allocation
    AGGRESSIVE = "AGGRESSIVE"  # Low vol, bullish — increase exposure


class RegimeDetector:
    """
    Multi-factor market regime detector.

    Combines:
    1. Hidden Markov Model (HMM) on SPY returns
    2. VIX level (fear gauge)
    3. SPY 200-day EMA trend
    4. Credit spread proxy (HYG vs TLT ratio change)
    """

    def __init__(
        self,
        vix_defensive: float = 25.0,
        vix_aggressive: float = 15.0,
        use_hmm: bool = True,
        hmm_states: int = 3,
    ) -> None:
        """
        Args:
            vix_defensive: VIX above this → defensive signal.
            vix_aggressive: VIX below this → aggressive signal.
            use_hmm: Whether to use HMM (requires hmmlearn).
            hmm_states: Number of HMM hidden states.
        """
        self._vix_defensive = vix_defensive
        self._vix_aggressive = vix_aggressive
        self._use_hmm = use_hmm
        self._hmm_states = hmm_states
        self._hmm_model = None
        self._last_regime: Optional[str] = None
        self._last_update: Optional[datetime] = None

    # ── VIX ──────────────────────────────────────────────────────────────

    def get_vix_level(self, vix_data: Optional[float] = None) -> float:
        """
        Get the current VIX level.

        Args:
            vix_data: Pre-fetched VIX value. If None, returns 0 (caller must provide).

        Returns:
            VIX level as float.
        """
        if vix_data is not None:
            return vix_data
        # In production, would fetch from market data provider
        logger.warning("No VIX data provided, returning 0.0")
        return 0.0

    def _vix_signal(self, vix: float) -> int:
        """
        VIX-based regime signal.

        Returns:
            -1 for defensive (high VIX), 0 for neutral, +1 for aggressive (low VIX)
        """
        if vix >= self._vix_defensive:
            return -1
        elif vix <= self._vix_aggressive:
            return 1
        return 0

    # ── SPY 200 EMA ─────────────────────────────────────────────────────

    def spy_above_200ema(
        self,
        spy_prices: Optional[pd.Series] = None,
    ) -> bool:
        """
        Check if SPY is above its 200-day EMA.

        Args:
            spy_prices: Series of SPY daily close prices (needs >= 200 data points).

        Returns:
            True if latest close > 200 EMA.
        """
        if spy_prices is None or len(spy_prices) < 200:
            logger.warning("Insufficient SPY data for 200 EMA calculation")
            return True  # Default to bullish if no data

        ema_200 = spy_prices.ewm(span=200, adjust=False).mean()
        return bool(spy_prices.iloc[-1] > ema_200.iloc[-1])

    def _trend_signal(self, spy_prices: Optional[pd.Series]) -> int:
        """
        Trend signal from SPY vs 200 EMA.

        Returns:
            +1 if above, -1 if below.
        """
        if spy_prices is None or len(spy_prices) < 200:
            return 0
        return 1 if self.spy_above_200ema(spy_prices) else -1

    # ── Credit Spread Proxy ──────────────────────────────────────────────

    def _credit_spread_signal(
        self,
        hyg_tlt_ratio: Optional[pd.Series] = None,
    ) -> int:
        """
        Credit spread proxy using HYG/TLT ratio.
        Rising ratio = risk-on (aggressive), falling = risk-off (defensive).

        Returns:
            +1 for risk-on, -1 for risk-off, 0 for neutral.
        """
        if hyg_tlt_ratio is None or len(hyg_tlt_ratio) < 60:
            return 0

        # 60-day rate of change of HYG/TLT ratio
        roc = (hyg_tlt_ratio.iloc[-1] / hyg_tlt_ratio.iloc[-60] - 1) * 100

        if roc > 2:
            return 1
        elif roc < -2:
            return -1
        return 0

    # ── HMM Regime ───────────────────────────────────────────────────────

    def _fit_hmm(
        self,
        spy_returns: pd.Series,
    ) -> Optional[np.ndarray]:
        """
        Fit a Gaussian HMM on SPY returns and return state sequence.

        Returns:
            Array of state labels, or None if hmmlearn not available.
        """
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            logger.warning("hmmlearn not installed, skipping HMM regime detection")
            return None

        if len(spy_returns) < 252:
            logger.warning("Need at least 1 year of data for HMM, got %d", len(spy_returns))
            return None

        returns = spy_returns.dropna().values.reshape(-1, 1)

        model = GaussianHMM(
            n_components=self._hmm_states,
            covariance_type="full",
            n_iter=100,
            random_state=42,
            tol=1e-4,
        )

        try:
            model.fit(returns)
            states = model.predict(returns)
            self._hmm_model = model
            return states
        except Exception as e:
            logger.warning("HMM fit failed: %s", e)
            return None

    def _hmm_signal(
        self,
        spy_returns: Optional[pd.Series] = None,
    ) -> int:
        """
        HMM-based regime signal.

        Maps the current HMM state to regime:
        - State with highest volatility → -1 (defensive)
        - State with lowest volatility → +1 (aggressive)
        - Other → 0 (neutral)
        """
        if not self._use_hmm or spy_returns is None:
            return 0

        states = self._fit_hmm(spy_returns)
        if states is None:
            return 0

        current_state = states[-1]

        # Find volatility of each state
        returns = spy_returns.dropna().values
        state_vols = {}
        for s in range(self._hmm_states):
            mask = states == s
            if mask.sum() > 0:
                state_vols[s] = np.std(returns[mask[:len(returns)]])

        if not state_vols:
            return 0

        # Sort states by volatility
        sorted_states = sorted(state_vols.items(), key=lambda x: x[1])
        lowest_vol_state = sorted_states[0][0]
        highest_vol_state = sorted_states[-1][0]

        if current_state == highest_vol_state:
            return -1
        elif current_state == lowest_vol_state:
            return 1
        return 0

    # ── Main Detection ───────────────────────────────────────────────────

    def detect_regime(
        self,
        vix: Optional[float] = None,
        spy_prices: Optional[pd.Series] = None,
        spy_returns: Optional[pd.Series] = None,
        hyg_tlt_ratio: Optional[pd.Series] = None,
    ) -> str:
        """
        Detect the current market regime by combining multiple signals.

        Args:
            vix: Current VIX level.
            spy_prices: SPY daily close prices (>= 200 days).
            spy_returns: SPY daily returns (>= 252 days for HMM).
            hyg_tlt_ratio: HYG/TLT ratio series for credit spread proxy.

        Returns:
            Regime string: "DEFENSIVE", "NEUTRAL", or "AGGRESSIVE"
        """
        signals = {}

        # VIX signal
        vix_val = self.get_vix_level(vix)
        signals["vix"] = self._vix_signal(vix_val)

        # Trend signal
        signals["trend"] = self._trend_signal(spy_prices)

        # Credit spread signal
        signals["credit"] = self._credit_spread_signal(hyg_tlt_ratio)

        # HMM signal
        signals["hmm"] = self._hmm_signal(spy_returns)

        # Weighted ensemble
        weights = {
            "vix": 0.30,
            "trend": 0.30,
            "credit": 0.20,
            "hmm": 0.20,
        }

        score = sum(signals[k] * weights[k] for k in signals)

        # Classify
        if score <= -0.3:
            regime = Regime.DEFENSIVE.value
        elif score >= 0.3:
            regime = Regime.AGGRESSIVE.value
        else:
            regime = Regime.NEUTRAL.value

        self._last_regime = regime
        self._last_update = datetime.now()

        logger.info(
            "Regime detected: %s (score=%.2f, signals=%s)",
            regime, score, signals,
        )
        return regime

    @property
    def last_regime(self) -> Optional[str]:
        """Last detected regime."""
        return self._last_regime

    @property
    def last_update(self) -> Optional[datetime]:
        """Timestamp of last regime detection."""
        return self._last_update
