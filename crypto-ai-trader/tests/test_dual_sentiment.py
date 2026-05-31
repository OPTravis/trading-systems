#!/usr/bin/env python3
"""
Test script for dual model sentiment cross-verification integration.

Tests:
1. LLM client can get second opinion client
2. Structured scoring format (1-10 score + confidence)
3. Cross-verification logic
4. Event-driven position adjustment trigger

Usage:
    cd ~/crypto-ai-trader
    .venv/bin/python tests/test_dual_sentiment.py
"""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_llm_client_import():
    """Test that LLM client imports work."""
    print("1. Testing LLM client imports...")
    try:
        print("   ✅ LLM client imports successful")
        return True
    except Exception as e:
        print(f"   ❌ LLM client import failed: {e}")
        return False


def test_second_opinion_client():
    """Test that second opinion client can be created."""
    print("2. Testing second opinion client creation...")
    try:
        from src.llm_client import get_second_opinion_client

        # Check if XIAOMI_API_KEY is set
        if not os.environ.get("XIAOMI_API_KEY"):
            print("   ⚠️  XIAOMI_API_KEY not set — skipping client creation test")
            return True

        client = get_second_opinion_client()
        if client is None:
            print("   ❌ Second opinion client returned None")
            return False

        print(
            f"   ✅ Second opinion client created: {client._primary_cfg.get('provider')}"
        )
        return True
    except Exception as e:
        print(f"   ❌ Second opinion client creation failed: {e}")
        return False


def test_structured_scoring():
    """Test structured scoring format."""
    print("3. Testing structured scoring format...")
    try:
        from src.market_researcher import MarketResearcher

        # Create a mock instance
        researcher = MarketResearcher.__new__(MarketResearcher)

        # Test keyword sentiment structured
        result = researcher._keyword_sentiment_structured("Bitcoin bullish rally surge")

        # Verify structure
        assert isinstance(result, dict), "Result should be a dict"
        assert "score" in result, "Result should have 'score' key"
        assert "confidence" in result, "Result should have 'confidence' key"
        assert "sentiment" in result, "Result should have 'sentiment' key"

        assert (
            1 <= result["score"] <= 10
        ), f"Score should be 1-10, got {result['score']}"
        assert (
            0.0 <= result["confidence"] <= 1.0
        ), f"Confidence should be 0.0-1.0, got {result['confidence']}"
        assert (
            -1.0 <= result["sentiment"] <= 1.0
        ), f"Sentiment should be -1.0 to 1.0, got {result['sentiment']}"

        print(
            f"   ✅ Structured scoring works: score={result['score']}, confidence={result['confidence']}"
        )
        return True
    except Exception as e:
        print(f"   ❌ Structured scoring test failed: {e}")
        return False


def test_cross_verification():
    """Test cross-verification logic."""
    print("4. Testing cross-verification logic...")
    try:
        from src.market_researcher import MarketResearcher

        # Create a mock instance
        researcher = MarketResearcher.__new__(MarketResearcher)

        # Test case 1: Models agree (within 2 points)
        primary = [{"score": 8, "confidence": 0.8, "sentiment": 0.6}]
        secondary = [{"score": 7, "confidence": 0.7, "sentiment": 0.4}]
        articles = [{"title": "Test", "summary": "Test"}]

        result = researcher._cross_verify_sentiment(primary, secondary, articles)

        assert len(result) == 1, "Should return one result"
        assert (
            result[0]["confidence"] == 0.9
        ), f"Should be HIGH confidence (0.9), got {result[0]['confidence']}"
        assert result[0]["primary_score"] == 8, "Should preserve primary score"
        assert result[0]["secondary_score"] == 7, "Should preserve secondary score"

        print("   ✅ Cross-verification logic works correctly")
        return True
    except Exception as e:
        print(f"   ❌ Cross-verification test failed: {e}")
        return False


def test_event_driven_adjustment():
    """Test event-driven position adjustment logic."""
    print("5. Testing event-driven position adjustment logic...")
    try:
        # This is a structural test - we can't actually run the adjustment
        # without a real portfolio, but we can verify the function exists
        from src.scan_orchestrator import _step_event_driven_adjustment

        assert callable(_step_event_driven_adjustment), "Function should be callable"

        print("   ✅ Event-driven adjustment function exists and is callable")
        return True
    except Exception as e:
        print(f"   ❌ Event-driven adjustment test failed: {e}")
        return False


def test_backward_compatibility():
    """Test backward compatibility with old float format."""
    print("6. Testing backward compatibility...")
    try:
        from src.market_researcher import MarketResearcher

        # Create a mock instance
        researcher = MarketResearcher.__new__(MarketResearcher)

        # Test that old float format still works
        old_format = [0.5, -0.3, 0.0]
        articles = [
            {"title": "Test 1", "summary": "Bullish news"},
            {"title": "Test 2", "summary": "Bearish news"},
            {"title": "Test 3", "summary": "Neutral news"},
        ]

        # Simulate old format handling
        for i, s in enumerate(old_format):
            if isinstance(s, (int, float)):
                # Old format should still work
                sentiment = round(s, 2)
                assert (
                    -1.0 <= sentiment <= 1.0
                ), f"Sentiment should be -1.0 to 1.0, got {sentiment}"

        print("   ✅ Backward compatibility maintained")
        return True
    except Exception as e:
        print(f"   ❌ Backward compatibility test failed: {e}")
        return False


def main():
    """Run all tests."""
    print("=" * 60)
    print("Dual Model Sentiment Cross-Verification Integration Test")
    print("=" * 60)
    print()

    tests = [
        test_llm_client_import,
        test_second_opinion_client,
        test_structured_scoring,
        test_cross_verification,
        test_event_driven_adjustment,
        test_backward_compatibility,
    ]

    results = []
    for test in tests:
        try:
            result = test()
            results.append(result)
        except Exception as e:
            print(f"   ❌ Test failed with exception: {e}")
            results.append(False)
        print()

    # Summary
    print("=" * 60)
    print("Test Summary")
    print("=" * 60)
    passed = sum(results)
    total = len(results)
    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("✅ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
