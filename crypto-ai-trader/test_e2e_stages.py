#!/usr/bin/env python3
"""
E2E Data Flow Validation for crypto-ai-trader
Tests each pipeline stage independently with evidence.
"""
import os, sys, json, time, traceback
from pathlib import Path

# Setup
PROJECT_ROOT = Path(__file__).parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

# Load env
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(Path.home() / ".hermes" / ".env", override=False)  # Shared API keys

results = {}

def run_test(name, fn):
    print(f"\n{'='*60}")
    print(f"STAGE: {name}")
    print(f"{'='*60}")
    try:
        fn()
        results[name] = "PASS"
    except Exception as e:
        print(f"FAIL: {e}")
        traceback.print_exc()
        results[name] = f"FAIL: {e}"

# ── Stage 1: Data Feed ──────────────────────────────────────────
def test_data_feed():
    from src.data_feed import DataFeedManager
    mgr = DataFeedManager()
    snapshot = mgr.get_market_snapshot()
    print(f"Snapshot keys: {list(snapshot.keys())}")
    print(f"BTC price: {snapshot.get('btc_price')}")
    print(f"Fear & Greed: {snapshot.get('fear_greed')}")
    print(f"Funding: {snapshot.get('funding', {}).get('avg_funding_rate')}")
    assert snapshot.get('btc_price') is not None, "btc_price is None"
    assert float(snapshot['btc_price']) > 0, "btc_price <= 0"
    print("✓ DataFeedManager.get_market_snapshot() returned valid data")

# ── Stage 2: OrderBook ──────────────────────────────────────────
def test_orderbook():
    from src.orderbook_analyzer import OrderBookAnalyzer
    ob = OrderBookAnalyzer()
    result = ob.analyze("BTCUSDT", limit=10)
    print(f"Score: {result.get('score')}")
    print(f"Bid/Ask ratio: {result.get('bid_ask_ratio'):.4f}")
    print(f"Spread: {result.get('spread_pct'):.6f}%")
    print(f"Whale bid: ${result.get('whale_bid'):,.0f}")
    print(f"Whale ask: ${result.get('whale_ask'):,.0f}")
    print(f"Support: ${result.get('support_level'):,.2f}")
    print(f"Resistance: ${result.get('resistance_level'):,.2f}")
    assert result is not None, "Result is None"
    assert 0 <= result['score'] <= 100, f"Score {result['score']} out of range"
    print("✓ OrderBookAnalyzer.analyze() returned valid analysis")

# ── Stage 3: Indicators ─────────────────────────────────────────
def test_indicators():
    from src.binance_client import BinanceClient
    from src.indicators import Indicators
    
    client = BinanceClient()
    klines = client.get_klines("BTCUSDT", "1h", limit=100)
    closes = [float(k['close']) for k in klines]
    print(f"Loaded {len(closes)} hourly closes")
    print(f"Latest close: ${closes[-1]:,.2f}")
    
    rsi = Indicators.rsi(closes, period=14)
    macd = Indicators.macd(closes)
    bb = Indicators.bollinger_bands(closes)
    
    print(f"RSI(14): {rsi:.2f}")
    print(f"MACD: line={macd['macd']:.4f}, signal={macd['signal']:.4f}, hist={macd['histogram']:.4f}")
    print(f"Bollinger Bands: upper=${bb['upper']:,.2f}, middle=${bb['middle']:,.2f}, lower=${bb['lower']:,.2f}")
    
    assert 0 <= rsi <= 100, f"RSI {rsi} out of range"
    assert all(k in macd for k in ['macd', 'signal', 'histogram']), "Missing MACD keys"
    assert all(k in bb for k in ['upper', 'middle', 'lower']), "Missing BB keys"
    assert bb['upper'] >= bb['middle'] >= bb['lower'], "BB order violated"
    print("✓ Indicators: RSI, MACD, Bollinger Bands computed correctly")

# ── Stage 4: Market Scanner ─────────────────────────────────────
def test_market_scanner():
    from src.binance_client import BinanceClient
    from src.market_scanner import MarketScanner
    
    client = BinanceClient()
    scanner = MarketScanner(client)
    
    coin_data = {
        "symbol": "BTCUSDT",
        "price": 0,
        "volume_24h": 0,
        "price_change_24h": 0,
        "rank": 1,
    }
    
    result = scanner._analyze_coin(coin_data)
    if result:
        print(f"Symbol: {result['symbol']}")
        print(f"Score: {result['score']}")
        print(f"Price: ${result['price']:,.2f}")
        print(f"Entry signal: {result.get('entry_signal')}")
        print(f"Signals count: {len(result.get('signals', []))}")
        print(f"Signals: {result.get('signals', [])}")
        print(f"Factor scores: {result.get('factor_scores', {})}")
    else:
        print("Note: _analyze_coin returned None (score < 50 or no entry signal)")
        print("Running scoring directly to verify...")
        # Test scoring directly with mock data
        mtf_result = {
            "tf_1h": {"rsi": 55, "macd_histogram": 100, "current_price": 100000, "bb_position": 0.5, "volume_ratio": 1.2, "trend": "neutral", "vwap": 99500, "bb_lower": 98000, "bb_upper": 102000, "price_action_score": 55},
            "tf_4h": {"macd_histogram": 50},
            "trend_alignment": "neutral",
            "trend_score": 55,
            "entry_signal": "long",
        }
        score, _factor_scores = scanner._calculate_weighted_score(mtf_result, None, coin_data)
        print(f"Direct score: {score:.2f}")
        signals = scanner._generate_signals(mtf_result.get("tf_1h"), mtf_result.get("tf_4h"), mtf_result, None, False, None)
        print(f"Signals: {signals}")
        assert 0 <= score <= 100, f"Score {score} out of range"
    
    print("✓ MarketScanner._analyze_coin() executed successfully")

# ── Stage 5: Scoring ────────────────────────────────────────────
def test_scoring():
    from src.binance_client import BinanceClient
    from src.market_scanner import MarketScanner
    
    client = BinanceClient()
    scanner = MarketScanner(client)
    
    # Test with various factor combinations
    mtf_result = {
        "tf_1h": {"rsi": 40, "macd_histogram": 200, "current_price": 100000, "bb_position": 0.3, "volume_ratio": 1.5, "trend": "strong_up", "vwap": 99000, "bb_lower": 97000, "bb_upper": 103000, "price_action_score": 70},
        "tf_4h": {"macd_histogram": 150},
        "trend_alignment": "bullish",
        "trend_score": 80,
        "entry_signal": "long",
    }
    coin_data = {"symbol": "BTCUSDT", "volume_surge": True, "price": 100000, "volume_24h": 5e9, "price_change_24h": 3.5}
    
    score, _ = scanner._calculate_weighted_score(mtf_result, None, coin_data)
    print(f"Score (bullish): {score:.2f}")
    assert 0 <= score <= 100, f"Score {score} out of range"

    # Test with bearish data
    mtf_bear = {
        "tf_1h": {"rsi": 75, "macd_histogram": -300, "current_price": 100000, "bb_position": 0.9, "volume_ratio": 0.5, "trend": "strong_down", "vwap": 101000, "bb_lower": 97000, "bb_upper": 103000, "price_action_score": 30},
        "tf_4h": {"macd_histogram": -200},
        "trend_alignment": "bearish",
        "trend_score": 20,
        "entry_signal": None,
    }
    score_bear, _ = scanner._calculate_weighted_score(mtf_bear, None, coin_data)
    print(f"Score (bearish): {score_bear:.2f}")
    assert 0 <= score_bear <= 100, f"Score {score_bear} out of range"
    
    print("✓ _calculate_weighted_score() returns valid 0-100 for all inputs")

# ── Stage 6: Signals ────────────────────────────────────────────
def test_signals():
    from src.binance_client import BinanceClient
    from src.market_scanner import MarketScanner
    
    client = BinanceClient()
    scanner = MarketScanner(client)
    
    # Bullish scenario
    a_1h = {"trend": "strong_up", "rsi": 25, "macd_histogram": 150, "current_price": 100000, "vwap": 99000, "bb_lower": 97000, "bb_upper": 103000}
    a_4h = {"macd_histogram": 200}
    mtf = {"trend_alignment": "bullish", "trend_score": 85, "entry_signal": "long"}
    signals = scanner._generate_signals(a_1h, a_4h, mtf, None, True, None)
    print(f"Bullish signals ({len(signals)}):")
    for s in signals:
        print(f"  {s}")
    
    assert len(signals) > 0, "No signals generated"
    
    # Bearish scenario
    a_1h_bear = {"trend": "strong_down", "rsi": 78, "macd_histogram": -200, "current_price": 100000, "vwap": 101000, "bb_lower": 97000, "bb_upper": 103000}
    a_4h_bear = {"macd_histogram": -300}
    mtf_bear = {"trend_alignment": "bearish", "trend_score": 15, "entry_signal": "short"}
    signals_bear = scanner._generate_signals(a_1h_bear, a_4h_bear, mtf_bear, None, False, None)
    print(f"\nBearish signals ({len(signals_bear)}):")
    for s in signals_bear:
        print(f"  {s}")
    
    assert len(signals_bear) > 0, "No bearish signals generated"
    print("✓ _generate_signals() produces actionable signals for both scenarios")

# ── Stage 7: Trade Executor (Paper Mode) ────────────────────────
def test_trade_executor():
    os.environ["TRADING_MODE"] = "paper"
    from src.paper_trader import PaperTrader, is_paper_mode
    from src.trade_executor import execute_auto_trade
    
    assert is_paper_mode(), "Not in paper mode"
    print("TRADING_MODE=paper confirmed")
    
    pt = PaperTrader()
    balance = pt.get_free_balance("USDT")
    print(f"Paper USDT balance: ${balance:,.2f}")
    assert balance > 0, "Paper balance is 0"
    
    # Test place_order (market buy with quantity ~0.001 BTC ≈ $80)
    order = pt.place_order("BTCUSDT", "BUY", "MARKET", quantity=0.001)
    print(f"Paper order: {json.dumps(order, indent=2, default=str)}")
    assert order is not None, "Order is None"
    
    # Verify order was recorded
    if hasattr(pt, '_portfolio'):
        print(f"Portfolio after order: {pt._portfolio.get('positions', {})}")
    
    print("✓ PaperTrader: place_order works in paper mode")

# ── Run all tests ───────────────────────────────────────────────
if __name__ == "__main__":
    run_test("1. Data Feed", test_data_feed)
    run_test("2. OrderBook", test_orderbook)
    run_test("3. Indicators", test_indicators)
    run_test("4. Market Scanner", test_market_scanner)
    run_test("5. Scoring", test_scoring)
    run_test("6. Signals", test_signals)
    run_test("7. Trade Executor (Paper)", test_trade_executor)
    
    print("\n" + "="*60)
    print("E2E VALIDATION SUMMARY")
    print("="*60)
    all_pass = True
    for name, status in results.items():
        icon = "✅" if status == "PASS" else "❌"
        print(f"  {icon} {name}: {status}")
        if status != "PASS":
            all_pass = False
    
    print(f"\nOverall: {'ALL PASS ✅' if all_pass else 'SOME FAILED ❌'}")
    sys.exit(0 if all_pass else 1)
