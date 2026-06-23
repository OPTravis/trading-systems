"""
LightGBM Price Direction Predictor
CPU-friendly gradient boosting approach for predicting price direction (up/down) in next 24h.
"""

import logging
import warnings
from pathlib import Path
from typing import Dict, List, Optional

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

# Feature names (17 total)
FACTOR_FEATURES = [
    "rsi",
    "macd_histogram",
    "bb_position",
    "volume_ratio",
    "obv_divergence",
    "consolidation_score",
    "bb_squeeze",
    "rsi_divergence",
    "orderbook_imbalance",
    "sentiment_score",
    "trend_score",
    "price_action_score",
]
DERIVED_FEATURES = [
    "hmm_regime",
    "fear_greed",
    "btc_trend",
    "volatility_24h",
    "volume_surge",
    # --- P3 #11: new features ---
    "exchange_netflow",       # net BTC flow to/from exchanges (negative = outflow = bullish)
    "whale_activity",         # whale transaction z-score (higher = more large moves)
    "funding_rate",           # perpetual funding rate (contrarian: high positive = bearish)
    "open_interest_change",   # 24h OI change % (rising + price up = momentum)
]
ALL_FEATURES = FACTOR_FEATURES + DERIVED_FEATURES

# Model persistence path
MODEL_DIR = Path.home() / "crypto-ai-trader" / "data"
MODEL_PATH = MODEL_DIR / "lgbm_model.pkl"

# LightGBM parameters
LGBM_PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "n_estimators": 100,
    "verbose": -1,
    "random_state": 42,
}

MIN_TRAINING_SAMPLES = 100


class PricePredictor:
    """LightGBM-based price direction predictor for crypto trading."""

    def __init__(self):
        self.model: Optional[lgb.LGBMClassifier] = None
        self.scaler: Optional[StandardScaler] = None
        self.is_trained: bool = False
        self.feature_names: List[str] = ALL_FEATURES

    def _extract_features(self, features: Dict) -> np.ndarray:
        """Extract feature vector from dictionary."""
        return np.array([features.get(f, 0.0) for f in self.feature_names]).reshape(
            1, -1
        )

    def _extract_features_batch(self, features_list: List[Dict]) -> np.ndarray:
        """Extract feature matrix from list of dictionaries."""
        return np.array(
            [[d.get(f, 0.0) for f in self.feature_names] for d in features_list]
        )

    def is_ready(self) -> bool:
        """Check if model is trained and ready for predictions."""
        return self.is_trained and self.model is not None and self.scaler is not None

    def enrich_features(self, features: Dict, symbol: str = "BTCUSDT") -> Dict:
        """Populate missing derived features from data feeds.

        Fills in exchange_netflow, whale_activity, funding_rate,
        open_interest_change from on-chain and market data feeds.
        Uses neutral 0.0 defaults if data is unavailable.

        Args:
            features: Existing feature dict (modified in-place and returned).
            symbol: Trading pair symbol for data lookup.

        Returns:
            The enriched feature dict.
        """
        # --- exchange_netflow (from on-chain data) ---
        if "exchange_netflow" not in features:
            try:
                from src.data_feed_onchain import DeFiLlamaOnChain
                onchain = DeFiLlamaOnChain()
                score = onchain.get_onchain_score()
                # Map 0-100 score to roughly -1 to +1 range
                # Low score (bearish on-chain) → positive netflow (inflow to exchanges) → bearish
                # High score (bullish on-chain) → negative netflow (outflow) → bullish
                features["exchange_netflow"] = (50.0 - score) / 50.0
            except Exception:
                features["exchange_netflow"] = 0.0  # neutral default

        # --- whale_activity (placeholder — no dedicated feed yet) ---
        if "whale_activity" not in features:
            features["whale_activity"] = 0.0  # neutral default

        # --- funding_rate (from scoring aggregator) ---
        if "funding_rate" not in features:
            try:
                from src.data_feed_funding import FundingRate
                from src.data_feed_oi import OpenInterest
                from src.data_feed_scorer import ScoringDataAggregator
                funding = FundingRate()
                oi = OpenInterest()
                scorer = ScoringDataAggregator(funding, oi)
                sentiment = scorer.get_symbol_sentiment(symbol)
                # funding_rate is in decimal (e.g. 0.0001 = 0.01%)
                # Scale: 0.0001 → 0.01 range for feature
                raw_fr = sentiment.get("funding_rate", 0.0)
                features["funding_rate"] = raw_fr * 1000  # scale to ~0.1 range
            except Exception:
                features["funding_rate"] = 0.0  # neutral default

        # --- open_interest_change (from scoring aggregator) ---
        if "open_interest_change" not in features:
            try:
                from src.data_feed_funding import FundingRate
                from src.data_feed_oi import OpenInterest
                from src.data_feed_scorer import ScoringDataAggregator
                funding = FundingRate()
                oi = OpenInterest()
                scorer = ScoringDataAggregator(funding, oi)
                sentiment = scorer.get_symbol_sentiment(symbol)
                oi_pct = sentiment.get("oi_change_pct")
                features["open_interest_change"] = oi_pct if oi_pct is not None else 0.0
            except Exception:
                features["open_interest_change"] = 0.0  # neutral default

        return features

    def train(self, features_list: List[Dict], labels: List[int]) -> Dict:
        """
        Train the LightGBM model with time-based validation and early stopping.

        Data is split 80/20 by position (NOT random) to simulate time-series
        ordering — the first 80% become training data and the last 20% become
        the held-out validation set.  This avoids data leakage from future
        observations leaking into the training window.

        Args:
            features_list: List of feature dictionaries (assumed time-ordered).
            labels: List of binary labels (1=up, 0=down).

        Returns:
            Dict with training AND validation metrics.
        """
        if len(features_list) < MIN_TRAINING_SAMPLES:
            raise ValueError(
                f"Insufficient training samples: {len(features_list)} < {MIN_TRAINING_SAMPLES}"
            )

        # ── 1. Time-based train / validation split (80 / 20) ───────────
        X = self._extract_features_batch(features_list)
        y = np.array(labels)
        split_idx = int(len(X) * 0.8)
        # Ensure at least MIN_TRAINING_SAMPLES in train and some in val
        split_idx = max(split_idx, MIN_TRAINING_SAMPLES)
        split_idx = min(split_idx, len(X) - 1)  # leave at least 1 for val

        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]

        # ── 2. Fit scaler on TRAINING data only, then transform both ───
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_val_scaled = self.scaler.transform(X_val)

        # ── 3. Train LightGBM with early stopping ──────────────────────
        self.model = lgb.LGBMClassifier(**LGBM_PARAMS)  # type: ignore[arg-type]

        # Build callbacks for early stopping (LightGBM >= 4.x style)
        callbacks = [
            lgb.early_stopping(stopping_rounds=50, verbose=False),
            lgb.log_evaluation(period=0),  # suppress per-iteration logging
        ]

        # Suppress LightGBM warnings about early stopping with custom metric
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.model.fit(
                X_train_scaled,
                y_train,
                eval_set=[(X_train_scaled, y_train), (X_val_scaled, y_val)],
                eval_metric="auc",
                callbacks=callbacks,
            )

        self.is_trained = True

        # ── 4. Compute metrics on BOTH splits ──────────────────────────
        train_pred = self.model.predict(X_train_scaled)
        train_prob = self.model.predict_proba(X_train_scaled)[:, 1]

        val_pred = self.model.predict(X_val_scaled)
        val_prob = self.model.predict_proba(X_val_scaled)[:, 1]

        train_accuracy = float(accuracy_score(y_train, train_pred))
        try:
            train_auc = float(roc_auc_score(y_train, train_prob))
        except ValueError:
            train_auc = float("nan")
        try:
            train_logloss = float(log_loss(y_train, train_prob))
        except ValueError:
            train_logloss = float("nan")

        # Guard: validation set may be very small or single-class
        try:
            val_accuracy = float(accuracy_score(y_val, val_pred))
        except Exception:
            val_accuracy = float("nan")
        try:
            val_auc = float(roc_auc_score(y_val, val_prob))
        except ValueError:
            val_auc = float("nan")
        try:
            val_logloss = float(log_loss(y_val, val_prob))
        except Exception:
            val_logloss = float("nan")

        # ── 5. Log comparison ──────────────────────────────────────────
        best_iter = getattr(self.model, "best_iteration_", self.model.n_estimators)
        logger.info(
            "Model trained | train_acc=%.4f  train_auc=%.4f  train_logloss=%.4f "
            "| val_acc=%.4f  val_auc=%.4f  val_logloss=%.4f "
            "| n_train=%d  n_val=%d  best_iter=%s",
            train_accuracy,
            train_auc,
            train_logloss,
            val_accuracy,
            val_auc,
            val_logloss,
            len(y_train),
            len(y_val),
            best_iter,
        )

        # ── 6. Overfitting / random-model warning ──────────────────────
        if not (val_auc != val_auc):  # not NaN  (NaN != NaN is True)
            if val_auc < 0.52:
                logger.warning(
                    "VALIDATION AUC %.4f is barely better than random (0.50). "
                    "Model is likely overfitting or features have no predictive power. "
                    "Consider adding more data, reducing model complexity, or reviewing features.",
                    val_auc,
                )

        metrics = {
            # Train metrics
            "train_accuracy": train_accuracy,
            "train_auc": train_auc,
            "train_logloss": train_logloss,
            # Validation metrics
            "val_accuracy": val_accuracy,
            "val_auc": val_auc,
            "val_logloss": val_logloss,
            # Metadata
            "n_train": len(y_train),
            "n_val": len(y_val),
            "n_samples": len(labels),
            "best_iteration": best_iter,
            "feature_importance": dict(
                zip(self.feature_names, self.model.feature_importances_.tolist())
            ),
            # Backward-compatible keys (some consumers read 'accuracy' / 'loss')
            "accuracy": train_accuracy,
            "loss": train_logloss,
        }

        return metrics

    def predict(self, features: Dict) -> Dict:
        """
        Predict price direction for given features.

        Args:
            features: Dictionary with feature values

        Returns:
            Dict with 'direction', 'confidence', and 'prob_up'
        """
        if not self.is_ready():
            raise RuntimeError("Model not trained. Call train() or load_model() first.")

        # Auto-enrich with on-chain/market features if missing
        symbol = features.get("symbol", "BTCUSDT")
        features = self.enrich_features(dict(features), symbol=symbol)

        X = self._extract_features(features)
        assert self.scaler is not None
        X_scaled = self.scaler.transform(X)

        assert self.model is not None
        prob_up = self.model.predict_proba(X_scaled)[0, 1]
        direction = "up" if prob_up > 0.5 else "down"
        confidence = abs(prob_up - 0.5) * 2  # Scale to 0-1

        # Store features in Feature Store for training-serving consistency
        try:
            from src.feature_store import get_store

            fs = get_store()
            symbol = features.get("symbol", "UNKNOWN")
            fs.store_features(symbol, features, namespace="online")
        except Exception:
            logger.warning(
                "Failed to store features in Feature Store for %s (non-critical)",
                features.get("symbol", "UNKNOWN"),
                exc_info=True,
            )

        return {
            "direction": direction,
            "confidence": float(confidence),
            "prob_up": float(prob_up),
        }

    def save_model(self, path: Optional[str] = None):
        """Save model and scaler to disk."""
        if not self.is_ready():
            raise RuntimeError("No trained model to save.")

        save_path = Path(path) if path else MODEL_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)

        model_data = {
            "model": self.model,
            "scaler": self.scaler,
            "feature_names": self.feature_names,
            "lgbm_params": LGBM_PARAMS,
        }

        joblib.dump(model_data, save_path)
        logger.info(f"Model saved to {save_path}")

    def load_model(self, path: Optional[str] = None):
        """Load model and scaler from disk."""
        load_path = Path(path) if path else MODEL_PATH

        if not load_path.exists():
            raise FileNotFoundError(f"Model file not found: {load_path}")

        model_data = joblib.load(load_path)
        self.model = model_data["model"]
        self.scaler = model_data["scaler"]
        self.feature_names = model_data.get("feature_names", ALL_FEATURES)
        self.is_trained = True

        logger.info(f"Model loaded from {load_path}")


# Singleton instance
predictor = PricePredictor()


def get_predictor() -> PricePredictor:
    """Get the global PricePredictor instance."""
    return predictor


def predict(features: Dict) -> Dict:
    """Convenience function for prediction."""
    return predictor.predict(features)


def train(features_list: List[Dict], labels: List[int]) -> Dict:
    """Convenience function for training."""
    return predictor.train(features_list, labels)


def is_ready() -> bool:
    """Convenience function to check if model is ready."""
    return predictor.is_ready()


def save_model(path: Optional[str] = None):
    """Convenience function to save model."""
    predictor.save_model(path)


def load_model(path: Optional[str] = None):
    """Convenience function to load model."""
    predictor.load_model(path)
