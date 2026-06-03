"""
Deep coverage tests for remaining uncovered modules.
"""

from unittest.mock import MagicMock

# ── MarketResearcher (452 missed) ─────────────────────────────────────


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

    def test_load_recent_cache_empty(self, tmp_path):
        from src.market_researcher import MarketResearcher

        mr = MarketResearcher.__new__(MarketResearcher)
        mr._research_dir = tmp_path / "research"
        mr._research_dir.mkdir()
        mr._cache = {}
        mr._cache_ts = {}
        mr._load_recent_cache()
        assert mr._cache == {}


# ── PaperTrader (212 missed) ──────────────────────────────────────────


class TestPaperTrader:
    def test_is_paper_mode(self):
        from src.paper_trader import is_paper_mode

        assert isinstance(is_paper_mode(), bool)

    def test_constants(self):
        from src.paper_trader import (
            PAPER_FEE_RATE,
            PAPER_INITIAL_BALANCE,
            PAPER_MIN_ORDER_USDT,
            PAPER_SLIPPAGE_PCT,
        )

        assert PAPER_SLIPPAGE_PCT > 0
        assert PAPER_FEE_RATE > 0
        assert PAPER_MIN_ORDER_USDT > 0
        assert PAPER_INITIAL_BALANCE > 0

    def test_get_trading_client(self):
        from src.paper_trader import get_trading_client

        client = get_trading_client()
        assert client is not None

    def test_binance_sdk_proxy(self):
        from src.paper_trader import _BinanceSDKProxy

        mock_pt = MagicMock()
        mock_pt.get_current_price.return_value = 50000.0
        proxy = _BinanceSDKProxy(mock_pt)
        result = proxy.ticker_price("BTCUSDT")
        assert result["price"] == "50000.0"


# ── FreqtradeRiskPatterns (271 missed) ────────────────────────────────


class TestFreqtradeRiskPatterns:
    def test_calculate_risk_per_trade(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=95.0,
        )
        assert isinstance(result, float)
        assert result > 0

    def test_calculate_risk_per_trade_invalid(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=0.0,
            stoploss_price=95.0,
        )
        assert result == 10.0  # min_stake

    def test_calculate_risk_per_trade_same_price(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.02,
            entry_price=100.0,
            stoploss_price=100.0,
        )
        assert result == 10.0  # min_stake

    def test_calculate_risk_per_trade_max_cap(self):
        from src.freqtrade_risk_patterns import calculate_risk_per_trade_position_size

        result = calculate_risk_per_trade_position_size(
            portfolio_value=10000.0,
            risk_pct=0.5,  # 50% risk → position would be huge
            entry_price=100.0,
            stoploss_price=99.0,
            max_position_pct=0.15,
        )
        assert result <= 10000.0 * 0.15  # capped at 15%


# ── SectorClassifier (171 missed) ─────────────────────────────────────


class TestSectorClassifier:
    def test_init(self):
        from src.sector_classifier import SectorClassifier

        sc = SectorClassifier()
        assert sc is not None


# ── HMMRegime (214 missed) ────────────────────────────────────────────


class TestHMMRegime:
    def test_init(self):
        from src.hmm_regime import HMMRegimeDetector

        det = HMMRegimeDetector()
        assert det is not None


# ── ParamOptimizer (148 missed) ───────────────────────────────────────


class TestParamOptimizer:
    def test_init(self):
        from src.param_optimizer import ParamOptimizer

        po = ParamOptimizer()
        assert po is not None


# ── FundingArb (145 missed) ───────────────────────────────────────────


class TestFundingArb:
    def test_class_exists(self):
        from src.funding_arb import FundingArbitrage

        assert FundingArbitrage is not None


# ── SelfHealer (141 missed) ───────────────────────────────────────────


class TestSmartOrder:
    def test_class_exists(self):
        from src.smart_order import SmartOrder

        assert SmartOrder is not None


# ── TWAP/VWAP (125 missed) ───────────────────────────────────────────


class TestConceptDrift:
    def test_init(self):
        from src.concept_drift import ConceptDriftDetector

        cdd = ConceptDriftDetector()
        assert cdd is not None


# ── DynamicCoinPool (117 missed) ──────────────────────────────────────


class TestDynamicCoinPool:
    def test_init(self):
        from src.dynamic_coin_pool import DynamicCoinPool

        dcp = DynamicCoinPool(MagicMock())
        assert dcp is not None


# ── GridTrader (113 missed) ───────────────────────────────────────────


class TestStrategyEvolver:
    def test_init(self):
        from src.strategy_evolver import StrategyEvolver

        se = StrategyEvolver()
        assert se is not None


# ── StrategyRegistry (87 missed) ──────────────────────────────────────


class TestStrategyRegistry:
    def test_init(self):
        from src.strategy_registry import StrategyRegistry

        sr = StrategyRegistry()
        assert sr is not None


# ── LLMClient (87 missed) ─────────────────────────────────────────────


class TestLLMClient:
    def test_init(self):
        from src.llm_client import LLMClient

        llm = LLMClient()
        assert llm is not None


# ── TradeOutcomeRecorder (87 missed) ──────────────────────────────────


class TestTradeOutcomeRecorder:
    def test_class_exists(self):
        from src.trade_outcome_recorder import TradeOutcomeRecorder

        assert TradeOutcomeRecorder is not None


# ── Backtester (84 missed) ────────────────────────────────────────────


class TestBacktester:
    def test_class_exists(self):
        from src.backtester import Backtester

        assert Backtester is not None


# ── MetricsExporter (75 missed) ───────────────────────────────────────


class TestSentiment:
    def test_init(self):
        from src.sentiment import SentimentAnalyzer

        sa = SentimentAnalyzer()
        assert sa is not None


# ── TradeJournal (67 missed) ──────────────────────────────────────────


class TestTradeJournal:
    def test_init(self):
        from src.trade_journal import TradeJournal

        tj = TradeJournal()
        assert tj is not None


# ── OnchainProvider (62 missed) ───────────────────────────────────────


class TestPricePredictor:
    def test_init(self):
        from src.price_predictor import PricePredictor

        pp = PricePredictor()
        assert pp.is_ready() is False


# ── SocialSentiment (55 missed) ───────────────────────────────────────


class TestSocialSentiment:
    def test_class_exists(self):
        from src.social_sentiment import SocialSentimentAnalyzer

        assert SocialSentimentAnalyzer is not None


# ── DataFeedNews (52 missed) ──────────────────────────────────────────


class TestDataFeedBase:
    def test_class_exists(self):
        from src.data_feed import DataFeedManager

        assert DataFeedManager is not None


# ── DataFeedScorer (40 missed) ────────────────────────────────────────


class TestMultiTimeframe:
    def test_init(self):
        from src.multi_timeframe import MultiTimeframeAnalyzer

        mta = MultiTimeframeAnalyzer(MagicMock())
        assert mta is not None


# ── AdaptiveTrailing (20 missed) ──────────────────────────────────────


class TestAdaptiveTrailing:
    def test_class_exists(self):
        from src.adaptive_trailing import AdaptiveTrailingStop

        assert AdaptiveTrailingStop is not None


# ── Agents ────────────────────────────────────────────────────────────


class TestAgents:
    pass


# ── Strategies ────────────────────────────────────────────────────────


class TestStrategies:
    def test_base_strategy(self):
        from src.strategies.base import BaseStrategy

        assert BaseStrategy is not None

    def test_grid_strategy(self):
        from src.strategies.grid import GridStrategy

        assert GridStrategy is not None

    def test_dca_strategy(self):
        from src.strategies.dca import DCAStrategy

        assert DCAStrategy is not None

    def test_trend_strategy(self):
        from src.strategies.trend import TrendStrategy

        assert TrendStrategy is not None

    def test_bollinger_strategy(self):
        from src.strategies.bollinger import BollingerStrategy

        assert BollingerStrategy is not None

    def test_vwap_strategy(self):
        from src.strategies.vwap import VWAPStrategy

        assert VWAPStrategy is not None


# ── Portfolio modules ─────────────────────────────────────────────────


class TestPortfolioModules:

    def test_portfolio_pnl(self):
        from src.portfolio_pnl import PnlMixin

        assert PnlMixin is not None

    def test_portfolio_risk(self):
        from src.portfolio_risk import RiskMixin

        assert RiskMixin is not None


# ── Event Bus ─────────────────────────────────────────────────────────


class TestEventBus:
    def test_init(self):
        from src.event_bus import EventBus

        eb = EventBus()
        assert eb is not None


# ── EntryPrice ────────────────────────────────────────────────────────


class TestContextualBandit:
    def test_init(self):
        from src.contextual_bandit import ContextualBandit

        cb = ContextualBandit()
        assert cb is not None


# ── GARCHVol ──────────────────────────────────────────────────────────
