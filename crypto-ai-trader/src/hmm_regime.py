"""
HMM Market Regime Detector — Hidden Markov Model for regime identification.

Uses hmmlearn to fit a 4-state Gaussian HMM on BTC daily features:
1. Daily log returns
2. Realized volatility (14-day rolling)
3. RSI (14-period)
4. Bollinger Band position (price relative to bands)

Regimes:
- BULL_TREND: low vol, positive returns, RSI 50-70
- BEAR_TREND: high vol, negative returns, RSI < 40
- RANGE_BOUND: medium vol, near-zero returns, RSI 40-60
- HIGH_VOL: extreme vol, thick tails, any direction

Integration: strategy_adaptor reads regime probabilities to adjust
strategy weights, position sizing, and risk parameters.
"""

import json
import logging
import time
import numpy as np
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Regime labels (order matches HMM state ordering by mean return)
REGIME_LABELS = ["BEAR_TREND", "HIGH_VOL", "RANGE_BOUND", "BULL_TREND"]

# Regime-specific strategy adjustments
REGIME_STRATEGY_MAP = {
    "BULL_TREND": {
        "preferred_strategies": ["trend", "vwap"],
        "avoid_strategies": ["grid"],
        "position_scale": 1.2,      # Slightly more aggressive
        "sl_multiplier": 1.0,       # Normal SL
        "tp_multiplier": 1.2,       # Wider TP (ride the trend)
        "score_threshold_adj": -5,  # Lower threshold (more trades)
    },
    "BEAR_TREND": {
        "preferred_strategies": ["dca", "rsi"],
        "avoid_strategies": ["trend", "grid"],
        "position_scale": 0.6,      # Reduce size
        "sl_multiplier": 0.8,       # Tighter SL
        "tp_multiplier": 0.8,       # Quicker TP (don't hold losers)
        "score_threshold_adj": 10,  # Higher threshold (fewer trades)
    },
    "RANGE_BOUND": {
        "preferred_strategies": ["bollinger", "grid", "rsi"],
        "avoid_strategies": ["trend"],
        "position_scale": 1.0,      # Normal size
        "sl_multiplier": 1.0,
        "tp_multiplier": 1.0,
        "score_threshold_adj": 0,
    },
    "HIGH_VOL": {
        "preferred_strategies": ["dca"],
        "avoid_strategies": ["trend", "grid", "vwap"],
        "position_scale": 0.5,      # Half size
        "sl_multiplier": 1.5,       # Wider SL (whipsaw protection)
        "tp_multiplier": 1.5,       # Wider TP (capture swings)
        "score_threshold_adj": 5,   # Moderate bar (was +15, too aggressive with FEAR stacking)
    },
}

# Minimum klines needed for feature computation
MIN_KLINES = 50


class HMMRegimeDetector:
    """HMM-based market regime detection."""

    def __init__(self, db=None):
        if db is None:
            from src.state_db import get_state_db
            db = get_state_db()
        self._db = db
        self._model = None
        self._trained = False

    def _compute_features(self, klines_1h: List) -> Optional[np.ndarray]:
        """Compute feature matrix from 1h klines.

        Aggregates to daily bars, then computes:
        1. Log returns
        2. Realized volatility (14-day rolling std of returns)
        3. RSI (14-period)
        4. BB position (where price sits within bands)

        Returns: (N, 4) feature matrix, or None if insufficient data.
        """
        if len(klines_1h) < MIN_KLINES:
            return None

        # Parse klines to arrays (handle both dict and list formats)
        def _get(k, field, idx):
            if isinstance(k, dict):
                return float(k.get(field, k.get(idx, 0)))
            return float(k[idx])

        closes = np.array([_get(k, "close", 4) for k in klines_1h])
        highs = np.array([_get(k, "high", 2) for k in klines_1h])
        lows = np.array([_get(k, "low", 3) for k in klines_1h])

        # Aggregate to daily (24 bars per day)
        n_days = len(closes) // 24
        if n_days < 20:
            return None

        daily_closes = closes[::24][:n_days]
        daily_highs = np.array([highs[i*24:(i+1)*24].max() for i in range(n_days)])
        daily_lows = np.array([lows[i*24:(i+1)*24].min() for i in range(n_days)])

        # 1. Log returns
        log_returns = np.diff(np.log(daily_closes))

        # 2. Realized volatility (14-day rolling)
        vol_window = 14
        realized_vol = np.array([
            log_returns[max(0, i-vol_window+1):i+1].std() * np.sqrt(365)
            for i in range(len(log_returns))
        ])

        # 3. RSI (14-period)
        rsi = self._compute_rsi(daily_closes, period=14)

        # 4. BB position
        bb_pos = self._compute_bb_position(daily_closes, period=20, std_dev=2.0)

        # Align all features to same length
        min_len = min(len(log_returns), len(realized_vol), len(rsi), len(bb_pos))
        features = np.column_stack([
            log_returns[-min_len:],
            realized_vol[-min_len:],
            rsi[-min_len:],
            bb_pos[-min_len:],
        ])

        # Remove NaN rows
        valid = ~np.isnan(features).any(axis=1)
        features = features[valid]

        return features if len(features) >= 20 else None

    @staticmethod
    def _compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
        """Compute RSI for price array."""
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        rsi = np.full(len(prices), 50.0)
        if len(gains) < period:
            return rsi

        avg_gain = gains[:period].mean()
        avg_loss = losses[:period].mean()

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

            if avg_loss == 0:
                rsi[i + 1] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    @staticmethod
    def _compute_bb_position(prices: np.ndarray, period: int = 20, std_dev: float = 2.0) -> np.ndarray:
        """Compute price position within Bollinger Bands (-1 to +1)."""
        bb_pos = np.zeros(len(prices))
        for i in range(period, len(prices)):
            window = prices[i-period:i]
            mean = window.mean()
            std = window.std()
            if std > 0:
                upper = mean + std_dev * std
                lower = mean - std_dev * std
                bb_range = upper - lower
                bb_pos[i] = (prices[i] - lower) / bb_range if bb_range > 0 else 0.5
            else:
                bb_pos[i] = 0.5
        return bb_pos

    def train(self, klines_1h: List) -> bool:
        """Train HMM on historical klines data.

        Returns True if training succeeded.
        """
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            logger.error("hmmlearn not installed. Run: pip install hmmlearn")
            return False

        features = self._compute_features(klines_1h)
        if features is None:
            logger.warning("Insufficient data for HMM training")
            return False

        # Normalize features
        self._mean = features.mean(axis=0)
        self._std = features.std(axis=0)
        self._std[self._std == 0] = 1.0
        features_norm = (features - self._mean) / self._std

        # Fit HMM with 4 states
        model = GaussianHMM(
            n_components=4,
            covariance_type="diag",
            n_iter=200,
            random_state=42,
            tol=1e-4,
        )

        try:
            model.fit(features_norm)
        except Exception as e:
            logger.warning(f"HMM fitting failed: {e}")
            return False

        # Sort states by mean return (ascending: bear → bull)
        state_means = model.means_[:, 0]  # First feature = log return
        sorted_indices = np.argsort(state_means)

        # Store sorting order for prediction-time mapping
        self._state_order = sorted_indices

        # Reorder means and startprob (these are always safe to reorder)
        model.means_ = model.means_[sorted_indices]
        model.startprob_ = model.startprob_[sorted_indices]
        model.transmat_ = model.transmat_[sorted_indices][:, sorted_indices]

        # Reorder covars first (before any label flip logic)
        # Use internal _covars_ to bypass property setter shape validation
        # (hmmlearn getter may return (n,n,f) but setter expects (n,f) for diag)
        try:
            if hasattr(model, '_covars_') and model._covars_ is not None:
                model._covars_ = model._covars_[sorted_indices]
            elif hasattr(model, 'covars_') and model.covars_ is not None:
                covars = model.covars_
                if covars.ndim == 3:
                    covars = np.array([np.diag(covars[i]) for i in range(covars.shape[0])])
                model.covars_ = covars[sorted_indices]
        except Exception:
            logger.error("HMM covars reordering failed, skipping", exc_info=True)

        # Label consistency: compare new means with previously stored mapping
        # Use the FULL mapping, not just label_0, to detect actual flips.
        prev_mapping = self._load_label_mapping()
        if prev_mapping is not None:
            new_means_sorted = model.means_[:, 0]  # already sorted ascending
            prev_label_0 = prev_mapping.get("label_0", "BEAR_TREND")
            prev_label_3 = prev_mapping.get("label_3", "BULL_TREND")
            # Only flip if the extremes are reversed: prev had state0=BULL but now state0=lowest mean
            if prev_label_0 == "BULL_TREND" and prev_label_3 == "BEAR_TREND":
                logger.warning("HMM label flip detected — reversing state order for consistency")
                reverse = np.array([3, 2, 1, 0])
                model.means_ = model.means_[reverse]
                model.startprob_ = model.startprob_[reverse]
                model.transmat_ = model.transmat_[reverse][:, reverse]
                try:
                    if hasattr(model, '_covars_') and model._covars_ is not None:
                        model._covars_ = model._covars_[reverse]
                    elif hasattr(model, 'covars_') and model.covars_ is not None:
                        covars = model.covars_
                        if covars.ndim == 3:
                            covars = np.array([np.diag(covars[i]) for i in range(covars.shape[0])])
                        model.covars_ = covars[reverse]
                except Exception:
                    logger.error("HMM covars reverse reordering failed", exc_info=True)
                self._state_order = sorted_indices[reverse]

        # Store current label mapping for future consistency checks
        self._store_label_mapping(model.means_[:, 0])

        self._model = model
        self._trained = True

        # Store training metadata
        self._store_training_state(features)

        logger.info(
            f"HMM trained: {len(features)} samples, "
            f"regime means={[f'{REGIME_LABELS[i]}: {model.means_[i][0]:+.4f}' for i in range(4)]}"
        )
        return True

    def predict(self, klines_1h: List) -> Optional[Dict]:
        """Predict current market regime from recent klines.

        Returns:
            {
                "regime": str (most likely regime),
                "regime_idx": int,
                "probabilities": {regime: prob},
                "confidence": float (probability of top regime),
                "features": {name: value},
            }
        """
        if not self._trained:
            # Try to load from DB
            if not self._load_training_state():
                return None

        features = self._compute_features(klines_1h)
        if features is None:
            return None

        # Normalize
        features_norm = (features - self._mean) / self._std

        # Predict
        try:
            probs = self._model.predict_proba(features_norm)
            last_probs = probs[-1]  # Most recent time step

            regime_idx = int(np.argmax(last_probs))
            regime = REGIME_LABELS[regime_idx]
            confidence = float(last_probs[regime_idx])

            prob_dict = {
                REGIME_LABELS[i]: round(float(last_probs[i]), 4)
                for i in range(4)
            }

            # Current feature values
            feat_names = ["return", "volatility", "rsi", "bb_position"]
            feat_values = {
                name: round(float(features[-1, i]), 4)
                for i, name in enumerate(feat_names)
            }

            result = {
                "regime": regime,
                "regime_idx": regime_idx,
                "probabilities": prob_dict,
                "confidence": round(confidence, 4),
                "features": feat_values,
                "timestamp": time.time(),
            }

            # Cache result
            self._store_prediction(result)

            return result

        except Exception as e:
            logger.warning(f"HMM prediction failed: {e}")
            return None

    def get_strategy_adjustments(self, regime: str) -> Dict:
        """Get strategy adjustments for a given regime."""
        return REGIME_STRATEGY_MAP.get(regime, REGIME_STRATEGY_MAP["RANGE_BOUND"])

    def get_cached_prediction(self) -> Optional[Dict]:
        """Get the most recent cached prediction from DB."""
        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'hmm_regime'"
        ).fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                logger.error("Failed to parse cached HMM regime prediction from DB", exc_info=True)
        return None

    def _store_prediction(self, result: Dict):
        """Store prediction in DB."""
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('hmm_regime', ?, ?)""",
            (json.dumps(result), time.time()),
        )
        conn.commit()

    def _store_training_state(self, features: np.ndarray):
        """Store model parameters in DB for persistence."""
        # hmmlearn diag covars_: .covars_ property returns full (n, n_feat, n_feat)
        # but GaussianHMM(covariance_type='diag') expects (n, n_feat) on load.
        # Extract diagonals for correct round-trip.
        covars_raw = self._model.covars_
        if covars_raw.ndim == 3:
            covars_store = np.array([np.diag(covars_raw[i]) for i in range(covars_raw.shape[0])])
        else:
            covars_store = covars_raw

        state = {
            "mean": self._mean.tolist(),
            "std": self._std.tolist(),
            "means": self._model.means_.tolist(),
            "covars": covars_store.tolist(),
            "startprob": self._model.startprob_.tolist(),
            "transmat": self._model.transmat_.tolist(),
            "n_samples": len(features),
            "trained_at": time.time(),
        }
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('hmm_model_state', ?, ?)""",
            (json.dumps(state), time.time()),
        )
        conn.commit()

    def _store_label_mapping(self, means: np.ndarray):
        """Store current label-to-state mapping for consistency across retraining."""
        mapping = {
            "label_0": REGIME_LABELS[0] if means[0] <= means[-1] else REGIME_LABELS[3],
            "label_3": REGIME_LABELS[3] if means[0] <= means[-1] else REGIME_LABELS[0],
            "means_order": "ascending" if means[0] <= means[-1] else "descending",
            "means": means.tolist(),
            "timestamp": time.time(),
        }
        conn = self._db._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('hmm_label_mapping', ?, ?)""",
            (json.dumps(mapping), time.time()),
        )
        conn.commit()

    def _load_label_mapping(self) -> Optional[Dict]:
        """Load previously stored label mapping from DB."""
        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'hmm_label_mapping'"
        ).fetchone()
        if row:
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def _load_training_state(self) -> bool:
        """Load model from DB."""
        try:
            from hmmlearn.hmm import GaussianHMM
        except ImportError:
            return False

        conn = self._db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'hmm_model_state'"
        ).fetchone()
        if not row:
            return False

        try:
            state = json.loads(row["value"])
            self._mean = np.array(state["mean"])
            self._std = np.array(state["std"])

            model = GaussianHMM(
                n_components=4,
                covariance_type="diag",
                n_iter=0,  # No training, just load
            )
            # Manually set parameters
            model.n_features = 4
            model.means_ = np.array(state["means"])
            # covars: diag type expects (n_components, n_features), not full matrices
            covars_raw = np.array(state["covars"])
            if covars_raw.ndim == 3:
                # Legacy: stored as full diagonal matrices (n, n_feat, n_feat)
                covars_raw = np.array([np.diag(covars_raw[i]) for i in range(covars_raw.shape[0])])
            model.covars_ = covars_raw
            model.startprob_ = np.array(state["startprob"])
            model.transmat_ = np.array(state["transmat"])

            self._model = model
            self._trained = True
            return True
        except Exception as e:
            logger.warning(f"Failed to load HMM state: {e}")
            return False

    def format_report(self, prediction: Dict) -> str:
        """Format regime prediction as human-readable report."""
        if not prediction:
            return "HMM 未訓練或無預測結果"

        regime = prediction["regime"]
        conf = prediction["confidence"]
        probs = prediction["probabilities"]
        feats = prediction.get("features", {})

        REGIME_NAMES = {
            "BULL_TREND": "🟢 牛市趨勢",
            "BEAR_TREND": "🔴 熊市趨勢",
            "RANGE_BOUND": "⚪ 盤整震盪",
            "HIGH_VOL": "🟡 高波動",
        }

        lines = [
            f"## HMM 市場體制",
            "",
            f"**當前體制**: {REGIME_NAMES.get(regime, regime)}",
            f"**置信度**: {conf:.1%}",
            "",
            "**概率分佈**:",
        ]
        for r, p in sorted(probs.items(), key=lambda x: -x[1]):
            indicator = "●" if r == regime else "○"
            lines.append(f"- {indicator} {REGIME_NAMES.get(r, r)}: {p:.1%}")

        if feats:
            lines.extend([
                "",
                "**特徵值**:",
                f"- 收益率: {feats.get('return', 0):+.4f}",
                f"- 波動率: {feats.get('volatility', 0):.4f}",
                f"- RSI: {feats.get('rsi', 0):.1f}",
                f"- BB 位置: {feats.get('bb_position', 0):.2f}",
            ])

        adj = self.get_strategy_adjustments(regime)
        lines.extend([
            "",
            "**策略建議**:",
            f"- 偏好: {', '.join(adj['preferred_strategies'])}",
            f"- 避免: {', '.join(adj['avoid_strategies'])}",
            f"- 倉位縮放: {adj['position_scale']:.1f}x",
            f"- 閾值調整: {adj['score_threshold_adj']:+d}",
        ])

        return "\n".join(lines)
