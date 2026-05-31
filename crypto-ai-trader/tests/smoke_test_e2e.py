#!/usr/bin/env python3
"""
Smoke Test — E2E 全链路: 扫描→研究→执行
Verifies the full cron-scan pipeline runs end-to-end in PAPER mode.
"""

import os
import sys
import time
import traceback

# Force paper mode for safety
os.environ["TRADING_MODE"] = "paper"
os.environ["AUTO_EXECUTE"] = "false"  # Don't auto-execute even in paper mode

# Add project root to path (parent of tests/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
ERRORS = []


def test(name, fn):
    global PASS, FAIL
    try:
        result = fn()
        if result is False:
            FAIL += 1
            ERRORS.append(f"FAIL: {name}")
            print(f"  ❌ FAIL: {name}")
        else:
            PASS += 1
            print(f"  ✅ PASS: {name}")
    except Exception as e:
        FAIL += 1
        ERRORS.append(f"ERROR: {name} — {e}")
        print(f"  ❌ ERROR: {name} — {e}")
        traceback.print_exc()


# ===================================================================
# Phase 1: Import & Module Load Tests
# ===================================================================
print("\n" + "=" * 60)
print("Phase 1: Import & Module Load Tests")
print("=" * 60)

test("Import BinanceClient", lambda: __import__("src.binance_client"))
test("Import MarketScanner", lambda: __import__("src.market_scanner"))
test("Import PortfolioManager", lambda: __import__("src.portfolio"))
test("Import TradeExecutor", lambda: __import__("src.trade_executor"))
test("Import RiskManager", lambda: __import__("src.risk_manager"))
test("Import SentimentAnalyzer", lambda: __import__("src.sentiment"))
test("Import StrategyAdaptor", lambda: __import__("src.strategy_adaptor"))
test("Import MarketResearcher", lambda: __import__("src.market_researcher"))
test("Import BearAnalyst", lambda: __import__("src.bear_analyst"))
test("Import StrategyRegistry", lambda: __import__("src.strategy_registry"))
test("Import ScanOrchestrator", lambda: __import__("src.scan_orchestrator"))
test("Import PaperTrader", lambda: __import__("src.paper_trader"))
test("Import TradeJournal", lambda: __import__("src.trade_journal"))
test("Import DimensionScorer", lambda: __import__("src.dimension_scorer"))
test("Import PositionOptimizer", lambda: __import__("src.position_optimizer"))
test("Import TradeOutcomeRecorder", lambda: __import__("src.trade_outcome_recorder"))
test("Import MultiTimeframeAnalyzer", lambda: __import__("src.multi_timeframe"))
test("Import SelfHealer", lambda: __import__("src.self_healer"))
test("Import HMM Regime", lambda: __import__("src.hmm_regime"))


# ===================================================================
# Phase 2: Paper Trader Initialization
# ===================================================================
print("\n" + "=" * 60)
print("Phase 2: Paper Trader Initialization")
print("=" * 60)

test(
    "PaperTrader.is_paper_mode() returns True",
    lambda: __import__("src.paper_trader").is_paper_mode() is True,
)


def test_paper_trader_init():
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    assert pt is not None, "PaperTrader instance is None"
    bal = pt.get_balance("USDT")
    assert bal > 0, f"Paper balance should be > 0, got {bal}"
    return True


test("PaperTrader initializes with balance", test_paper_trader_init)


def test_paper_trader_simulates_order():
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    # Get BTC price to verify market data works
    price = pt.get_current_price("BTCUSDT")
    assert price > 0, f"BTC price should be > 0, got {price}"
    return True


test("PaperTrader fetches real BTC price", test_paper_trader_simulates_order)


# ===================================================================
# Phase 3: Core Component Tests
# ===================================================================
print("\n" + "=" * 60)
print("Phase 3: Core Component Tests")
print("=" * 60)


def test_strategy_adaptor():
    from src.strategy_adaptor import StrategyAdaptor

    adaptor = StrategyAdaptor()
    result = adaptor.adapt(
        fear_greed=45,
        btc_trend="NEUTRAL",
        btc_price_change_24h=-1.5,
        btc_adx=25,
        funding_rate=0.01,
        btc_score=52,
    )
    assert "regime" in result, f"Missing 'regime' in adapt result: {result.keys()}"
    assert "strategies" in result, "Missing 'strategies' in adapt result"
    assert "global" in result, "Missing 'global' in adapt result"
    regime = result["regime"]
    strategies = result["strategies"]
    enabled_count = sum(1 for s in strategies.values() if s.get("enabled"))
    print(f"    Regime={regime}, enabled_strategies={enabled_count}")
    return True


test("StrategyAdaptor.adapt() works", test_strategy_adaptor)


def test_risk_manager_init():
    from src.paper_trader import PaperTrader
    from src.risk_manager import RiskManager

    pt = PaperTrader()
    rm = RiskManager(binance_client=pt)
    assert rm is not None, "RiskManager is None"
    return True


test("RiskManager initializes", test_risk_manager_init)


def test_market_researcher_init():
    from src.market_researcher import MarketResearcher

    mr = MarketResearcher()
    assert mr is not None, "MarketResearcher is None"
    return True


test("MarketResearcher initializes", test_market_researcher_init)


def test_bear_analyst_init():
    from src.bear_analyst import BearAnalyst

    ba = BearAnalyst()
    assert ba is not None, "BearAnalyst is None"
    return True


test("BearAnalyst initializes", test_bear_analyst_init)


def test_sentiment_analyzer():
    from src.sentiment import SentimentAnalyzer

    sa = SentimentAnalyzer()
    market = sa.get_market_sentiment()
    assert "fear_greed" in market, f"Missing fear_greed in sentiment: {market.keys()}"
    fng = market["fear_greed"]
    assert 0 <= fng <= 100, f"Fear & Greed out of range: {fng}"
    print(f"    F&G={fng}")
    return True


test("SentimentAnalyzer.get_market_sentiment()", test_sentiment_analyzer)


def test_trade_journal():
    from src.trade_journal import TradeJournal

    tj = TradeJournal()
    assert tj is not None, "TradeJournal is None"
    return True


test("TradeJournal initializes", test_trade_journal)


def test_strategy_registry():
    from src.strategy_registry import StrategyRegistry

    sr = StrategyRegistry()
    assert sr is not None, "StrategyRegistry is None"
    return True


test("StrategyRegistry initializes", test_strategy_registry)


# ===================================================================
# Phase 4: Market Data Tests
# ===================================================================
print("\n" + "=" * 60)
print("Phase 4: Market Data Tests (Live Binance API)")
print("=" * 60)


def test_btc_klines():
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    klines = pt.get_klines("BTCUSDT", "1h", limit=10)
    assert len(klines) >= 5, f"Expected >=5 klines, got {len(klines)}"
    print(f"    Got {len(klines)} BTCUSDT 1h klines")
    return True


test("BTC klines fetch", test_btc_klines)


def test_btc_24hr_stats():
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    stats = pt.get_24hr_stats("BTCUSDT")
    assert stats, "24hr stats is empty"
    last_price = float(stats.get("last_price", 0))
    assert last_price > 0, f"BTC last_price should be > 0, got {last_price}"
    print(f"    BTC 24h price: ${last_price:,.2f}")
    return True


test("BTC 24hr stats", test_btc_24hr_stats)


def test_market_scanner_scan():
    from src.market_scanner import MarketScanner
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    scanner = MarketScanner(pt)
    opps = scanner.scan_all()
    assert isinstance(opps, list), f"scan_all should return list, got {type(opps)}"
    print(f"    Found {len(opps)} raw opportunities")
    if opps:
        top = opps[0]
        print(f"    Top: {top.get('symbol','?')} score={top.get('score',0)}")
    return True


test("MarketScanner.scan_all()", test_market_scanner_scan)


def test_top_movers():
    from src.market_scanner import MarketScanner
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    scanner = MarketScanner(pt)
    movers = scanner.get_top_movers(limit=5)
    assert isinstance(movers, list), "get_top_movers should return list"
    print(f"    Got {len(movers)} top movers")
    return True


test("MarketScanner.get_top_movers()", test_top_movers)


# ===================================================================
# Phase 5: Strategy Pipeline Tests
# ===================================================================
print("\n" + "=" * 60)
print("Phase 5: Strategy Pipeline Tests")
print("=" * 60)


def test_pre_trade_check():
    from src.paper_trader import PaperTrader
    from src.risk_manager import RiskManager

    pt = PaperTrader()
    rm = RiskManager(binance_client=pt)
    check = rm.pre_trade_check(
        symbol="BTCUSDT",
        price=100000.0,
        atr=1500.0,
        positions=[],
        score=75,
        strategy="trend",
    )
    assert "allowed" in check, f"pre_trade_check missing 'allowed': {check.keys()}"
    print(f"    pre_trade_check: allowed={check['allowed']}")
    return True


test("RiskManager.pre_trade_check()", test_pre_trade_check)


def test_dimension_scorer():
    from src.dimension_scorer import DimensionScorer
    from src.paper_trader import PaperTrader

    pt = PaperTrader()
    ds = DimensionScorer(binance_client=pt)
    result = ds.score_all()
    assert result is not None, "DimensionScorer.score_all() returned None"
    report = ds.format_report(result)
    assert report and len(report) > 0, "format_report returned empty"
    print(f"    Dimension report length: {len(report)} chars")
    return True


test("DimensionScorer.score_all()", test_dimension_scorer)


def test_position_optimizer_init():
    from src.market_scanner import MarketScanner
    from src.paper_trader import PaperTrader
    from src.position_optimizer import PositionOptimizer

    pt = PaperTrader()
    scanner = MarketScanner(pt)
    from src.portfolio import PortfolioManager

    pm = PortfolioManager()
    po = PositionOptimizer(binance_client=pt, portfolio=pm, market_scanner=scanner)
    assert po is not None, "PositionOptimizer is None"
    return True


test("PositionOptimizer initializes", test_position_optimizer_init)


# ===================================================================
# Phase 6: Full E2E Pipeline (cron-scan)
# ===================================================================
print("\n" + "=" * 60)
print("Phase 6: Full E2E Pipeline (cron-scan in paper mode)")
print("=" * 60)


def test_full_pipeline():
    from src.scan_orchestrator import cmd_cron_scan

    print("    Running cmd_cron_scan() in paper mode...")
    start = time.time()
    cmd_cron_scan()
    elapsed = time.time() - start
    print(f"    Pipeline completed in {elapsed:.1f}s")
    return True


test("Full E2E pipeline (cron-scan)", test_full_pipeline)


# ===================================================================
# Summary
# ===================================================================
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"  ✅ Passed: {PASS}")
print(f"  ❌ Failed: {FAIL}")
print(f"  Total:    {PASS + FAIL}")

if ERRORS:
    print("\n  Failures:")
    for err in ERRORS:
        print(f"    - {err}")

print()
sys.exit(0 if FAIL == 0 else 1)
