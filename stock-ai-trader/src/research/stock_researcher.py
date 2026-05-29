"""
LLM-based stock research with dual-model verification.

Combines news, fundamentals, technicals, and sentiment into a structured
ResearchReport with buy/hold/sell recommendation and confidence score.
Uses DeepSeek (primary) + Xiaomi (verification) for robustness.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import requests

from shared.core.state_db import get_state_db
from shared.risk.risk_manager import RiskManager
from src.brokers.broker_protocol import BrokerProtocol
from src.data.stock_data_feed import StockDataFeed

logger = logging.getLogger(__name__)


# ─── LLM endpoints ──────────────────────────────────────────────────────────

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

XIAOMI_API_URL = os.environ.get("XIAOMI_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1") + "/chat/completions"
XIAOMI_MODEL = "mimo-v2.5-pro"


class Recommendation(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


@dataclass
class ResearchReport:
    """Structured research output for a single stock."""
    symbol: str
    timestamp: datetime
    recommendation: Recommendation
    confidence: float              # 0.0 – 1.0
    summary: str                   # 1-2 paragraph executive summary
    bull_case: str                 # Key bull arguments
    bear_case: str                 # Key bear arguments
    fair_value_estimate: Optional[float] = None
    risk_rating: str = "MEDIUM"    # LOW / MEDIUM / HIGH
    catalysts: List[str] = field(default_factory=list)
    technical_summary: str = ""
    fundamental_summary: str = ""
    sentiment_score: float = 0.0   # -1 to +1
    primary_model: str = "deepseek"
    verification_model: str = "xiaomi"
    models_agreed: bool = True     # Did both models agree on direction?


# ─── Prompt templates ────────────────────────────────────────────────────────

_ANALYSIS_PROMPT = """You are a senior equity research analyst.
Analyze {symbol} and return a JSON object with these fields:
- recommendation: one of STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL
- confidence: float 0.0-1.0
- summary: 1-2 paragraph executive summary
- bull_case: key bullish arguments (2-3 sentences)
- bear_case: key bearish arguments (2-3 sentences)
- fair_value_estimate: estimated fair price per share (number or null)
- risk_rating: LOW, MEDIUM, or HIGH
- catalysts: list of upcoming catalysts (earnings, FDA, etc.)

Use the following data to inform your analysis:

TECHNICAL DATA:
{technical_data}

FUNDAMENTAL DATA:
{fundamental_data}

RECENT NEWS:
{news_data}

SENTIMENT SCORE (FinBERT): {sentiment_score}

Respond ONLY with valid JSON. No markdown fences."""


_VERIFICATION_PROMPT = """Review this stock analysis for {symbol} and respond with a JSON object:
- agree: true/false (do you agree with the recommendation?)
- your_recommendation: your independent recommendation (STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL)
- your_confidence: float 0.0-1.0
- concerns: list of any concerns with the original analysis

Original analysis:
{original_analysis}

Respond ONLY with valid JSON."""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _call_llm(api_url: str, model: str, api_key: str, prompt: str, temperature: float = 0.3) -> Optional[str]:
    """Call an LLM API and return the response text, or None on failure. Retries up to 3 times."""
    import time as _time
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                api_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": 2048,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            logger.warning("LLM call attempt %d/%d failed (%s/%s): %s", attempt + 1, max_retries, api_url, model, e)
            if attempt < max_retries - 1:
                _time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s
        except Exception as e:
            logger.error("LLM call failed (%s/%s): %s", api_url, model, e)
            return None
    logger.error("LLM call failed after %d retries (%s/%s)", max_retries, api_url, model)
    return None


def _parse_json(text: str) -> Optional[dict]:
    """Extract JSON from LLM response, tolerating markdown fences and extra text."""
    import re
    text = text.strip()
    if not text:
        return None

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try stripping markdown fences
    fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?\s*```"
    match = re.search(fence_pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    brace_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
    match = re.search(brace_pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse LLM JSON response (first 200 chars): %s", text[:200])
    return None


# ─── StockResearcher ─────────────────────────────────────────────────────────

class StockResearcher:
    """
    Deep research on individual stocks using dual LLM verification.

    Combines news, fundamentals, technicals, and sentiment from the
    data feeds, sends to DeepSeek for primary analysis, then validates
    with Xiaomi for cross-model verification.
    """

    def __init__(
        self,
        data_feed: StockDataFeed,
        fundamental_feed=None,
        news_feed=None,
        sentiment_feed=None,
        deepseek_key: Optional[str] = None,
        xiaomi_key: Optional[str] = None,
    ):
        self.data_feed = data_feed
        self.fundamental_feed = fundamental_feed
        self.news_feed = news_feed
        self.sentiment_feed = sentiment_feed
        self.deepseek_key = deepseek_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.xiaomi_key = xiaomi_key or os.environ.get("XIAOMI_API_KEY", "")

        if not self.deepseek_key:
            logger.warning("No DEEPSEEK_API_KEY set — primary LLM calls will fail")
        if not self.xiaomi_key:
            logger.warning("No XIAOMI_API_KEY set — verification calls will fail")

    def analyze_stock(self, symbol: str) -> ResearchReport:
        """
        Run full research pipeline on a stock symbol.

        1. Gather technical data (OHLCV, indicators)
        2. Gather fundamental data (P/E, revenue growth, etc.)
        3. Gather recent news headlines
        4. Run FinBERT sentiment on news
        5. Send to DeepSeek for primary analysis
        6. Send to Xiaomi for verification
        7. Merge into ResearchReport
        """
        logger.info("Starting research on %s", symbol)
        now = datetime.now(timezone.utc)

        # 1 — Technical data
        technical_data = self._gather_technicals(symbol)

        # 2 — Fundamental data
        fundamental_data = self._gather_fundamentals(symbol)

        # 3 — News
        news_data = self._gather_news(symbol)

        # 4 — Sentiment
        sentiment_score = self._compute_sentiment(news_data)

        # 5 — Primary LLM analysis
        prompt = _ANALYSIS_PROMPT.format(
            symbol=symbol,
            technical_data=technical_data,
            fundamental_data=fundamental_data,
            news_data=news_data,
            sentiment_score=f"{sentiment_score:.2f}",
        )

        primary_text = _call_llm(DEEPSEEK_API_URL, DEEPSEEK_MODEL, self.deepseek_key, prompt)
        primary_json = _parse_json(primary_text) if primary_text else None

        if primary_json is None:
            logger.error("Primary LLM analysis failed for %s — returning HOLD", symbol)
            return ResearchReport(
                symbol=symbol,
                timestamp=now,
                recommendation=Recommendation.HOLD,
                confidence=0.1,
                summary="LLM analysis unavailable. Defaulting to HOLD.",
                bull_case="",
                bear_case="",
                sentiment_score=sentiment_score,
                risk_rating="HIGH",
            )

        # 6 — Verification with second model
        models_agreed = True
        if self.xiaomi_key and primary_text:
            verify_prompt = _VERIFICATION_PROMPT.format(
                symbol=symbol,
                original_analysis=json.dumps(primary_json, indent=2),
            )
            verify_text = _call_llm(XIAOMI_API_URL, XIAOMI_MODEL, self.xiaomi_key, verify_prompt)
            verify_json = _parse_json(verify_text) if verify_text else None

            if verify_json and not verify_json.get("agree", True):
                models_agreed = False
                # Blend: reduce confidence when models disagree
                primary_json["confidence"] = min(
                    primary_json.get("confidence", 0.5),
                    verify_json.get("your_confidence", 0.5),
                )
                logger.warning(
                    "Models disagree on %s: primary=%s, verification=%s",
                    symbol,
                    primary_json.get("recommendation"),
                    verify_json.get("your_recommendation"),
                )

        # 7 — Build report
        rec_str = primary_json.get("recommendation", "HOLD").upper()
        try:
            recommendation = Recommendation(rec_str)
        except ValueError:
            recommendation = Recommendation.HOLD

        return ResearchReport(
            symbol=symbol,
            timestamp=now,
            recommendation=recommendation,
            confidence=float(primary_json.get("confidence", 0.5)),
            summary=primary_json.get("summary", ""),
            bull_case=primary_json.get("bull_case", ""),
            bear_case=primary_json.get("bear_case", ""),
            fair_value_estimate=primary_json.get("fair_value_estimate"),
            risk_rating=primary_json.get("risk_rating", "MEDIUM"),
            catalysts=primary_json.get("catalysts", []),
            technical_summary=technical_data[:500],
            fundamental_summary=fundamental_data[:500],
            sentiment_score=sentiment_score,
            models_agreed=models_agreed,
        )

    # ── Data gathering helpers ───────────────────────────────────────────

    def _gather_technicals(self, symbol: str) -> str:
        """Fetch recent OHLCV and compute basic indicators as text."""
        try:
            df = self.data_feed.get_history(symbol, period="3mo", interval="1d")
            if df is None or df.empty:
                return "No technical data available."

            last = df.iloc[-1]
            sma_20 = df["close"].tail(20).mean()
            sma_50 = df["close"].tail(50).mean()
            rsi = self._compute_rsi(df["close"], 14)
            avg_vol = df["volume"].tail(20).mean()
            pct_change_1w = (df["close"].iloc[-1] / df["close"].iloc[-5] - 1) * 100 if len(df) >= 5 else 0.0
            pct_change_1m = (df["close"].iloc[-1] / df["close"].iloc[-20] - 1) * 100 if len(df) >= 20 else 0.0

            return (
                f"Price: ${last['close']:.2f}  |  SMA20: ${sma_20:.2f}  |  SMA50: ${sma_50:.2f}\n"
                f"RSI(14): {rsi:.1f}  |  1W change: {pct_change_1w:+.1f}%  |  1M change: {pct_change_1m:+.1f}%\n"
                f"Avg 20d volume: {avg_vol:,.0f}  |  Latest volume: {last['volume']:,.0f}\n"
                f"52w high: ${df['high'].max():.2f}  |  52w low: ${df['low'].min():.2f}"
            )
        except Exception as e:
            logger.error("Technical gather failed for %s: %s", symbol, e)
            return f"Technical data error: {e}"

    def _gather_fundamentals(self, symbol: str) -> str:
        """Fetch key fundamental metrics as text."""
        if not self.fundamental_feed:
            return "Fundamental feed not configured."
        try:
            metrics = self.fundamental_feed.get_key_metrics(symbol)
            if not metrics:
                return "No fundamental data available."
            parts = []
            for k, v in metrics.items():
                if v is not None:
                    parts.append(f"{k}: {v}")
            return "\n".join(parts[:30])
        except Exception as e:
            logger.error("Fundamental gather failed for %s: %s", symbol, e)
            return f"Fundamental data error: {e}"

    def _gather_news(self, symbol: str) -> str:
        """Fetch recent news headlines as text."""
        if not self.news_feed:
            return "News feed not configured."
        try:
            articles = self.news_feed.get_news(symbol, days=7)
            if not articles:
                return "No recent news."
            headlines = []
            for a in articles[:10]:
                title = a.get("title", "")
                desc = a.get("description", "")[:120]
                headlines.append(f"- {title}: {desc}")
            return "\n".join(headlines)
        except Exception as e:
            logger.error("News gather failed for %s: %s", symbol, e)
            return f"News data error: {e}"

    def _compute_sentiment(self, news_text: str) -> float:
        """Run FinBERT sentiment on news text. Returns -1 to +1."""
        if not self.sentiment_feed:
            return 0.0
        try:
            return self.sentiment_feed.analyze_text(news_text)
        except Exception as e:
            logger.error("Sentiment computation failed: %s", e)
            return 0.0

    @staticmethod
    def _compute_rsi(prices, period: int = 14) -> float:
        """Compute RSI from a price series."""
        if len(prices) < period + 1:
            return 50.0
        deltas = prices.diff().dropna()
        gains = deltas.where(deltas > 0, 0.0).tail(period).mean()
        losses = (-deltas.where(deltas < 0, 0.0)).tail(period).mean()
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))
