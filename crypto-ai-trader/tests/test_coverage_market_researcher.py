"""
Tests for MarketResearcher — targeting the 452 missed lines.
"""

import json
import os
import time
from unittest.mock import MagicMock


class TestMarketResearcher:
    def test_init(self):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher()
        assert mr is not None
        assert mr.CACHE_TTL == 3600

    def test_constants(self):
        from src.market_researcher import MarketResearcher

        assert MarketResearcher.MAX_ADJUSTMENT == 15.0
        assert MarketResearcher.MIN_ADJUSTMENT == -15.0
        assert MarketResearcher.RESEARCH_TIMEOUT == 60

    def test_research_cache_hit(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {"BTC": {"symbol": "BTCUSDT", "score_adjustment": 5.0}}
        mr._cache_ts = {"BTC": time.time()}
        mr.CACHE_TTL = 3600
        result = mr.research("BTCUSDT")
        assert result["symbol"] == "BTCUSDT"

    def test_research_cache_expired(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {"BTC": {"symbol": "BTCUSDT"}}
        mr._cache_ts = {"BTC": time.time() - 7200}  # expired
        mr.CACHE_TTL = 3600
        mr.RESEARCH_TIMEOUT = 5
        # Mock the internal research methods
        mr._research_news = MagicMock(return_value=[])
        mr._research_onchain = MagicMock(return_value={})
        mr._research_catalysts = MagicMock(return_value=[])
        mr._calculate_adjustment = MagicMock(return_value=(0.0, "LOW"))
        mr._summarize = MagicMock(return_value="No data")
        result = mr.research("BTCUSDT")
        assert result["symbol"] == "BTCUSDT"

    def test_research_full_pipeline(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr.CACHE_TTL = 3600
        mr.RESEARCH_TIMEOUT = 30
        mr._research_news = MagicMock(return_value=[{"title": "BTC rally"}])
        mr._research_onchain = MagicMock(return_value={"whale_activity": "high"})
        mr._research_catalysts = MagicMock(return_value=[{"event": "halving"}])
        mr._calculate_adjustment = MagicMock(return_value=(5.0, "HIGH"))
        mr._summarize = MagicMock(return_value="Bullish outlook")
        result = mr.research("BTCUSDT")
        assert result["score_adjustment"] == 5.0
        assert result["confidence"] == "HIGH"
        assert len(result["news"]) == 1

    def test_research_timeout(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr.CACHE_TTL = 3600
        mr.RESEARCH_TIMEOUT = 0.001  # very short timeout
        # These will be called but may timeout
        mr._research_news = MagicMock(return_value=[])
        mr._research_onchain = MagicMock(return_value={})
        mr._research_catalysts = MagicMock(return_value=[])
        mr._calculate_adjustment = MagicMock(return_value=(0.0, "LOW"))
        mr._summarize = MagicMock(return_value="Timeout")
        result = mr.research("BTCUSDT")
        assert result["symbol"] == "BTCUSDT"

    def test_load_recent_cache_empty(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr._load_recent_cache()
        assert mr._cache == {}

    def test_load_recent_cache_with_file(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr.CACHE_TTL = 3600
        # Write a recent cache file
        cache_file = mr._research_dir / "BTC_20260601.json"
        cache_file.write_text(json.dumps({"symbol": "BTCUSDT", "score": 5.0}))
        # Set mtime to now
        os.utime(str(cache_file), (time.time(), time.time()))
        mr._load_recent_cache()
        assert "BTC" in mr._cache


class TestSaveJson:
    def test_save_json_success(self, tmp_path):
        from src.market_researcher import _save_json

        filepath = tmp_path / "test.json"
        result = _save_json(filepath, {"key": "value"})
        assert result is True
        assert filepath.exists()
        assert json.loads(filepath.read_text()) == {"key": "value"}

    def test_save_json_failure(self, tmp_path):
        from src.market_researcher import _save_json

        filepath = tmp_path / "nonexistent" / "test.json"
        result = _save_json(filepath, {"key": "value"})
        assert result is False


class TestMarketResearcherHelpers:
    def test_summarize(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        result = mr._summarize(
            news=[{"title": "BTC rally", "sentiment": 0.8}],
            onchain={"whale_activity": "accumulating"},
            catalysts=[
                {"event": "ETF approval", "date": "2026-07-01", "impact": "POSITIVE"}
            ],
        )
        assert isinstance(result, str)

    def test_calculate_adjustment_positive(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr.MAX_ADJUSTMENT = 15.0
        mr.MIN_ADJUSTMENT = -15.0
        news = [{"sentiment": 0.8}] * 5
        onchain = {"whale_activity": "accumulating"}
        catalysts = [{"event": "ETF", "impact": "POSITIVE"}]
        adj, conf = mr._calculate_adjustment(news, onchain, catalysts)
        assert isinstance(adj, float)
        assert isinstance(conf, str)

    def test_calculate_adjustment_negative(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr.MAX_ADJUSTMENT = 15.0
        mr.MIN_ADJUSTMENT = -15.0
        news = [{"sentiment": -0.8}] * 5
        onchain = {"whale_activity": "dumping"}
        catalysts = []
        adj, conf = mr._calculate_adjustment(news, onchain, catalysts)
        assert isinstance(adj, float)

    def test_calculate_adjustment_neutral(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr.MAX_ADJUSTMENT = 15.0
        mr.MIN_ADJUSTMENT = -15.0
        news = []
        onchain = {}
        catalysts = []
        adj, conf = mr._calculate_adjustment(news, onchain, catalysts)
        assert isinstance(adj, float)
        assert isinstance(conf, str)
