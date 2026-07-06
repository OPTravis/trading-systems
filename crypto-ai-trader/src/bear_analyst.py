"""
Bear Analyst — Provides bearish counter-arguments for high-scoring opportunities.

When the scanner identifies a promising trade, this agent plays devil's advocate:
it inverts key metrics (RSI, funding rate, Fear & Greed, TVL, volume) into a
bear_score (0-100) and, when warranted, asks DeepSeek LLM for additional risk
factors. If the bear case is stronger than the opportunity score, the trade is
vetoed.

Bear score > 70 AND bear_score > opportunity_score  →  trade vetoed.
"""

import json
import logging
from typing import Dict, List, Optional

from src.llm_client import get_llm_client

logger = logging.getLogger(__name__)


class BearResult:
    """Typed container for bearish analysis output."""

    __slots__ = ("bear_score", "veto", "reasons", "risk_factors", "confidence")

    def __init__(
        self,
        bear_score: float = 0.0,
        veto: bool = False,
        reasons: Optional[List[str]] = None,
        risk_factors: Optional[List[str]] = None,
        confidence: str = "LOW",
    ):
        self.bear_score = bear_score
        self.veto = veto
        self.reasons = reasons or []
        self.risk_factors = risk_factors or []
        self.confidence = confidence

    # Make it behave like a dict for easy serialisation / printing
    def to_dict(self) -> Dict:
        return {
            "bear_score": self.bear_score,
            "veto": self.veto,
            "reasons": self.reasons,
            "risk_factors": self.risk_factors,
            "confidence": self.confidence,
        }

    def __repr__(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


class BearAnalyst:
    """Bearish counter-argument agent for high-score opportunities.

    Pipeline:
    1. Extract raw metrics from opportunity_data
    2. Compute bear_score using inverted factor logic (0-100)
    3. If bear_score >= 50, call DeepSeek LLM for additional risk factors
    4. Determine veto: bear_score > 70 AND bear_score > opportunity_score
    """

    # --- Inverted factor thresholds / weights ---
    RSI_OVERBOUGHT_HIGH = 70  # +25 bear pts
    RSI_OVERBOUGHT_MID = 60  # +15 bear pts
    FUNDING_CROWDED_THRESHOLD = 0.01  # +20 bear pts (fraction, e.g. 0.0001 = 0.01%)
    FUNDING_EXTREME_THRESHOLD = (
        0.05  # +30 bear pts (extreme funding = forced liquidations likely)
    )
    TAKER_RATIO_HIGH = 1.5  # +15 bear pts (taker buy/sell ratio imbalance)
    TAKER_RATIO_EXTREME = 2.0  # +25 bear pts (extreme taker imbalance)
    FNG_EUPHORIA_HIGH = 70  # +20 bear pts
    FNG_EUPHORIA_MID = 60  # +10 bear pts
    TVL_DROP_THRESHOLD = -3.0  # +15 bear pts
    VOLUME_DECLINING_BONUS = 10  # +10 bear pts
    MAX_BEAR_SCORE = 100

    # Veto thresholds
    VETO_ABSOLUTE_THRESHOLD = 70  # bear_score must exceed this
    LLM_CALLOUT_THRESHOLD = 50  # only call LLM if bear_score >= this

    def analyze(
        self,
        symbol: str,
        opportunity_data: Dict,
        research_data: Dict,
    ) -> BearResult:
        """Run bearish analysis on an opportunity.

        Args:
            symbol: Trading pair (e.g. "BTCUSDT")
            opportunity_data: Scanner output with scores and indicators
            research_data: Researcher output (may be empty dict)

        Returns:
            BearResult with bear_score, veto flag, reasons, and risk_factors.
        """
        logger.info("BearAnalyst: evaluating %s", symbol)

        # 1. Extract metrics
        metrics = self._extract_metrics(opportunity_data, research_data)

        # 2. Compute bear_score
        bear_score, reasons = self._compute_bear_score(metrics)

        # 3. Optionally enrich with LLM
        risk_factors: List[str] = []
        confidence = "LOW"

        if bear_score >= self.LLM_CALLOUT_THRESHOLD:
            risk_factors, llm_confidence = self._llm_risk_analysis(
                symbol, metrics, bear_score
            )
            confidence = llm_confidence
        else:
            # Without LLM we have limited confidence
            confidence = "LOW"

        # 4. Determine veto
        opportunity_score = metrics.get("score", 0)
        veto = (
            bear_score > self.VETO_ABSOLUTE_THRESHOLD and bear_score > opportunity_score
        )

        if veto:
            logger.warning(
                "BearAnalyst: VETO on %s — bear_score %.1f > opportunity_score %.1f",
                symbol,
                bear_score,
                opportunity_score,
            )

        return BearResult(
            bear_score=round(bear_score, 1),
            veto=veto,
            reasons=reasons,
            risk_factors=risk_factors,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metrics(opportunity_data: Dict, research_data: Dict) -> Dict:
        """Pull the relevant raw metrics into a flat dict.

        Falls back to sensible defaults when keys are missing.
        """
        m: Dict = {}

        # Opportunity-level scores
        m["score"] = opportunity_data.get("score", 0)
        m["technical_score"] = opportunity_data.get("technical_score", 0)
        m["trend_strength"] = opportunity_data.get("trend_strength", 0)
        m["volume_surge"] = opportunity_data.get("volume_surge", 0)
        m["price"] = opportunity_data.get("price", 0)
        m["entry_price"] = opportunity_data.get("entry_price", 0)
        m["signals"] = opportunity_data.get("signals", [])

        # Bear-relevant indicators (may live in different places)
        m["rsi"] = opportunity_data.get("rsi", research_data.get("rsi", 50))
        m["funding_rate"] = opportunity_data.get(
            "funding_rate", research_data.get("funding_rate", 0)
        )
        m["market_sentiment"] = opportunity_data.get(
            "market_sentiment", research_data.get("market_sentiment", 50)
        )
        m["on_chain_score"] = opportunity_data.get(
            "on_chain_score", research_data.get("on_chain_score", 50)
        )
        m["fng"] = opportunity_data.get(
            "fng", research_data.get("fng", research_data.get("fear_greed", 50))
        )
        m["tvl_24h_change"] = opportunity_data.get(
            "tvl_24h_change", research_data.get("tvl_24h_change", 0)
        )
        m["volume_trend"] = opportunity_data.get(
            "volume_trend", research_data.get("volume_trend", "flat")
        )
        m["research_summary"] = opportunity_data.get(
            "research_summary", research_data.get("research_summary", "")
        )
        # On-chain indicators
        m["taker_buy_sell_ratio"] = opportunity_data.get(
            "taker_buy_sell_ratio",
            research_data.get(
                "taker_buy_sell_ratio",
                opportunity_data.get(
                    "taker_ratio", research_data.get("taker_ratio", 1.0)
                ),
            ),
        )
        m["funding_rate_8h"] = opportunity_data.get(
            "funding_rate_8h",
            research_data.get(
                "funding_rate_8h",
                opportunity_data.get(
                    "funding_rate", research_data.get("funding_rate", 0)
                ),
            ),
        )

        return m

    def _compute_bear_score(self, metrics: Dict) -> tuple:
        """Invert indicators into a bear_score 0-100.

        Returns (bear_score, reasons_list).
        """
        score = 0.0
        reasons: List[str] = []

        # --- RSI overbought ---
        rsi = float(metrics.get("rsi", 50))
        if rsi > self.RSI_OVERBOUGHT_HIGH:
            score += 25
            reasons.append(f"RSI overbought at {rsi:.0f}")
        elif rsi > self.RSI_OVERBOUGHT_MID:
            score += 15
            reasons.append(f"RSI elevated at {rsi:.0f}")

        # --- Bullish offset: RSI deeply oversold (bounce setup) ---
        if rsi < 30:
            score -= 15
            reasons.append(f"RSI oversold at {rsi:.0f} (bounce setup)")

        # --- Funding rate crowded long ---
        funding = float(metrics.get("funding_rate", 0))
        if funding > self.FUNDING_EXTREME_THRESHOLD:
            score += 30
            reasons.append(
                f"Extreme funding rate {funding * 100:.3f}% — high liquidation risk"
            )
        elif funding > self.FUNDING_CROWDED_THRESHOLD:
            score += 20
            reasons.append(f"Funding rate elevated at {funding * 100:.2f}%")

        # --- Bullish offset: extremely negative funding (short squeeze setup) ---
        if funding < -0.01:
            score -= 10
            reasons.append(
                f"Negative funding {funding * 100:.3f}% (short squeeze potential)"
            )

        # --- On-chain: Taker buy/sell ratio ---
        taker_ratio = float(metrics.get("taker_buy_sell_ratio", 1.0))
        if taker_ratio > self.TAKER_RATIO_EXTREME:
            score += 25
            reasons.append(
                f"Extreme taker buy/sell ratio {taker_ratio:.2f} — aggressive long chasing"
            )
        elif taker_ratio > self.TAKER_RATIO_HIGH:
            score += 15
            reasons.append(f"Taker buy/sell ratio elevated at {taker_ratio:.2f}")

        # --- Fear & Greed (euphoria) ---
        fng = float(metrics.get("fng", 50))
        if fng > self.FNG_EUPHORIA_HIGH:
            score += 20
            reasons.append(f"Fear & Greed euphoria at {fng:.0f}")
        elif fng > self.FNG_EUPHORIA_MID:
            score += 10
            reasons.append(f"Fear & Greed elevated at {fng:.0f}")

        # --- Bullish offset: extreme fear (contrarian buy signal) ---
        if fng < 25:
            score -= 10
            reasons.append(f"Extreme fear FNG={fng:.0f} (contrarian buy)")

        # --- TVL 24h change negative ---
        tvl_chg = float(metrics.get("tvl_24h_change", 0))
        if tvl_chg < self.TVL_DROP_THRESHOLD:
            score += 15
            reasons.append(f"TVL declining ({tvl_chg:.1f}% 24h)")

        # --- Volume trend declining ---
        vol_trend = str(metrics.get("volume_trend", "")).lower()
        if vol_trend in ("declining", "decreasing", "falling"):
            score += self.VOLUME_DECLINING_BONUS
            reasons.append("Volume trend declining")

        # Clamp to [5, MAX_BEAR_SCORE] — always produce at least a minimal bear analysis
        score = max(5, min(self.MAX_BEAR_SCORE, score))

        return score, reasons

    def _llm_risk_analysis(
        self, symbol: str, metrics: Dict, bear_score: float
    ) -> tuple:
        """Call LLM (DeepSeek) for additional bearish risk factors.

        Returns (risk_factors_list, confidence_str).
        """
        llm = get_llm_client()

        coin = symbol.replace("USDT", "").replace("BUSD", "").upper()

        prompt = (
            f"You are a bearish crypto analyst. The coin {coin} (pair {symbol}) "
            f"has a bear score of {bear_score:.0f}/100. "
            f"Here are the key metrics:\n"
            f"- RSI: {metrics.get('rsi', 'N/A')}\n"
            f"- Funding rate: {metrics.get('funding_rate', 'N/A')}\n"
            f"- Funding rate (8h): {metrics.get('funding_rate_8h', 'N/A')}\n"
            f"- Taker buy/sell ratio: {metrics.get('taker_buy_sell_ratio', 'N/A')}\n"
            f"- Fear & Greed: {metrics.get('fng', 'N/A')}\n"
            f"- TVL 24h change: {metrics.get('tvl_24h_change', 'N/A')}%\n"
            f"- Volume trend: {metrics.get('volume_trend', 'N/A')}\n"
            f"- Technical score: {metrics.get('technical_score', 'N/A')}\n"
            f"- Opportunity score: {metrics.get('score', 'N/A')}\n"
            f"- Market sentiment: {metrics.get('market_sentiment', 'N/A')}\n"
            f"- Research summary: {metrics.get('research_summary', 'N/A')}\n\n"
            f"Provide exactly 3-5 specific bearish risk factors for this coin. "
            f"Be concrete and data-driven. Return them as a JSON array of strings. "
            f"Also state your confidence as HIGH, MEDIUM, or LOW.\n"
            f'Respond ONLY with JSON: {{"risk_factors": [...], "confidence": "..."}}'
        )

        result = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a risk analyst.",
            temperature=0.3,
            max_tokens=512,
        )

        if result is None:
            logger.warning("BearAnalyst: all LLM providers failed for %s", symbol)
            return [], "LOW"

        try:
            content = result["content"]
            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            data = json.loads(content)
            risk_factors = data.get("risk_factors", [])
            confidence = data.get("confidence", "MEDIUM")

            # Ensure risk_factors is a list of strings
            if not isinstance(risk_factors, list):
                risk_factors = [str(risk_factors)]

            logger.info(
                "BearAnalyst: LLM returned %d risk factors (confidence=%s, provider=%s)",
                len(risk_factors),
                confidence,
                result.get("provider", "unknown"),
            )
            return risk_factors, confidence

        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning("BearAnalyst: failed to parse LLM response: %s", e)

        return [], "LOW"


# ──────────────────────────────────────────────────────────────────────
# Quick smoke test
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    analyst = BearAnalyst()
    result = analyst.analyze(
        "BTCUSDT",
        {
            "score": 80,
            "technical_score": 75,
            "rsi": 72,
            "funding_rate": 0.02,
            "taker_buy_sell_ratio": 1.7,
            "market_sentiment": 65,
        },
        {},
    )
    print(result)
