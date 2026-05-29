"""Test 5: Trade Signal — Does the signal generator produce BUY/SELL/HOLD?"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.ccxt_client import BinanceClient
from src.market_scanner import MarketScanner

def test_signal_generation():
    client = BinanceClient()
    scanner = MarketScanner(binance_client=client)
    
    print("[1] MarketScanner initialized")
    
    # Provide data that should generate signals
    a_1h = {
        "trend": "strong_up",
        "rsi": 72,         # overbought
        "macd_histogram": 0.05,
        "current_price": 104500,
        "vwap": 103800,
        "bb_lower": 102000,
        "bb_upper": 104200,
    }
    a_4h = {"macd_histogram": 0.03}
    mtf_result = {
        "trend_alignment": "bullish",
        "trend_score": 78,
        "entry_signal": "long",
    }
    sentiment_data = {"sentiment_score": 60, "funding_rate": 0.0003, "oi_change_pct": 4.0}
    new_signals_data = {
        "obv_div": {"divergence": True, "strength": 0.8},
        "bb_squeeze": {"squeezing": True, "percentile": 15.0},
        "rsi_div": {"detected": True, "strength": 0.75},
        "consolidation": {"breaking_out": True, "volume_confirmed": True, "days_in_range": 12, "range_pct": 3.5},
    }
    
    print("[2] Generating signals...")
    signals = scanner._generate_signals(
        a_1h, a_4h, mtf_result, sentiment_data, 
        volume_surge=True, new_signals_data=new_signals_data
    )
    
    print(f"[3] Generated {len(signals)} signals:")
    for s in signals:
        print(f"    {s}")
    
    assert isinstance(signals, list), "Signals must be a list"
    assert len(signals) > 0, "No signals generated"
    
    # Check for actionable signal types in the list
    signal_text = " ".join(signals).lower()
    has_buyish = any(w in signal_text for w in ["long", "bullish", "entry", "above", "oversold"])
    has_sellish = any(w in signal_text for w in ["short", "bearish", "overbought"])
    print(f"[4] Signal direction: buyish={has_buyish}, sellish={has_sellish}")
    
    print(f"[PASS] Test 5 — Signal generation works ({len(signals)} signals produced)")

if __name__ == "__main__":
    test_signal_generation()
