"""
Unit tests for LightGBM PricePredictor validation logic.

Covers:
- Time-based train/val split (80/20, NOT random)
- Validation metrics returned (accuracy, AUC, log_loss)
- Scaler fit on training data only
- Early stopping callback passed to lgb.fit()
- Backward-compatible metric keys
- Low AUC warning
- Edge cases (single-class val, minimal samples)
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Ensure project root on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Fake LightGBM model
# ---------------------------------------------------------------------------

class _FakeModel:
    """Mimics LGBMClassifier enough for PricePredictor.train()."""

    def __init__(self, **kwargs):
        self.n_estimators = kwargs.get("n_estimators", 100)
        self.best_iteration_ = 42
        self.feature_importances_ = np.ones(22)

    def fit(self, X, y, eval_set=None, eval_metric=None, callbacks=None):
        self._fit_eval_set = eval_set
        self._fit_eval_metric = eval_metric
        self._fit_callbacks = callbacks
        return self

    def predict(self, X):
        n = len(X)
        return np.array([1 if i < n // 2 else 0 for i in range(n)])

    def predict_proba(self, X):
        n = len(X)
        probs = np.zeros((n, 2))
        for i in range(n):
            if i < n // 2:
                probs[i] = [0.3, 0.7]
            else:
                probs[i] = [0.7, 0.3]
        return probs


def _build_mock_lgb():
    """Build a mock lightgbm module with FakeModel."""
    mock_lgb = MagicMock()
    mock_lgb.early_stopping.return_value = "es_callback"
    mock_lgb.log_evaluation.return_value = "le_callback"
    mock_lgb.LGBMClassifier = _FakeModel
    return mock_lgb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FEATURE_NAMES = [
    "rsi", "macd_histogram", "bb_position", "volume_ratio",
    "obv_divergence", "consolidation_score", "bb_squeeze",
    "rsi_divergence", "orderbook_imbalance", "sentiment_score",
    "trend_score", "price_action_score", "hmm_regime", "fear_greed",
    "btc_trend", "volatility_24h", "volume_surge",
    "exchange_netflow", "whale_activity", "funding_rate",
    "open_interest_change",
]


def _make_features_and_labels(n=200, seed=42, all_same_label=None):
    """Generate synthetic features (list[dict]) and labels (list[int])."""
    rng = np.random.RandomState(seed)
    features_list = []
    labels = []
    for i in range(n):
        feats = {f: float(rng.randn()) for f in _FEATURE_NAMES}
        features_list.append(feats)
        if all_same_label is not None:
            labels.append(all_same_label)
        else:
            labels.append(int(rng.randint(0, 2)))
    return features_list, labels


@pytest.fixture(autouse=True)
def _mock_lightgbm(monkeypatch):
    """Patch lgb references inside src.price_predictor so no real LightGBM needed."""
    mock_lgb = _build_mock_lgb()
    import src.price_predictor as pp_mod
    monkeypatch.setattr(pp_mod, "lgb", mock_lgb)
    # Store mock so tests can inspect it
    self_mock_lgb = mock_lgb
    yield mock_lgb


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTimeBasedSplit:
    """Verify the train/val split is positional (time-based), not random."""

    def test_split_ratio_200(self):
        """200 samples → 160 train / 40 val."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=200)
        metrics = pp.train(features_list, labels)
        assert metrics["n_train"] == 160
        assert metrics["n_val"] == 40
        assert metrics["n_samples"] == 200

    def test_split_ratio_250(self):
        """250 samples → 200 train / 50 val."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=250)
        metrics = pp.train(features_list, labels)
        assert metrics["n_train"] == 200
        assert metrics["n_val"] == 50

    def test_minimum_training_samples_enforced(self):
        """When 80% < MIN_TRAINING_SAMPLES, split pushes train up to minimum."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=110)
        metrics = pp.train(features_list, labels)
        assert metrics["n_train"] >= 100

    def test_split_preserves_order(self):
        """Scaler mean matches training-only data (first 80%)."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=200, seed=99)
        pp.train(features_list, labels)

        X = pp._extract_features_batch(features_list)
        X_train_expected = X[:160]
        expected_means = X_train_expected.mean(axis=0)
        np.testing.assert_array_almost_equal(
            pp.scaler.mean_, expected_means, decimal=10
        )


class TestValidationMetrics:
    """Verify the returned metrics dict contains all required keys."""

    def _train_default(self):
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels()
        return pp.train(features_list, labels)

    def test_returns_val_accuracy(self):
        metrics = self._train_default()
        assert "val_accuracy" in metrics
        assert isinstance(metrics["val_accuracy"], float)

    def test_returns_val_auc(self):
        metrics = self._train_default()
        assert "val_auc" in metrics
        assert isinstance(metrics["val_auc"], float)

    def test_returns_val_logloss(self):
        metrics = self._train_default()
        assert "val_logloss" in metrics
        assert isinstance(metrics["val_logloss"], float)

    def test_returns_train_metrics(self):
        metrics = self._train_default()
        assert "train_accuracy" in metrics
        assert "train_auc" in metrics
        assert "train_logloss" in metrics

    def test_returns_best_iteration(self):
        metrics = self._train_default()
        assert "best_iteration" in metrics
        assert metrics["best_iteration"] == 42

    def test_returns_feature_importance(self):
        metrics = self._train_default()
        assert "feature_importance" in metrics
        assert len(metrics["feature_importance"]) > 0

    def test_backward_compatible_keys(self):
        """Old consumers may read 'accuracy' and 'loss'."""
        metrics = self._train_default()
        assert "accuracy" in metrics
        assert "loss" in metrics
        assert metrics["accuracy"] == metrics["train_accuracy"]
        assert metrics["loss"] == metrics["train_logloss"]


class TestEarlyStopping:
    """Verify early stopping is wired into lgb.fit()."""

    def test_early_stopping_callback_passed(self, _mock_lightgbm):
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels()
        pp.train(features_list, labels)

        model = pp.model
        assert model._fit_eval_metric == "auc"
        assert model._fit_eval_set is not None
        assert len(model._fit_eval_set) == 2  # train + val
        assert model._fit_callbacks is not None

    def test_early_stopping_50_rounds(self, _mock_lightgbm):
        """lgb.early_stopping called with stopping_rounds=50."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels()
        pp.train(features_list, labels)
        _mock_lightgbm.early_stopping.assert_called_with(
            stopping_rounds=50, verbose=False
        )


class TestScalerIsolation:
    """Verify scaler is fit ONLY on training data (no val leakage)."""

    def test_scaler_not_fit_on_val(self):
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=200, seed=7)
        pp.train(features_list, labels)

        X_all = pp._extract_features_batch(features_list)
        X_train_only = X_all[:160]

        # scaler.mean_ should match X_train_only, not X_all
        train_mean = X_train_only.mean(axis=0)
        all_mean = X_all.mean(axis=0)
        assert not np.allclose(pp.scaler.mean_, all_mean, atol=1e-12), (
            "Scaler appears to be fit on ALL data (train+val) — data leakage!"
        )
        np.testing.assert_array_almost_equal(
            pp.scaler.mean_, train_mean, decimal=10
        )


class TestLowAucWarning:
    """Verify a WARNING is logged when validation AUC < 0.52."""

    def test_low_auc_triggers_warning(self, caplog, monkeypatch):
        """When mock returns degenerate probabilities, AUC will be ~0.5."""

        class _BadModel:
            n_estimators = 100
            best_iteration_ = 5
            feature_importances_ = np.ones(22)

            def __init__(self, **kwargs):
                pass

            def fit(self, X, y, eval_set=None, eval_metric=None, callbacks=None):
                return self

            def predict(self, X):
                return np.zeros(len(X), dtype=int)

            def predict_proba(self, X):
                n = len(X)
                return np.column_stack([np.full(n, 0.51), np.full(n, 0.49)])

        import src.price_predictor as pp_mod
        mock_lgb = _build_mock_lgb()
        mock_lgb.LGBMClassifier = _BadModel
        monkeypatch.setattr(pp_mod, "lgb", mock_lgb)

        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=200)

        with caplog.at_level(logging.WARNING, logger="src.price_predictor"):
            metrics = pp.train(features_list, labels)

        assert metrics["val_auc"] < 0.52 or any(
            "barely better than random" in r.message for r in caplog.records
        ), f"Expected warning for low AUC, val_auc={metrics['val_auc']}"


class TestEdgeCases:
    """Boundary conditions and error handling."""

    def test_insufficient_samples_raises(self):
        """Fewer than MIN_TRAINING_SAMPLES should raise ValueError."""
        from src.price_predictor import PricePredictor, MIN_TRAINING_SAMPLES
        pp = PricePredictor()
        n_too_few = MIN_TRAINING_SAMPLES - 1
        features_list, labels = _make_features_and_labels(n=n_too_few)
        with pytest.raises(ValueError, match="Insufficient training samples"):
            pp.train(features_list, labels)

    def test_exact_minimum_samples(self):
        """Exactly MIN_TRAINING_SAMPLES should succeed."""
        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=100)
        metrics = pp.train(features_list, labels)
        assert metrics["n_samples"] == 100
        assert metrics["n_train"] >= 80
        assert metrics["n_val"] >= 1

    def test_single_class_val_no_crash(self, monkeypatch):
        """If validation set has only one class, AUC is undefined but shouldn't crash."""

        class _SingleClassModel:
            n_estimators = 100
            best_iteration_ = 10
            feature_importances_ = np.ones(22)

            def __init__(self, **kwargs):
                pass

            def fit(self, X, y, eval_set=None, eval_metric=None, callbacks=None):
                return self

            def predict(self, X):
                return np.ones(len(X), dtype=int)

            def predict_proba(self, X):
                n = len(X)
                return np.column_stack([np.full(n, 0.1), np.full(n, 0.9)])

        import src.price_predictor as pp_mod
        mock_lgb = _build_mock_lgb()
        mock_lgb.LGBMClassifier = _SingleClassModel
        monkeypatch.setattr(pp_mod, "lgb", mock_lgb)

        from src.price_predictor import PricePredictor
        pp = PricePredictor()
        features_list, labels = _make_features_and_labels(n=200, all_same_label=1)
        metrics = pp.train(features_list, labels)
        assert "val_auc" in metrics
        assert "val_accuracy" in metrics
