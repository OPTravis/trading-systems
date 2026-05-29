"""
LightGBM Price Direction Predictor
CPU-friendly gradient boosting approach for predicting price direction (up/down) in next 24h.
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)

# Feature names (17 total)
FACTOR_FEATURES = [
    'rsi', 'macd_histogram', 'bb_position', 'volume_ratio', 'obv_divergence',
    'consolidation_score', 'bb_squeeze', 'rsi_divergence', 'orderbook_imbalance',
    'sentiment_score', 'trend_score', 'price_action_score'
]
DERIVED_FEATURES = ['hmm_regime', 'fear_greed', 'btc_trend', 'volatility_24h', 'volume_surge']
ALL_FEATURES = FACTOR_FEATURES + DERIVED_FEATURES

# Model persistence path
MODEL_DIR = Path.home() / 'crypto-ai-trader' / 'data'
MODEL_PATH = MODEL_DIR / 'lgbm_model.pkl'

# LightGBM parameters
LGBM_PARAMS = {
    'objective': 'binary',
    'metric': 'binary_logloss',
    'num_leaves': 31,
    'learning_rate': 0.05,
    'n_estimators': 100,
    'verbose': -1,
    'random_state': 42
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
        return np.array([features.get(f, 0.0) for f in self.feature_names]).reshape(1, -1)
    
    def _extract_features_batch(self, features_list: List[Dict]) -> np.ndarray:
        """Extract feature matrix from list of dictionaries."""
        return np.array([[d.get(f, 0.0) for f in self.feature_names] for d in features_list])
    
    def is_ready(self) -> bool:
        """Check if model is trained and ready for predictions."""
        return self.is_trained and self.model is not None and self.scaler is not None
    
    def train(self, features_list: List[Dict], labels: List[int]) -> Dict:
        """
        Train the LightGBM model on provided features and labels.
        
        Args:
            features_list: List of feature dictionaries
            labels: List of binary labels (1=up, 0=down)
            
        Returns:
            Dict with training metrics
        """
        if len(features_list) < MIN_TRAINING_SAMPLES:
            raise ValueError(f"Insufficient training samples: {len(features_list)} < {MIN_TRAINING_SAMPLES}")
        
        # Convert to numpy arrays
        X = self._extract_features_batch(features_list)
        y = np.array(labels)
        
        # Initialize and fit scaler
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)
        
        # Initialize and train LightGBM
        self.model = lgb.LGBMClassifier(**LGBM_PARAMS)
        self.model.fit(X_scaled, y)
        self.is_trained = True
        
        # Calculate training metrics
        train_pred = self.model.predict(X_scaled)
        train_prob = self.model.predict_proba(X_scaled)[:, 1]
        
        accuracy = np.mean(train_pred == y)
        loss = -np.mean(y * np.log(train_prob + 1e-15) + (1 - y) * np.log(1 - train_prob + 1e-15))
        
        metrics = {
            'accuracy': float(accuracy),
            'loss': float(loss),
            'n_samples': len(labels),
            'feature_importance': dict(zip(self.feature_names, self.model.feature_importances_.tolist()))
        }
        
        logger.info(f"Model trained: accuracy={accuracy:.4f}, loss={loss:.4f}, samples={len(labels)}")
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
        
        X = self._extract_features(features)
        X_scaled = self.scaler.transform(X)
        
        prob_up = self.model.predict_proba(X_scaled)[0, 1]
        direction = 'up' if prob_up > 0.5 else 'down'
        confidence = abs(prob_up - 0.5) * 2  # Scale to 0-1
        
        # Store features in Feature Store for training-serving consistency
        try:
            from ..core.feature_store import get_store
            fs = get_store()
            symbol = features.get('symbol', 'UNKNOWN')
            fs.store_features(symbol, features, namespace='online')
        except Exception:
            logger.warning("Failed to store features in Feature Store for %s (non-critical)", features.get('symbol', 'UNKNOWN'), exc_info=True)
        
        return {
            'direction': direction,
            'confidence': float(confidence),
            'prob_up': float(prob_up)
        }
    
    def save_model(self, path: Optional[str] = None):
        """Save model and scaler to disk."""
        if not self.is_ready():
            raise RuntimeError("No trained model to save.")
        
        save_path = Path(path) if path else MODEL_PATH
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'lgbm_params': LGBM_PARAMS
        }
        
        joblib.dump(model_data, save_path)
        logger.info(f"Model saved to {save_path}")
    
    def load_model(self, path: Optional[str] = None):
        """Load model and scaler from disk."""
        load_path = Path(path) if path else MODEL_PATH
        
        if not load_path.exists():
            raise FileNotFoundError(f"Model file not found: {load_path}")
        
        model_data = joblib.load(load_path)
        self.model = model_data['model']
        self.scaler = model_data['scaler']
        self.feature_names = model_data.get('feature_names', ALL_FEATURES)
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
