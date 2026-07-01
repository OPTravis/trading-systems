"""
Market Scanner - Find trading opportunities across Binance SPOT
Phase 2: Multi-timeframe analysis, dynamic coin pool, weighted multi-factor scoring
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .exchange_client import ExchangeClient
from .data_feed import DataFeedManager
from .dynamic_coin_pool import DynamicCoinPool
from .indicators import Indicators
from .multi_timeframe import MultiTimeframeAnalyzer

logger = logging.getLogger(__name__)

# Specialist agents — wrap inline factor calculations into testable components
from src.agents.market_sentiment_agent import MarketSentimentAgent
from src.agents.onchain_agent import OnChainAgent
from src.agents.prepump_agent import PrePumpAgent
from src.agents.sentiment_agent import SentimentAgent
from src.agents.technical_agent import TechnicalAgent
from src.agents.trend_agent import TrendAgent
from src.agents.volume_agent import VolumeAgent
from src.strategy_guard import strategy_guard


class _RateLimiter:
    """Simple rate limiter: track timestamps, sleep if too fast."""

    def __init__(self, max_per_second: float = 25):
        self._max_per_second = max_per_second
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            # Keep only last 2 seconds of history
            self._timestamps = [t for t in self._timestamps if now - t < 2.0]
            if len(self._timestamps) >= int(self._max_per_second * 2):
                sleep_time = self._timestamps[0] + 2.0 - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.monotonic()
            self._timestamps.append(now)


class MarketScanner:
    """Scan Binance SPOT market for trading opportunities using multi-factor scoring."""

    def __init__(
        self,
        binance_client: "ExchangeClient",
        min_volume_24h: float = 5_000_000,
        min_volatility: float = 2.0,
        max_coins: int = 40,
    ):
        self.client = binance_client
        self.min_volume = min_volume_24h
        self.min_volatility = min_volatility
        self.max_coins = max_coins
        self.mtf_analyzer = MultiTimeframeAnalyzer(binance_client)
        self.coin_pool = DynamicCoinPool(binance_client)
        self.data_feed = DataFeedManager()
        self._rate_limiter = _RateLimiter(max_per_second=25)
        # Cached online learner weights (5-min TTL to avoid DB hit per coin)
        self._learner_weights: Optional[Dict[str, float]] = None
        self._learner_ts: float = 0

    @strategy_guard(max_failures=2, cooldown_sec=300, default_return=[])
    def scan_all(self) -> List[Dict]:
        """Full market scan — dynamic pool + multi-factor scoring, return top 20."""
        logger.info("Starting Phase 2 market scan...")

        # 1. Build dynamic coin pool
        pool = self.coin_pool.build_pool(
            min_volume_usd=self.min_volume, max_coins=self.max_coins
        )
        if not pool:
            logger.error("Dynamic coin pool returned empty")
            return []

        # 2. Sector-filter the pool — use real positions to enforce sector exposure limits
        from src.state_db import get_state_db

        try:
            db = get_state_db()
            db_positions = db.portfolio_get_all()
            _real_positions = [
                {
                    "symbol": sym,
                    "quantity": data["quantity"],
                    "entry_price": data["entry_price"],
                }
                for sym, data in db_positions.items()
            ]
        except Exception:
            logger.error(
                "Failed to load real positions from DB for pool filtering",
                exc_info=True,
            )
            _real_positions = []
        pool = self.coin_pool.get_sector_filtered_pool(pool, positions=_real_positions)

        logger.info(f"Scanning {len(pool)} candidate coins...")

        # 3. Analyze each coin in parallel (capped workers for rate limits)
        opportunities: List[Dict] = []
        max_workers = min(3, len(pool))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._analyze_coin, c): c for c in pool}

            for future in as_completed(futures):
                coin = futures[future]
                try:
                    result = future.result()
                    if result:
                        opportunities.append(result)
                except Exception as e:
                    logger.warning(f"Failed to analyze {coin['symbol']}: {e}")

        # 4. Cross-sectional Relative Value scoring (simplified stat-arb for spot)
        self._apply_relative_value_boost(opportunities)

        # 5. Sort by weighted score, return top 20
        opportunities.sort(key=lambda x: x["score"], reverse=True)

        # 4b. Optional LLM sentiment enrichment for top candidates (best-effort)
        try:
            self._llm_enrich_sentiment(opportunities[:5])
        except Exception:
            logger.debug("LLM sentiment enrichment skipped (non-critical)", exc_info=True)

        logger.info(f"Found {len(opportunities)} opportunities (top 20 returned)")
        return opportunities[:20]

    def get_top_movers(self, limit: int = 10) -> List[Dict]:
        """Get top gainers and losers.

        Returns:
            List of dicts, each with 'symbol', 'change_pct', 'quote_volume',
            'direction' ('gainer' or 'loser').
        """
        tickers = self.client.get_24hr_stats()

        # get_24hr_stats() with no symbol returns List[Dict] — guard against Dict
        if not tickers or not isinstance(tickers, list):
            return []

        usdt_pairs = [
            t
            for t in tickers
            if isinstance(t, dict) and t.get("symbol", "").endswith("USDT")
        ]

        # Sort by price change
        gainers = sorted(
            usdt_pairs, key=lambda x: float(x.get("price_change_pct", 0)), reverse=True
        )[:limit]
        losers = sorted(usdt_pairs, key=lambda x: float(x.get("price_change_pct", 0)))[
            :limit
        ]

        return [
            {
                "symbol": g.get("symbol", ""),
                "change_pct": float(g.get("price_change_pct", 0)),
                "quote_volume": float(g.get("quote_volume", 0)),
                "direction": "gainer",
            }
            for g in gainers
        ] + [
            {
                "symbol": l.get("symbol", ""),
                "change_pct": float(l.get("price_change_pct", 0)),
                "quote_volume": float(l.get("quote_volume", 0)),
                "direction": "loser",
            }
            for l in losers
        ]

    # ------------------------------------------------------------------
    # Internal analysis
    # ------------------------------------------------------------------

    def _analyze_coin(self, coin_data: Dict) -> Optional[Dict]:
        """Analyze a single coin with multi-timeframe + sentiment scoring."""
        symbol = coin_data["symbol"]

        # 1. Multi-timeframe analysis
        try:
            mtf_result = self.mtf_analyzer.analyze(symbol)
        except Exception as e:
            logger.debug(f"MTF analysis failed for {symbol}: {e}")
            return None

        # 2. Volume surge detection on 1h klines (BEFORE scoring so it feeds into _factor_volume_momentum)
        volume_surge = False
        new_signals_data = {}  # OBV, BB squeeze, RSI divergence, consolidation
        try:
            self._rate_limiter.wait()
            klines_1h = self.client.get_klines(symbol, "1h", limit=50)
            if len(klines_1h) >= 21:
                recent_volumes = [k["volume"] for k in klines_1h[-21:-1]]
                current_volume = klines_1h[-1]["volume"]
                avg_volume = (
                    sum(recent_volumes) / len(recent_volumes) if recent_volumes else 1
                )
                # Scale by actual elapsed time in the candle (cap at 2x)
                import time as _time_mod

                candle_open_time = klines_1h[-1].get("open_time", 0)
                elapsed_min = (
                    ((_time_mod.time() * 1000) - candle_open_time) / 60000
                    if candle_open_time
                    else 30
                )
                scale_factor = min(60.0 / max(elapsed_min, 1.0), 2.0)
                estimated_full_candle = current_volume * scale_factor
                if avg_volume > 0 and estimated_full_candle > avg_volume * 1.5:
                    volume_surge = True

            # NEW: Compute pre-pump indicators from 1h klines
            if len(klines_1h) >= 35:
                new_signals_data["obv_div"] = Indicators.obv_divergence(
                    klines_1h, lookback=20
                )
                new_signals_data["bb_squeeze"] = Indicators.bb_squeeze(klines_1h)
                new_signals_data["rsi_div"] = Indicators.rsi_divergence(klines_1h)

            # NEW: Consolidation breakout from 4h klines (need more history)
            try:
                self._rate_limiter.wait()
                klines_4h = self.client.get_klines(symbol, "4h", limit=80)
                if len(klines_4h) >= 35:
                    new_signals_data["consolidation"] = (
                        Indicators.consolidation_breakout(klines_4h)
                    )
            except Exception:
                logger.error(
                    "Consolidation breakout analysis failed for %s",
                    symbol,
                    exc_info=True,
                )
        except Exception:
            logger.error("4h klines fetch failed for %s", symbol, exc_info=True)

        # Inject volume_surge into coin_data so _factor_volume_momentum can use it
        coin_data["volume_surge"] = volume_surge

        # 3. Sentiment data (graceful degradation) — per-symbol funding/OI
        sentiment_data = None
        try:
            sentiment_data = self.data_feed.scorer.get_symbol_sentiment(symbol)
        except Exception:
            logger.error("Symbol sentiment fetch failed for %s", symbol, exc_info=True)
            sentiment_data = None

        # 3b. Global market sentiment (Fear & Greed) — market-wide emotion
        fng_value = 50
        try:
            fng = self.data_feed.fng.get_current()
            if fng:
                fng_value = int(fng.get("value", 50))
        except Exception:
            logger.error(
                "Fear & Greed Index fetch failed, defaulting to 50", exc_info=True
            )
            fng_value = 50

        # 3c. On-chain score (DeFiLlama TVL changes)
        onchain_score = 50.0
        try:
            onchain_score = self.data_feed.onchain.get_onchain_score()
        except Exception:
            logger.error(
                "On-chain score fetch failed, defaulting to 50.0", exc_info=True
            )
            onchain_score = 50.0

        # 4. Call specialist agents to compute factor scores
        agent_scores = {}

        # TechnicalAgent — factors 1 (technical), 5 (price action), 8 (BB squeeze), 9 (RSI div)
        try:
            tf_1h_raw = mtf_result.get("tf_1h", {})
            tech_result = TechnicalAgent().analyze(
                tf_1h=tf_1h_raw,
                bb_squeeze_data=(
                    new_signals_data.get("bb_squeeze") if new_signals_data else None
                ),
                rsi_div_data=(
                    new_signals_data.get("rsi_div") if new_signals_data else None
                ),
            )
            agent_scores["technical"] = tech_result
        except Exception as e:
            logger.debug(f"TechnicalAgent failed for {symbol}: {e}")

        # TrendAgent — factor 2 (multi-TF trend alignment)
        try:
            trend_result = TrendAgent().analyze(mtf_result)
            agent_scores["trend"] = trend_result
        except Exception as e:
            logger.debug(f"TrendAgent failed for {symbol}: {e}")

        # VolumeAgent — factor 3 (volume rank + surge)
        try:
            vol_result = VolumeAgent().analyze(
                coin_data=coin_data, volume_surge=volume_surge
            )
            agent_scores["volume"] = vol_result
        except Exception as e:
            logger.debug(f"VolumeAgent failed for {symbol}: {e}")

        # SentimentAgent — factor 4 (funding rate + OI)
        try:
            sent_result = SentimentAgent().analyze(funding_data=sentiment_data)
            agent_scores["sentiment"] = sent_result
        except Exception as e:
            logger.debug(f"SentimentAgent failed for {symbol}: {e}")

        # PrePumpAgent — factors 6 (OBV divergence) + 7 (consolidation breakout)
        try:
            ns = new_signals_data or {}
            prepump_result = PrePumpAgent().analyze(
                obv_div_data=ns.get("obv_div"),
                consolidation_data=ns.get("consolidation"),
            )
            agent_scores["prepump"] = prepump_result
        except Exception as e:
            logger.debug(f"PrePumpAgent failed for {symbol}: {e}")

        # OnChainAgent — factor 10 (DeFiLlama TVL)
        try:
            onchain_result = OnChainAgent().analyze(onchain_score=onchain_score)
            agent_scores["onchain"] = onchain_result
        except Exception as e:
            logger.debug(f"OnChainAgent failed for {symbol}: {e}")

        # MarketSentimentAgent — factor 11 (Fear & Greed contrarian)
        try:
            fng_result = MarketSentimentAgent().analyze(fng_value=fng_value)
            agent_scores["market_sentiment"] = fng_result
        except Exception as e:
            logger.debug(f"MarketSentimentAgent failed for {symbol}: {e}")

        # 5. Weighted multi-factor score (agent-based with inline fallback)
        market_sentiment_score = (
            fng_result.score if "market_sentiment" in agent_scores else 50.0
        )

        # 5b. Order book depth score (Phase 4)
        orderbook_score = 50.0
        try:
            from src.orderbook_analyzer import OrderBookAnalyzer

            ob = OrderBookAnalyzer(binance_client=self.client)
            ob_result = ob.analyze(symbol, limit=10)
            if ob_result:
                orderbook_score = ob_result["score"]
        except Exception as e:
            logger.debug(f"OrderBook analysis failed for {symbol}: {e}")

        score, factor_scores = self._calculate_weighted_score(
            mtf_result,
            sentiment_data,
            coin_data,
            new_signals_data,
            agent_scores=agent_scores,
            market_sentiment_score=market_sentiment_score,
            onchain_score=onchain_score,
            orderbook_score=orderbook_score,
        )

        # 5c. LightGBM Price Direction Predictor (Phase 9 — replaces Transformer+GRU)
        predicted_direction = None
        prediction_confidence = None
        tf_1h: Dict = mtf_result.get("tf_1h", {})
        try:
            from src.price_predictor import get_predictor

            predictor = get_predictor()
            if predictor.is_ready():
                features = {
                    "rsi": tf_1h.get("rsi", 50),
                    "macd_histogram": tf_1h.get("macd_histogram", 0),
                    "bb_position": tf_1h.get("bb_position", 0.5),
                    "volume_ratio": tf_1h.get("volume_ratio", 1.0),
                    "obv_divergence": 0,  # computed elsewhere
                    "consolidation_score": tf_1h.get("consolidation_score", 50),
                    "bb_squeeze": 1 if tf_1h.get("bb_squeeze", False) else 0,
                    "rsi_divergence": 0,
                    "orderbook_imbalance": (orderbook_score - 50) / 50,
                    "sentiment_score": (market_sentiment_score - 50) / 50,
                    "trend_score": mtf_result.get("trend_score", 50) / 100,
                    "price_action_score": tf_1h.get("price_action_score", 50) / 100,
                    "hmm_regime": 0,
                    "fear_greed": fng_value or 50,
                    "btc_trend": 1,
                    "volatility_24h": coin_data.get("price_change_24h", 0),
                    "volume_surge": 1 if volume_surge else 0,
                }
                pred = predictor.predict(features)
                predicted_direction = pred["direction"]
                prediction_confidence = pred["confidence"]
                # Bonus: if predictor agrees with score direction, boost by up to 5
                if predicted_direction == "up" and score >= 50:
                    bonus = min(5.0, pred["prob_up"] * 5)
                    score = min(100.0, score + bonus)
        except Exception as e:
            logger.debug(f"Price predictor unavailable for {symbol}: {e}")

        # Gate: score >= 50 to be included
        # (No separate entry_signal gate — the multi-factor score already
        # incorporates 12 weighted signals; downstream strategy_adaptor
        # applies regime-aware thresholds for final filtering.)
        if score < 50:
            return None

        # Extract sub-scores for transparency
        tf_4h = mtf_result.get("tf_4h", {})
        tf_15m = mtf_result.get("tf_15m", {})
        entry_signal = mtf_result.get("entry_signal", None)

        # Compute technical_score for downstream consumers (e.g. scan_orchestrator)
        if "technical" in agent_scores and hasattr(agent_scores["technical"], "data"):
            technical_score = agent_scores["technical"].data.get(
                "f_technical", self._factor_technical(tf_1h, tf_4h)
            )
        else:
            technical_score = self._factor_technical(tf_1h, tf_4h)

        return {
            "symbol": symbol,
            "score": round(score, 2),
            "price": coin_data.get("price", tf_1h.get("current_price", 0)),
            "volume_24h": coin_data.get("volume_24h", 0),
            "price_change_24h": coin_data.get("price_change_24h", 0),
            "rank": coin_data.get("rank", 0),
            "volume_surge": volume_surge,
            "entry_signal": entry_signal,
            "trend_alignment": mtf_result.get("trend_alignment", ""),
            "trend_score": mtf_result.get("trend_score", 0),
            "trend_strength": mtf_result.get(
                "trend_score", 0
            ),  # alias for backward compat
            "atr_15m": mtf_result.get("atr_15m", 0),
            "technical_score": round(technical_score, 1),
            "sentiment_score": (
                sentiment_data.get("sentiment_score") if sentiment_data else None
            ),
            "funding_rate": (
                sentiment_data.get("funding_rate") if sentiment_data else None
            ),
            "oi_change_pct": (
                sentiment_data.get("oi_change_pct") if sentiment_data else None
            ),
            "market_sentiment_score": round(market_sentiment_score, 1),
            "onchain_score": round(onchain_score, 1),
            "predicted_direction": predicted_direction,
            "prediction_confidence": (
                round(prediction_confidence, 3) if prediction_confidence else None
            ),
            "analysis": {
                "1h": tf_1h,
                "4h": tf_4h,
                "15m": tf_15m,
            },
            "signals": self._generate_signals(
                tf_1h, tf_4h, mtf_result, sentiment_data, volume_surge, new_signals_data
            ),
            "factor_scores": factor_scores,
        }

    # ------------------------------------------------------------------
    # Weighted multi-factor scoring
    # ------------------------------------------------------------------

    def _calculate_weighted_score(
        self,
        mtf_result: Dict,
        sentiment_data: Optional[Dict],
        coin_data: Dict,
        new_signals_data: Optional[Dict] = None,
        agent_scores: Optional[Dict] = None,
        market_sentiment_score: float = 50.0,
        onchain_score: float = 50.0,
        orderbook_score: float = 50.0,
    ) -> tuple:
        """Weighted multi-factor scoring (0-100) using specialist agents.

        Returns (score, factor_scores_dict).

        When ``agent_scores`` is provided, individual factor scores are
        extracted from the agent results.  Missing agents fall back to
        the original inline factor calculations.

        Factor weights (sum = 100):
        - Technical (1h):         15%  — RSI, MACD, BB, VWAP from 1h analysis
        - Multi-TF Trend:         15%  — 4h/1h/15m alignment score
        - Volume/Momentum:        10%  — 24h volume rank + volume surge
        - Funding/OI:              8%  — contrarian funding + OI flow
        - Price Action:            8%  — volatility, momentum
        - OBV Divergence:          8%  — smart money accumulation detection
        - Consolidation Breakout:  8%  — long-range breakout with volume
        - BB Squeeze:              4%  — volatility compression before explosion
        - RSI Divergence:          4%  — momentum bottom detection
        - On-Chain (DeFiLlama):   10%  — TVL change across major chains
        - Market Sentiment (F&G):  5%  — contrarian fear/greed index
        - Order Book Depth:        5%  — buy/sell pressure + whale detection

        Weights are read from DB (learned by OnlineLearner) or fall back to
        the hardcoded defaults above.
        """

        tf_1h = mtf_result.get("tf_1h", {})
        tf_4h = mtf_result.get("tf_4h", {})
        current_price = tf_1h.get("current_price", 0)
        ag = agent_scores or {}

        # --- Extract individual factor scores from agents or fall back ------
        # Factor 1: Technical (15%) — from TechnicalAgent
        tech_data = ag.get("technical", {})
        if hasattr(tech_data, "data"):
            tech_data = tech_data.data
        f1 = tech_data.get("f_technical", self._factor_technical(tf_1h, tf_4h))

        # Factor 2: Trend (15%) — from TrendAgent
        trend_result = ag.get("trend")
        if trend_result and hasattr(trend_result, "score"):
            f2 = trend_result.score
        else:
            f2 = self._factor_trend_alignment(mtf_result)

        # Factor 3: Volume (10%) — from VolumeAgent
        vol_result = ag.get("volume")
        if vol_result and hasattr(vol_result, "score"):
            f3 = vol_result.score
        else:
            f3 = self._factor_volume_momentum(coin_data)

        # Factor 4: Sentiment (8%) — from SentimentAgent
        sent_result = ag.get("sentiment")
        if sent_result and hasattr(sent_result, "score"):
            f4 = sent_result.score
        else:
            f4 = self._factor_sentiment(sentiment_data)

        # Factor 5: Price Action (8%) — from TechnicalAgent
        f5 = tech_data.get("f_price_action", self._factor_price_action(tf_1h))

        # f_ma: Daily MA support (±15 adjustment) — no agent, always inline
        f_ma = self._factor_daily_ma_support(tf_4h, current_price)

        # Factor 6: OBV Divergence (8%) — from PrePumpAgent
        pp_data = ag.get("prepump", {})
        if hasattr(pp_data, "data"):
            pp_data = pp_data.data
        f7 = pp_data.get(
            "f_obv_divergence",
            self._factor_obv_divergence((new_signals_data or {}).get("obv_div")),
        )

        # Factor 7: Consolidation Breakout (8%) — from PrePumpAgent
        f8 = pp_data.get(
            "f_consolidation",
            self._factor_consolidation((new_signals_data or {}).get("consolidation")),
        )

        # Factor 8: BB Squeeze (4%) — from TechnicalAgent
        f9 = tech_data.get(
            "f_bb_squeeze",
            self._factor_bb_squeeze((new_signals_data or {}).get("bb_squeeze")),
        )

        # Factor 9: RSI Divergence (4%) — from TechnicalAgent
        f10 = tech_data.get(
            "f_rsi_divergence",
            self._factor_rsi_divergence((new_signals_data or {}).get("rsi_div")),
        )

        # Read learned weights from DB (OnlineLearner), fall back to defaults
        # Cache for 5 minutes to avoid creating OnlineLearner per coin
        _now = time.time()
        if self._learner_weights is not None and (_now - self._learner_ts) < 300:
            _w = self._learner_weights
        else:
            try:
                from src.online_learner import OnlineLearner

                _learner = OnlineLearner()
                _w = _learner.get_current_weights()
                self._learner_weights = _w
                self._learner_ts = _now
            except Exception:
                logger.error(
                    "Failed to load learned weights from OnlineLearner, falling back to defaults",
                    exc_info=True,
                )
                _w = {
                    "technical": 16.0,
                    "trend": 16.0,
                    "volume": 11.0,
                    "sentiment": 9.0,
                    "price_action": 8.0,
                    "obv_divergence": 8.0,
                    "consolidation": 8.0,
                    "bb_squeeze": 4.0,
                    "rsi_divergence": 4.0,
                    "onchain": 8.0,
                    "market_sentiment": 5.0,
                    "orderbook": 3.0,
                }
                self._learner_weights = _w
                self._learner_ts = _now

        score = (
            (_w["technical"] / 100) * f1
            + (_w["trend"] / 100) * f2
            + (_w["volume"] / 100) * f3
            + (_w["sentiment"] / 100) * f4
            + (_w["price_action"] / 100) * f5
            + (_w["obv_divergence"] / 100) * f7
            + (_w["consolidation"] / 100) * f8
            + (_w["bb_squeeze"] / 100) * f9
            + (_w["rsi_divergence"] / 100) * f10
            + (_w["onchain"] / 100) * onchain_score
            + (_w["market_sentiment"] / 100) * market_sentiment_score
            + (_w.get("orderbook", 3.0) / 100) * orderbook_score
        ) + f_ma

        # Store individual factor scores for trade_outcome_recorder
        factor_scores = {
            "technical": round(f1, 1),
            "trend": round(f2, 1),
            "volume": round(f3, 1),
            "sentiment": round(f4, 1),
            "price_action": round(f5, 1),
            "obv_divergence": round(f7, 1),
            "consolidation": round(f8, 1),
            "bb_squeeze": round(f9, 1),
            "rsi_divergence": round(f10, 1),
            "onchain": round(onchain_score, 1),
            "market_sentiment": round(market_sentiment_score, 1),
            "orderbook": round(orderbook_score, 1),
        }

        return max(0.0, min(100.0, score)), factor_scores

    # -- Factor 1: Technical (1h) — 30% weight -----------------------

    @staticmethod
    def _factor_technical(tf_1h: Dict, tf_4h: Optional[Dict] = None) -> float:
        """RSI + MACD + BB + VWAP + MA alignment from 1h analysis.

        Sub-score breakdown (max 100):
            RSI:    25 pts  — momentum positioning
            MACD:   25 pts  — trend confirmation
            BB:     20 pts  — mean-reversion opportunity
            VWAP:   15 pts  — intraday value
            MA:     15 pts  — structural alignment
            4H RSI: ±5 pts  — higher timeframe confirmation
        """
        score = 40.0  # Neutral baseline centered at 40

        # RSI scoring (adjustments from baseline)
        rsi = tf_1h.get("rsi", 50)
        if rsi < 20:
            score += 20
        elif rsi < 30:
            score += 18
        elif rsi < 40:
            score += 10
        elif rsi < 50:
            score += 5
        elif 50 <= rsi <= 60:
            score += 3
        elif 60 <= rsi < 70:
            score += 5
        elif rsi > 80:
            score -= 15
        elif rsi > 70:
            score -= 5

        # MACD histogram
        macd_hist = tf_1h.get("macd_histogram", 0)
        if macd_hist > 0:
            score += 25
        elif macd_hist < 0:
            score -= 10

        # BB below lower (adjustment from baseline)
        current_price = tf_1h.get("current_price", 0)
        bb_lower = tf_1h.get("bb_lower", 0)
        if current_price and bb_lower and current_price < bb_lower:
            score += 10

        # Price above VWAP (adjustment from baseline)
        vwap = tf_1h.get("vwap", 0)
        if current_price and vwap and current_price > vwap:
            score += 10

        # MA alignment bullish (MA7 > MA25 > MA99) (adjustment from baseline)
        ma7 = tf_1h.get("ma7", 0)
        ma25 = tf_1h.get("ma25", 0)
        ma99 = tf_1h.get("ma99", 0)
        if ma7 > ma25 > ma99:
            score += 10

        # 4H RSI confirmation — higher timeframe filter
        # Research basis: 4H is the only consistently profitable timeframe
        # When 4H RSI confirms oversold, 1H signals are more reliable
        if tf_4h:
            rsi_4h = tf_4h.get("rsi", 50)
            if rsi_4h < 35:
                score += 5  # 4H confirms oversold → higher confidence entry
            elif rsi_4h > 65:
                score -= 5  # 4H overbought → 1H oversold likely just a dip

        return max(0.0, min(100.0, score))

    @staticmethod
    def _factor_daily_ma_support(analysis_4h: Dict, current_price: float) -> float:
        """Daily MA support/resistance confirmation.

        Uses 4h data (100 bars ≈ 16 days) to approximate daily MAs:
        - Price above 4h MA99 (~daily MA50) → bullish structure +10%
        - Price above 4h MA25 (~daily MA20) → short-term bullish +5%
        - Price below both → structural weakness -10%
        """
        if not analysis_4h or not current_price:
            return 0.0

        ma99 = analysis_4h.get("ma99", 0)
        ma25 = analysis_4h.get("ma25", 0)

        adjustment = 0.0
        if ma99 > 0 and current_price > ma99:
            adjustment += 10.0  # Strong structural support
        elif ma99 > 0 and current_price < ma99:
            adjustment -= 10.0  # Below major support → risky

        if ma25 > 0 and current_price > ma25:
            adjustment += 5.0  # Short-term support
        elif ma25 > 0 and current_price < ma25:
            adjustment -= 5.0

        return adjustment

    # -- Factor 2: Multi-TF Trend — 25% weight ------------------------

    @staticmethod
    def _factor_trend_alignment(mtf_result: Dict) -> float:
        """Direct trend score from MultiTimeframeAnalyzer (already 0-100)."""
        return max(0.0, min(100.0, float(mtf_result.get("trend_score", 0))))

    # -- Factor 3: Volume/Momentum — 15% weight ----------------------

    @staticmethod
    def _factor_volume_momentum(coin_data: Dict) -> float:
        """24h volume rank + price change scoring."""
        score = 40.0  # Neutral baseline centered at 40

        # Volume rank
        rank = coin_data.get("rank", 999)
        if rank <= 10:
            score += 20
        elif rank <= 20:
            score += 15
        elif rank <= 30:
            score += 5
        else:
            score += 0

        # Price change 24h
        price_change = coin_data.get("price_change_24h", 0)
        if 0 < price_change <= 5:
            score += 15
        elif 5 < price_change <= 15:
            score += 20
        elif price_change > 15:
            score += 10
        elif -5 <= price_change <= 0:
            score += 5
        elif price_change < -5:
            score -= 5

        # Volume surge (+20 if detected — set externally if available)
        if coin_data.get("volume_surge", False):
            score += 20

        return max(0.0, min(100.0, score))

    # -- Factor 4: Sentiment — 15% weight ----------------------------

    @staticmethod
    def _factor_sentiment(sentiment_data: Optional[Dict]) -> float:
        """Map sentiment_score (-15 to +15) to 0-100 range. Neutral = 50 if no data."""
        if sentiment_data is None:
            return 50.0

        sentiment_score = sentiment_data.get("sentiment_score", 0)
        # Map -15..+15 to ~0..100  (50 + score * 3.33)
        mapped = 50.0 + sentiment_score * 3.33
        return max(0.0, min(100.0, mapped))

    # -- Factor 5: Price Action — 10% weight -------------------------

    @staticmethod
    def _factor_price_action(tf_1h: Dict) -> float:
        """Volatility + momentum scoring from 1h analysis."""
        score = 40.0  # Neutral baseline centered at 40

        # Volatility scoring
        vol = tf_1h.get("volatility_pct", 0)
        if 2 <= vol <= 8:
            score += 30
        elif 8 < vol <= 15:
            score += 15
        elif vol > 15:
            score -= 5
        else:
            score += 5  # <2%

        # Momentum positive
        momentum = tf_1h.get("momentum", 0)
        if momentum > 0:
            score += 15

        return max(0.0, min(100.0, score))

    # -- Factor 6: Sector Priority — 5% weight -----------------------

    @staticmethod
    def _factor_sector_priority(coin_data: Dict) -> float:
        """Sector crowding: high=100, medium=60, low=20."""
        priority = coin_data.get("sector_priority", "high")
        mapping = {"high": 100.0, "medium": 60.0, "low": 20.0}
        return mapping.get(priority, 50.0)

    # -- NEW: Factor 7: OBV Divergence — 10% weight --------------------
    @staticmethod
    def _factor_obv_divergence(obv_data: Optional[Dict]) -> float:
        """OBV divergence: price down but OBV up = smart money accumulation.

        Scoring: detected=True → 60-100 (based on strength), rising OBV trend → +20.
        """
        if not obv_data:
            return 30.0  # neutral/no data

        if obv_data.get("detected"):
            strength = obv_data.get("strength", 0)
            base = 60 + min(40, strength * 0.4)
        else:
            base = 30.0

        # Rising OBV trend bonus
        if obv_data.get("obv_trend") == "rising":
            base = min(100, base + 20)

        return max(0.0, min(100.0, base))

    # -- NEW: Factor 8: Consolidation Breakout — 10% weight -------------
    @staticmethod
    def _factor_consolidation(consol_data: Optional[Dict]) -> float:
        """Long-term consolidation breakout: 30+ day range with volume confirmation.

        Breaking out with volume = 90-100, in consolidation but not yet = 50-70.
        """
        if not consol_data:
            return 30.0

        if consol_data.get("breaking_out"):
            base = 80.0
            if consol_data.get("volume_confirmed"):
                base = 95.0  # full breakout with volume
        elif consol_data.get("in_consolidation"):
            days = consol_data.get("days_in_range", 0)
            range_pct = consol_data.get("range_pct", 25)
            # Tighter and longer consolidation = higher score
            base = 50.0
            if days >= 40:
                base += 15
            elif days >= 30:
                base += 10
            if range_pct <= 15:
                base += 10  # very tight range
        else:
            base = 30.0

        return max(0.0, min(100.0, base))

    # -- NEW: Factor 9: BB Squeeze — 5% weight ------------------------
    @staticmethod
    def _factor_bb_squeeze(squeeze_data: Optional[Dict]) -> float:
        """Bollinger Band squeeze: volatility compressed → explosion imminent.

        Squeezing (bottom 20th percentile) = 80-100, mild compression = 40-60.
        """
        if not squeeze_data:
            return 30.0

        if squeeze_data.get("squeezing"):
            # Lower percentile = tighter squeeze = stronger signal
            pctile = squeeze_data.get("percentile", 50)
            base = 90 - pctile  # bottom 5th percentile → 85, 20th → 70
        else:
            percentile = squeeze_data.get("percentile", 50)
            if percentile <= 35:
                base = 50.0  # mild compression
            else:
                base = 30.0  # normal/expanding

        return max(0.0, min(100.0, base))

    # -- NEW: Factor 10: RSI Divergence — 5% weight --------------------
    @staticmethod
    def _factor_rsi_divergence(rsi_div_data: Optional[Dict]) -> float:
        """RSI bullish divergence: price lower low but RSI higher low.

        Detected = 70-100, not detected with oversold RSI = 40-50.
        """
        if not rsi_div_data:
            return 30.0

        if rsi_div_data.get("detected"):
            strength = rsi_div_data.get("strength", 0)
            base = 70 + min(30, strength * 0.3)
        else:
            # Not detected but oversold RSI is still mildly bullish
            rsi = rsi_div_data.get("rsi_current", 50)
            if rsi < 30:
                base = 50.0
            elif rsi < 40:
                base = 40.0
            else:
                base = 30.0

        return max(0.0, min(100.0, base))

    # ------------------------------------------------------------------
    # Cross-sectional Relative Value (simplified stat-arb for spot)
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_relative_value_boost(opportunities: List[Dict]) -> None:
        """Boost score for coins whose RSI is significantly below market median.

        Simplified statistical arbitrage adapted for spot-only trading:
        instead of simultaneously longing the undervalued asset and shorting
        the overvalued one (true pairs trading), we only buy the undervalued
        coin when its RSI diverges from the cross-sectional median.

        Logic:
        - Compute median 1H RSI across all scanned coins
        - For each coin, compute RSI gap = (coin_rsi - median_rsi)
        - Negative gap (coin more oversold than peers) → score boost
        - Positive gap (coin less oversold) → no penalty (let other factors decide)
        - Max boost: +5 points (proportional to gap, capped)
        """
        if len(opportunities) < 5:
            return  # Need enough coins for meaningful cross-sectional comparison

        # Collect RSI values
        rsi_values = []
        for opp in opportunities:
            rsi = opp.get("analysis", {}).get("1h", {}).get("rsi")
            if rsi is not None and 0 < rsi < 100:
                rsi_values.append(rsi)

        if len(rsi_values) < 5:
            return

        # Compute median RSI
        rsi_values.sort()
        mid = len(rsi_values) // 2
        median_rsi = rsi_values[mid]

        # Apply relative value boost
        for opp in opportunities:
            rsi = opp.get("analysis", {}).get("1h", {}).get("rsi")
            if rsi is None or rsi <= 0:
                continue

            gap = rsi - median_rsi  # negative = more oversold than peers

            if gap <= -15:
                # Significantly more oversold → strong relative value signal
                opp["score"] = round(opp["score"] + 5, 2)
                opp.setdefault("signals", []).append(
                    f"📊 Relative Value: RSI {rsi:.0f} vs median {median_rsi:.0f} (+5)"
                )
            elif gap <= -8:
                # Moderately more oversold → mild boost
                opp["score"] = round(opp["score"] + 2, 2)
                opp.setdefault("signals", []).append(
                    f"📊 Relative Value: RSI {rsi:.0f} vs median {median_rsi:.0f} (+2)"
                )

    # ------------------------------------------------------------------
    # LLM Sentiment Enrichment (optional, best-effort)
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_enrich_sentiment(top_opportunities: List[Dict]) -> None:
        """Enrich top candidates with LLM-based sentiment analysis.

        Best-effort: silently skips on any failure. Adds an 'llm_sentiment'
        field and appends a signal to the signal list.
        """
        if not top_opportunities:
            return

        try:
            from src.llm_client import LLMClient
            client = LLMClient()
        except Exception as e:
            logger.warning("market_scanner._llm_enrich_sentiment: " + str(e))
            return

        for opp in top_opportunities:
            symbol = opp.get("symbol", "")
            price = opp.get("price", 0)
            score = opp.get("score", 0)
            funding = opp.get("funding_rate")
            oi_change = opp.get("oi_change_pct")
            signals = opp.get("signals", [])

            context = (
                f"Symbol: {symbol}, Price: {price}, Score: {score}/100, "
                f"Funding rate: {funding}, OI 24h change: {oi_change}%, "
                f"Current signals: {', '.join(signals[:5])}"
            )

            prompt = (
                "You are a crypto trading analyst. Based on this data, give a "
                "1-sentence sentiment assessment (bullish/bearish/neutral) with "
                "a brief reason. Max 20 words. Start with BULLISH/BEARISH/NEUTRAL.\n\n"
                f"{context}"
            )

            try:
                resp = client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    max_tokens=60,
                )
                if resp and resp.get("content"):
                    assessment = resp["content"].strip()[:100]
                    opp["llm_sentiment"] = assessment
                    opp.setdefault("signals", []).append(f"🤖 LLM: {assessment}")
            except Exception:
                logger.debug("LLM sentiment failed for %s", symbol, exc_info=True)

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _generate_signals(
        self,
        a_1h: Dict,
        a_4h: Dict,
        mtf_result: Dict,
        sentiment_data: Optional[Dict],
        volume_surge: bool = False,
        new_signals_data: Optional[Dict] = None,
    ) -> List[str]:
        """Generate human-readable signals including multi-TF, sentiment, and new pre-pump indicators."""
        signals = []

        # --- Multi-timeframe trend signals ---
        trend_alignment = mtf_result.get("trend_alignment", "")
        entry_signal = mtf_result.get("entry_signal", None)

        if entry_signal:
            signals.append(f"✅ Multi-TF Entry Signal ({entry_signal})")
        if trend_alignment == "bullish":
            signals.append("🚀 Multi-TF Bullish Alignment")
        elif trend_alignment == "bearish":
            signals.append("📉 Multi-TF Bearish Alignment")

        # Trend score label
        trend_score = mtf_result.get("trend_score", 0)
        if trend_score >= 75:
            signals.append(f"💪 Strong Trend Score: {trend_score:.0f}")
        elif trend_score >= 50:
            signals.append(f"📈 Moderate Trend Score: {trend_score:.0f}")

        # --- 1h Trend signals ---
        trend = a_1h.get("trend", "")
        if trend == "strong_up":
            signals.append("🚀 1h Strong Uptrend")
        elif trend == "strong_down":
            signals.append("📉 1h Strong Downtrend")

        # --- RSI signals ---
        rsi = a_1h.get("rsi", 50)
        if rsi < 30:
            signals.append(f"💎 RSI Oversold ({rsi:.1f})")
        elif rsi > 70:
            signals.append(f"🔥 RSI Overbought ({rsi:.1f})")

        # --- 4H RSI confirmation ---
        rsi_4h = a_4h.get("rsi", 50)
        if rsi_4h < 35:
            signals.append(f"✅ 4H RSI Confirmed Oversold ({rsi_4h:.1f})")
        elif rsi_4h > 65 and rsi < 35:
            signals.append(f"⚠️ 4H RSI Diverges ({rsi_4h:.1f}) — 1H dip, 4H not oversold")

        # --- MACD signals (multi-timeframe) ---
        macd_1h = a_1h.get("macd_histogram", 0)
        macd_4h = a_4h.get("macd_histogram", 0)
        if macd_1h > 0 and macd_4h > 0:
            signals.append("✅ MACD Bullish (1h+4h)")
        elif macd_1h > 0:
            signals.append("📊 MACD Positive (1h)")
        elif macd_1h < 0 and macd_4h < 0:
            signals.append("⚠️ MACD Bearish (1h+4h)")

        # --- VWAP signals ---
        current_price = a_1h.get("current_price", 0)
        vwap = a_1h.get("vwap", 0)
        if current_price and vwap:
            if current_price > vwap:
                signals.append("📈 Above VWAP")
            else:
                signals.append("📉 Below VWAP")

        # --- Bollinger Band signals ---
        bb_lower = a_1h.get("bb_lower", 0)
        bb_upper = a_1h.get("bb_upper", 0)
        if current_price and bb_lower and current_price < bb_lower:
            signals.append("🎯 Below Lower Bollinger Band")
        elif current_price and bb_upper and current_price > bb_upper:
            signals.append("🎯 Above Upper Bollinger Band")

        # --- Volume surge ---
        if volume_surge:
            signals.append("🌊 1h Volume Surge (1.5x avg)")

        # --- Sentiment signals ---
        if sentiment_data:
            sent_score = sentiment_data.get("sentiment_score", 0)
            funding = sentiment_data.get("funding_rate")
            oi_change = sentiment_data.get("oi_change_pct")

            if sent_score >= 8:
                signals.append(f"😊 Strong Positive Sentiment ({sent_score:.1f})")
            elif sent_score <= -8:
                signals.append(f"😰 Strong Negative Sentiment ({sent_score:.1f})")

            if funding is not None:
                if funding < -0.01:
                    signals.append(
                        f"💚 Negative Funding ({funding:.4f}) — contrarian bullish"
                    )
                elif funding > 0.03:
                    signals.append(
                        f"💸 High Funding ({funding:.4f}) — overleveraged longs"
                    )

            if oi_change is not None:
                if oi_change > 10:
                    signals.append(f"📈 OI Surge +{oi_change:.1f}%")
                elif oi_change < -10:
                    signals.append(f"📉 OI Drop {oi_change:.1f}%")

        # --- NEW: Pre-pump detection signals ---
        if new_signals_data:
            # OBV Divergence
            obv_div = new_signals_data.get("obv_div")
            if obv_div and obv_div.get("detected"):
                signals.append(
                    f"🐋 OBV Bullish Divergence (strength: {obv_div['strength']:.0f})"
                )
            elif obv_div and obv_div.get("obv_trend") == "rising":
                signals.append("📊 OBV Rising Trend")

            # Consolidation Breakout
            consol = new_signals_data.get("consolidation")
            if consol and consol.get("breaking_out"):
                vol_tag = " + Volume" if consol.get("volume_confirmed") else ""
                signals.append(
                    f"🚀 Consolidation Breakout ({consol['days_in_range']}d range{vol_tag})"
                )
            elif consol and consol.get("in_consolidation"):
                signals.append(
                    f"📦 In Consolidation ({consol['days_in_range']}d, {consol['range_pct']:.1f}% range)"
                )

            # BB Squeeze
            squeeze = new_signals_data.get("bb_squeeze")
            if squeeze and squeeze.get("squeezing"):
                signals.append(
                    f"⚡ BB Squeeze (percentile: {squeeze['percentile']:.0f}%)"
                )

            # RSI Divergence
            rsi_div = new_signals_data.get("rsi_div")
            if rsi_div and rsi_div.get("detected"):
                signals.append(
                    f"💎 RSI Bullish Divergence (strength: {rsi_div['strength']:.0f})"
                )

        return signals
