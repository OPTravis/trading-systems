"""
Verification script for LightGBM Price Direction Predictor.
Generates synthetic data with known patterns and validates all functionality.
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from src.price_predictor import PricePredictor, ALL_FEATURES, MIN_TRAINING_SAMPLES


def generate_synthetic_data(n_samples: int = 200) -> tuple:
    """
    Generate synthetic data with known pattern.
    Pattern: positive trend_score + positive rsi divergence -> price goes up.
    """
    np.random.seed(42)
    
    features_list = []
    labels = []
    
    for _ in range(n_samples):
        features = {
            'rsi': np.random.uniform(0, 100),
            'macd_histogram': np.random.uniform(-1, 1),
            'bb_position': np.random.uniform(-1, 1),
            'volume_ratio': np.random.uniform(0.5, 2.0),
            'obv_divergence': np.random.uniform(-1, 1),
            'consolidation_score': np.random.uniform(0, 1),
            'bb_squeeze': np.random.uniform(0, 1),
            'rsi_divergence': np.random.uniform(-1, 1),
            'orderbook_imbalance': np.random.uniform(-1, 1),
            'sentiment_score': np.random.uniform(-1, 1),
            'trend_score': np.random.uniform(-1, 1),
            'price_action_score': np.random.uniform(-1, 1),
            'hmm_regime': np.random.randint(0, 4),
            'fear_greed': np.random.uniform(0, 100),
            'btc_trend': np.random.randint(0, 3),
            'volatility_24h': np.random.uniform(0, 0.1),
            'volume_surge': np.random.randint(0, 2)
        }
        
        # Known pattern: positive trend + positive RSI divergence -> up
        score = features['trend_score'] + features['rsi_divergence']
        label = 1 if score > 0 else 0
        
        features_list.append(features)
        labels.append(label)
    
    return features_list, labels


def test_training():
    """Test model training."""
    print("Testing training...")
    
    features_list, labels = generate_synthetic_data(200)
    
    predictor = PricePredictor()
    metrics = predictor.train(features_list, labels)
    
    assert metrics['accuracy'] > 0.5, f"Accuracy too low: {metrics['accuracy']}"
    assert metrics['n_samples'] == 200, f"Wrong sample count: {metrics['n_samples']}"
    assert predictor.is_ready(), "Model should be ready after training"
    
    print(f"  ✓ Training successful: accuracy={metrics['accuracy']:.4f}")
    return predictor


def test_prediction(predictor: PricePredictor):
    """Test prediction."""
    print("Testing prediction...")
    
    test_features = {
        'rsi': 50.0,
        'macd_histogram': 0.0,
        'bb_position': 0.0,
        'volume_ratio': 1.0,
        'obv_divergence': 0.0,
        'consolidation_score': 0.5,
        'bb_squeeze': 0.5,
        'rsi_divergence': 0.5,  # positive
        'orderbook_imbalance': 0.0,
        'sentiment_score': 0.0,
        'trend_score': 0.5,  # positive
        'price_action_score': 0.0,
        'hmm_regime': 2,
        'fear_greed': 50.0,
        'btc_trend': 1,
        'volatility_24h': 0.02,
        'volume_surge': 1
    }
    
    result = predictor.predict(test_features)
    
    assert 'direction' in result, "Missing direction"
    assert result['direction'] in ['up', 'down'], f"Invalid direction: {result['direction']}"
    assert 'confidence' in result, "Missing confidence"
    assert 0 <= result['confidence'] <= 1, f"Invalid confidence: {result['confidence']}"
    assert 'prob_up' in result, "Missing prob_up"
    assert 0 <= result['prob_up'] <= 1, f"Invalid prob_up: {result['prob_up']}"
    
    print(f"  ✓ Prediction successful: direction={result['direction']}, confidence={result['confidence']:.4f}")
    return result


def test_save_load():
    """Test model persistence."""
    print("Testing save/load...")
    
    predictor = PricePredictor()
    features_list, labels = generate_synthetic_data(200)
    predictor.train(features_list, labels)
    
    test_path = '/tmp/test_lgbm_model.pkl'
    
    # Save
    predictor.save_model(test_path)
    
    # Load into new predictor
    new_predictor = PricePredictor()
    new_predictor.load_model(test_path)
    
    assert new_predictor.is_ready(), "Loaded model should be ready"
    
    # Test prediction with loaded model
    test_features = {
        'rsi': 50.0, 'macd_histogram': 0.0, 'bb_position': 0.0,
        'volume_ratio': 1.0, 'obv_divergence': 0.0, 'consolidation_score': 0.5,
        'bb_squeeze': 0.5, 'rsi_divergence': 0.5, 'orderbook_imbalance': 0.0,
        'sentiment_score': 0.0, 'trend_score': 0.5, 'price_action_score': 0.0,
        'hmm_regime': 2, 'fear_greed': 50.0, 'btc_trend': 1,
        'volatility_24h': 0.02, 'volume_surge': 1
    }
    
    result = new_predictor.predict(test_features)
    assert 'direction' in result
    
    print(f"  ✓ Save/load successful")
    
    # Cleanup
    os.remove(test_path)


def test_minimum_samples():
    """Test minimum sample validation."""
    print("Testing minimum samples validation...")
    
    predictor = PricePredictor()
    
    try:
        predictor.train([{f: 0.0 for f in ALL_FEATURES}] * 50, [0] * 50)
        assert False, "Should raise ValueError"
    except ValueError as e:
        print(f"  ✓ Minimum samples validation works: {e}")


def test_not_ready():
    """Test prediction before training."""
    print("Testing prediction before training...")
    
    predictor = PricePredictor()
    
    try:
        predictor.predict({f: 0.0 for f in ALL_FEATURES})
        assert False, "Should raise RuntimeError"
    except RuntimeError as e:
        print(f"  ✓ Not ready validation works: {e}")


if __name__ == '__main__':
    print("=" * 60)
    print("LightGBM Price Predictor Verification")
    print("=" * 60)
    print()
    
    try:
        predictor = test_training()
        test_prediction(predictor)
        test_save_load()
        test_minimum_samples()
        test_not_ready()
        
        print()
        print("=" * 60)
        print("All tests passed! ✓")
        print("=" * 60)
        sys.exit(0)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"Test failed: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        sys.exit(1)
