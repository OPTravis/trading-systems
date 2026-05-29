#!/usr/bin/env python3
"""
驗證腳本：確認 On-Chain + Sentiment 修改正確集成到 data_feed / market_scanner
"""

import sys
sys.path.insert(0, "/home/travis/crypto-ai-trader")

def test_data_feed():
    from src.data_feed import DataFeedManager, DeFiLlamaOnChain

    # 1. DeFiLlamaOnChain 獨立測試
    oc = DeFiLlamaOnChain()
    score = oc.get_onchain_score()
    assert 0.0 <= score <= 100.0, f"On-chain score out of range: {score}"
    print(f"✅ DeFiLlamaOnChain.get_onchain_score() = {score:.1f}")

    # 2. DataFeedManager 包含 onchain 屬性
    mgr = DataFeedManager()
    assert hasattr(mgr, "onchain"), "DataFeedManager missing 'onchain' attribute"
    assert hasattr(mgr.onchain, "get_onchain_score"), "onchain missing get_onchain_score"
    print("✅ DataFeedManager.onchain 存在且方法齊全")

    # 3. get_market_snapshot 包含 onchain_score
    snap = mgr.get_market_snapshot()
    assert "onchain_score" in snap, "snapshot missing onchain_score"
    assert snap["onchain_score"] is not None, "onchain_score is None"
    assert 0.0 <= snap["onchain_score"] <= 100.0, f"snapshot onchain_score out of range"
    print(f"✅ get_market_snapshot()['onchain_score'] = {snap['onchain_score']:.1f}")

    # 4. Fear & Greed 存在
    assert "fear_greed" in snap, "snapshot missing fear_greed"
    print(f"✅ F&G value = {snap['fear_greed']['value'] if snap['fear_greed'] else 'N/A'}")

    mgr.close()
    return True


def test_market_scanner_signature():
    import inspect
    from src.market_scanner import MarketScanner

    sig = inspect.signature(MarketScanner._calculate_weighted_score)
    params = list(sig.parameters.keys())
    assert "market_sentiment_score" in params, "Missing market_sentiment_score param"
    assert "onchain_score" in params, "Missing onchain_score param"
    print("✅ _calculate_weighted_score 簽名包含 market_sentiment_score + onchain_score")

    # 測試默認值調用（不傳新參數時應返回舊行為）
    # 需要 mock mtf_result
    mtf = {"tf_1h": {"rsi": 50, "macd_histogram": 0, "current_price": 100, "vwap": 90,
                     "ma7": 110, "ma25": 100, "ma99": 90, "volatility_pct": 5, "momentum": 1},
           "tf_4h": {"ma99": 95, "ma25": 98},
           "trend_score": 60}
    scanner = MarketScanner.__new__(MarketScanner)
    score_default = scanner._calculate_weighted_score(mtf, None, {}, {})
    assert 0.0 <= score_default <= 100.0, f"Default score out of range: {score_default}"
    print(f"✅ Default call score = {score_default:.1f}")

    # 測試新參數調用
    score_new = scanner._calculate_weighted_score(
        mtf, None, {}, {}, market_sentiment_score=80.0, onchain_score=70.0
    )
    assert 0.0 <= score_new <= 100.0, f"New params score out of range: {score_new}"
    print(f"✅ With onchain=70 sentiment=80 score = {score_new:.1f}")

    return True


if __name__ == "__main__":
    print("=" * 60)
    print("驗證：On-Chain + Sentiment 集成")
    print("=" * 60)

    ok1 = test_data_feed()
    ok2 = test_market_scanner_signature()

    if ok1 and ok2:
        print("\n🎉 全部驗證通過")
        sys.exit(0)
    else:
        print("\n❌ 驗證失敗")
        sys.exit(1)
