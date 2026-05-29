"""
Scan Orchestrator — Main pipeline: Scan → Score → Research → Risk → Execute.

5-phase pipeline:
  Phase 1: Sync portfolio, detect market regime, screen universe
  Phase 2: Score stocks via StockScorer, rank via CompositeRanker
  Phase 3: Research top N candidates (news + sentiment + fundamentals)
  Phase 4: Risk checks via StockRiskManager
  Phase 5: Execute trades (if AUTO_EXECUTE=true)
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ─── Data Models ────────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    """Output signal from the scan pipeline."""
    symbol: str
    side: str  # 'BUY' or 'SELL'
    quantity: float = 0.0
    price: float = 0.0
    score: float = 0.0
    currency: str = "USD"
    market: str = "US"
    sector: str = ""
    strategy: str = "momentum"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_approved: bool = True
    research_summary: str = ""
    risk_warnings: List[str] = field(default_factory=list)
    factor_scores: Dict[str, float] = field(default_factory=dict)
    position_size_usd: float = 0.0


@dataclass
class ScanResult:
    """Full scan pipeline result."""
    timestamp: str
    regime: str
    universe_size: int
    candidates_scored: int
    research_completed: int
    signals: List[TradeSignal]
    blocked: List[dict]
    duration_sec: float


# ─── Universe Loading ───────────────────────────────────────────────────────

def load_universe(name: str = "sp500", config_dir: str = None) -> List[str]:
    """Load a stock universe from universes.yaml."""
    config_dir = config_dir or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config"
    )
    path = os.path.join(config_dir, "universes.yaml")
    if not os.path.exists(path):
        logger.warning("universes.yaml not found, returning empty universe")
        return []

    with open(path) as f:
        data = yaml.safe_load(f)

    universe_cfg = data.get("universes", {}).get(name)
    if not universe_cfg:
        logger.warning("Universe '%s' not found in config", name)
        return []

    symbols = []
    sectors = universe_cfg.get("sectors", {})
    for sector, tickers in sectors.items():
        symbols.extend(tickers)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in symbols:
        if s not in seen:
            seen.add(s)
            unique.append(s)

    logger.info("Loaded universe '%s': %d symbols", name, len(unique))
    return unique


# ─── Scan Orchestrator ──────────────────────────────────────────────────────

class ScanOrchestrator:
    """
    Main pipeline orchestrator for the stock AI trader.

    Coordinates:
    - Market regime detection
    - Universe screening & factor scoring
    - Deep research on top candidates
    - Pre-trade risk checks
    - Trade execution
    """

    def __init__(
        self,
        broker=None,
        portfolio=None,
        stock_data_feed=None,
        stock_scorer=None,
        composite_ranker=None,
        risk_manager=None,
        regime_detector=None,
        stock_researcher=None,
        position_sizer=None,
        trade_executor=None,
        feature_store=None,
        config: dict = None,
    ):
        """
        Args:
            broker: BrokerProtocol instance for market data & orders.
            portfolio: PortfolioManager instance.
            stock_data_feed: StockDataFeed for OHLCV / quotes.
            stock_scorer: StockScorer for multi-factor scoring.
            composite_ranker: CompositeRanker for cross-sectional ranking.
            risk_manager: StockRiskManager for pre-trade risk checks.
            regime_detector: RegimeDetector for market regime.
            stock_researcher: StockResearcher for LLM-based deep research.
            position_sizer: HybridPositionSizer for position sizing.
            trade_executor: TradeExecutor for order placement.
            feature_store: FeatureStore for factor persistence.
            config: Override config dict.
        """
        self.broker = broker
        self.portfolio = portfolio
        self.data_feed = stock_data_feed
        self.scorer = stock_scorer
        self.ranker = composite_ranker
        self.risk_mgr = risk_manager
        self.regime_detector = regime_detector
        self.researcher = stock_researcher
        self.sizer = position_sizer
        self.executor = trade_executor
        self.feature_store = feature_store
        self.config = config or self._load_config()

    def _load_config(self) -> dict:
        """Load config from YAML."""
        config_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config"
        )
        config_path = os.path.join(config_dir, "config.yaml")
        if os.path.exists(config_path):
            with open(config_path) as f:
                return yaml.safe_load(f)
        return {}

    # ── Main Pipeline ───────────────────────────────────────────────────

    def run(
        self,
        universe_name: str = "sp500",
        auto_execute: bool = False,
        top_n_research: int = 5,
        min_score: float = 60.0,
    ) -> ScanResult:
        """
        Run the full scan pipeline.

        Args:
            universe_name: Universe to scan (default: sp500).
            auto_execute: If True, execute trades automatically.
            top_n_research: Number of top candidates to deep-research.
            min_score: Minimum composite score to pass.

        Returns:
            ScanResult with signals and metadata.
        """
        t_start = time.time()
        auto_execute = auto_execute or os.environ.get("AUTO_EXECUTE", "").lower() == "true"

        logger.info("=" * 60)
        logger.info("SCAN PIPELINE START — universe=%s, auto_execute=%s", universe_name, auto_execute)
        logger.info("=" * 60)

        # ── Phase 1: Sync + Regime + Universe ──────────────────────────
        logger.info("Phase 1: Sync portfolio, detect regime, load universe")
        regime = self._phase1_sync_and_regime()
        universe = load_universe(universe_name)
        if not universe:
            logger.warning("Empty universe, aborting")
            return ScanResult(
                timestamp=datetime.now().isoformat(), regime=regime,
                universe_size=0, candidates_scored=0, research_completed=0,
                signals=[], blocked=[], duration_sec=time.time() - t_start,
            )

        # ── Phase 2: Score & Rank ──────────────────────────────────────
        logger.info("Phase 2: Score %d stocks and rank", len(universe))
        scored_candidates, factor_scores = self._phase2_score_and_rank(universe)

        # ── Phase 3: Deep Research ─────────────────────────────────────
        logger.info("Phase 3: Research top %d candidates", top_n_research)
        top_candidates = scored_candidates[:top_n_research]
        research_results = self._phase3_research(top_candidates, factor_scores)

        # ── Phase 4: Risk Checks ───────────────────────────────────────
        logger.info("Phase 4: Risk checks on %d candidates", len(research_results))
        approved_signals, blocked = self._phase4_risk_check(
            research_results, factor_scores, min_score
        )

        # ── Phase 5: Execute ───────────────────────────────────────────
        if auto_execute and approved_signals:
            logger.info("Phase 5: Executing %d trades", len(approved_signals))
            self._phase5_execute(approved_signals)
        elif approved_signals:
            logger.info("Phase 5: %d signals ready (auto_execute=false)", len(approved_signals))

        duration = time.time() - t_start
        result = ScanResult(
            timestamp=datetime.now().isoformat(),
            regime=regime,
            universe_size=len(universe),
            candidates_scored=len(scored_candidates),
            research_completed=len(research_results),
            signals=approved_signals,
            blocked=blocked,
            duration_sec=duration,
        )

        logger.info("SCAN PIPELINE COMPLETE in %.1fs — %d signals, %d blocked",
                     duration, len(approved_signals), len(blocked))
        return result

    # ── Phase 1 ────────────────────────────────────────────────────────

    def _phase1_sync_and_regime(self) -> str:
        """Sync portfolio from broker and detect market regime."""
        regime = "NEUTRAL"

        # Sync portfolio
        if self.portfolio and self.broker:
            try:
                self.portfolio.sync_from_broker(self.broker)
            except Exception as e:
                logger.warning("Portfolio sync failed: %s", e)

        # Detect regime
        if self.regime_detector:
            try:
                # Fetch VIX
                vix = None
                if self.data_feed:
                    try:
                        vix_quote = self.data_feed.get_realtime_quote("^VIX")
                        vix = vix_quote.get("price")
                    except Exception:
                        pass

                regime = self.regime_detector.detect_regime(vix=vix)
                logger.info("Market regime: %s", regime)
            except Exception as e:
                logger.warning("Regime detection failed: %s", e)

        return regime

    # ── Phase 2 ────────────────────────────────────────────────────────

    def _phase2_score_and_rank(
        self, universe: List[str]
    ) -> tuple[List[str], Dict[str, Dict[str, float]]]:
        """
        Score all stocks in universe and rank them.

        Returns:
            (ranked_symbols, factor_scores): symbols sorted by composite score,
            and the per-symbol factor breakdown.
        """
        factor_scores: Dict[str, Dict[str, float]] = {}

        if not self.scorer:
            logger.warning("No StockScorer configured, returning empty")
            return [], factor_scores

        # Gather market data for scoring (batch via yfinance for speed)
        market_data: Dict[str, dict] = {}
        if self.data_feed:
            try:
                market_data = self.data_feed.get_multiple_quotes(universe)
                logger.info("Batch quotes fetched: %d/%d symbols", len(market_data), len(universe))
            except Exception as e:
                logger.warning("Batch quote failed, falling back to individual: %s", e)
                for sym in universe:
                    try:
                        quote = self.data_feed.get_realtime_quote(sym)
                        market_data[sym] = quote
                    except Exception as e:
                        logger.debug("Quote failed for %s: %s", sym, e)

        # Score each stock
        for sym in universe:
            try:
                score = self.scorer.score_stock(sym, market_data.get(sym, {}))
                factor_scores[sym] = {
                    "technical": score.technical,
                    "fundamental": score.fundamental,
                    "momentum": score.momentum,
                    "sentiment": score.sentiment,
                    "quality": score.quality,
                    "value": score.value,
                    "composite": score.composite,
                }
            except Exception as e:
                logger.debug("Scoring failed for %s: %s", sym, e)

        # Rank using CompositeRanker
        if self.ranker and factor_scores:
            ranked_df = self.ranker.rank_universe(list(factor_scores.keys()), factor_scores)
            ranked_symbols = ranked_df["symbol"].tolist()
        else:
            # Fallback: sort by composite score
            ranked_symbols = sorted(
                factor_scores.keys(),
                key=lambda s: factor_scores[s].get("composite", 0),
                reverse=True,
            )

        logger.info("Scored %d / %d stocks, top 5: %s",
                     len(factor_scores), len(universe), ranked_symbols[:5])
        return ranked_symbols, factor_scores

    # ── Phase 3 ────────────────────────────────────────────────────────

    def _phase3_research(
        self,
        candidates: List[str],
        factor_scores: Dict[str, Dict[str, float]],
    ) -> List[dict]:
        """
        Deep research on top candidates using LLM + news + sentiment + fundamentals.

        Returns:
            List of research dicts with score adjustments.
        """
        results = []

        if not self.researcher:
            # No researcher — pass through with no adjustment
            for sym in candidates:
                results.append({
                    "symbol": sym,
                    "score_adjustment": 0.0,
                    "confidence": "none",
                    "summary": "No researcher configured",
                    "news": [],
                    "sentiment": 0.0,
                })
            return results

        # Parallel research
        with ThreadPoolExecutor(max_workers=min(5, len(candidates))) as pool:
            futures = {
                pool.submit(self.researcher.analyze_stock, sym): sym
                for sym in candidates
            }
            try:
                for fut in as_completed(futures, timeout=120):
                    sym = futures[fut]
                    try:
                        report = fut.result()  # ResearchReport dataclass
                        # Convert recommendation to numeric score adjustment
                        _rec_map = {
                            "STRONG_BUY": 20, "BUY": 10, "HOLD": 0,
                            "SELL": -10, "STRONG_SELL": -20,
                        }
                        results.append({
                            "symbol": sym,
                            "score_adjustment": _rec_map.get(
                                report.recommendation.value if hasattr(report.recommendation, 'value') else str(report.recommendation),
                                0
                            ),
                            "confidence": report.confidence,
                            "summary": report.summary,
                            "news": report.catalysts,
                            "sentiment": report.sentiment_score,
                        })
                    except Exception as e:
                        logger.warning("Research failed for %s: %s", sym, e)
            except TimeoutError:
                logger.warning("Research timed out — %d / %d completed",
                               len(results), len(candidates))

        logger.info("Research completed for %d / %d candidates", len(results), len(candidates))
        return results

    # ── Phase 4 ────────────────────────────────────────────────────────

    def _phase4_risk_check(
        self,
        research_results: List[dict],
        factor_scores: Dict[str, Dict[str, float]],
        min_score: float,
    ) -> tuple[List[TradeSignal], List[dict]]:
        """
        Run pre-trade risk checks and generate final trade signals.

        Returns:
            (approved_signals, blocked_signals)
        """
        approved = []
        blocked = []

        for res in research_results:
            sym = res["symbol"]
            factors = factor_scores.get(sym, {})
            base_score = factors.get("composite", 50.0)
            adj_score = base_score + res.get("score_adjustment", 0.0)

            # Minimum score filter
            if adj_score < min_score:
                blocked.append({
                    "symbol": sym,
                    "reason": f"Score {adj_score:.1f} < min {min_score}",
                    "score": adj_score,
                })
                continue

            # Get current price
            price = 0.0
            if self.data_feed:
                try:
                    quote = self.data_feed.get_realtime_quote(sym)
                    price = quote.get("price", 0.0)
                except Exception:
                    pass

            if price <= 0:
                blocked.append({"symbol": sym, "reason": "No price data", "score": adj_score})
                continue

            # Risk check
            risk_approved = True
            risk_warnings = []
            position_multiplier = 1.0

            if self.risk_mgr:
                from src.risk.stock_risk_manager import TradeSignal as RiskSignal
                risk_signal = RiskSignal(
                    symbol=sym,
                    side="buy",
                    quantity=0,  # TBD by sizer
                    price=price,
                    market="US",
                    sector=factors.get("sector", ""),
                )
                try:
                    decision = self.risk_mgr.pre_trade_check(risk_signal)
                    risk_approved = decision.approved
                    risk_warnings = decision.warnings
                    position_multiplier = decision.position_multiplier
                    if not risk_approved:
                        blocked.append({
                            "symbol": sym,
                            "reason": decision.reason,
                            "score": adj_score,
                        })
                        continue
                except Exception as e:
                    logger.warning("Risk check failed for %s: %s", sym, e)

            # Position sizing
            position_size_usd = 0.0
            if self.sizer and self.portfolio:
                try:
                    nav = self.portfolio.get_nav()
                    portfolio_ctx = {
                        "total_value": nav,
                        "n_positions": self.portfolio.position_count,
                    }
                    size_frac = self.sizer.calculate(sym, portfolio_ctx)
                    position_size_usd = nav * size_frac * position_multiplier
                except Exception as e:
                    logger.warning("Position sizing failed for %s: %s", sym, e)

            # Calculate stop/take-profit
            stop_loss = price * 0.95  # 5% stop
            take_profit = price * 1.10  # 10% target

            signal = TradeSignal(
                symbol=sym,
                side="BUY",
                price=price,
                score=adj_score,
                sector=factors.get("sector", ""),
                stop_loss=stop_loss,
                take_profit=take_profit,
                risk_approved=risk_approved,
                research_summary=res.get("summary", ""),
                risk_warnings=risk_warnings,
                factor_scores=factors,
                position_size_usd=position_size_usd,
            )
            approved.append(signal)

        # Sort by score descending
        approved.sort(key=lambda s: s.score, reverse=True)

        logger.info("Risk check: %d approved, %d blocked", len(approved), len(blocked))
        return approved, blocked

    # ── Phase 5 ────────────────────────────────────────────────────────

    def _phase5_execute(self, signals: List[TradeSignal]):
        """Execute approved trade signals."""
        if not self.executor:
            logger.warning("No TradeExecutor configured — signals not executed")
            return

        for signal in signals:
            if signal.position_size_usd <= 0:
                logger.info("Skipping %s — position size is zero", signal.symbol)
                continue

            try:
                quantity = signal.position_size_usd / signal.price if signal.price > 0 else 0
                if quantity <= 0:
                    continue

                result = self.executor.execute(
                    symbol=signal.symbol,
                    side=signal.side,
                    quantity=quantity,
                    price=signal.price,
                    order_type="LMT",
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                )

                if result.get("success"):
                    logger.info("Executed: %s %s %.2f @ %.2f",
                                signal.side, signal.symbol, quantity, signal.price)
                else:
                    logger.warning("Execution failed for %s: %s",
                                   signal.symbol, result.get("error"))

            except Exception as e:
                logger.error("Execution error for %s: %s", signal.symbol, e)

    # ── Single Symbol Analysis ─────────────────────────────────────────

    def analyze_symbol(self, symbol: str) -> dict:
        """Deep analysis of a single stock (for `analyze` command)."""
        result = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "quote": {},
            "factor_scores": {},
            "research": {},
            "risk": {},
        }

        # Quote
        if self.data_feed:
            try:
                result["quote"] = self.data_feed.get_realtime_quote(symbol)
            except Exception as e:
                logger.warning("Quote failed for %s: %s", symbol, e)

        # Factor scores
        if self.scorer:
            try:
                score = self.scorer.score_stock(symbol, result["quote"])
                result["factor_scores"] = {
                    "composite": score.composite,
                    "technical": score.technical,
                    "fundamental": score.fundamental,
                    "momentum": score.momentum,
                    "sentiment": score.sentiment,
                    "quality": score.quality,
                    "value": score.value,
                    "weights": score.weights,
                }
            except Exception as e:
                logger.warning("Scoring failed for %s: %s", symbol, e)

        # Research
        if self.researcher:
            try:
                result["research"] = self.researcher.analyze_stock(symbol)
            except Exception as e:
                logger.warning("Research failed for %s: %s", symbol, e)

        return result
