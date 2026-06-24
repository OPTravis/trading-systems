"""
Market Researcher - Deep research on high-score coins.
Triggered when scan finds a high-score opportunity.
Searches news, on-chain metrics, and fundamental data to enrich scoring.
"""

import json
import logging
import os
import time
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeout,
)
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

from src.llm_client import get_llm_client


class _IPv4HTTPAdapter(HTTPAdapter):
    """Force IPv4 connections — Jina s.jina.ai hangs on IPv6 from this host."""

    def send(self, *args, **kwargs):
        # Monkey-patch urllib3 to force IPv4 for this request
        import urllib3.util.connection as uc

        orig = uc.create_connection

        def _ipv4_create_connection(address, *a, **kw):
            host, port = address
            # Force getaddrinfo to return only AF_INET (IPv4)
            import socket

            for res in socket.getaddrinfo(
                host, port, socket.AF_INET, socket.SOCK_STREAM
            ):
                af, socktype, proto, canonname, sa = res
                sock = socket.socket(af, socktype, proto)
                try:
                    sock.connect(sa)
                    return sock
                except Exception:
                    sock.close()
            raise OSError(f"IPv4 connection to {host}:{port} failed")

        uc.create_connection = _ipv4_create_connection
        try:
            return super().send(*args, **kwargs)
        finally:
            uc.create_connection = orig


# Reusable session with IPv4 adapter for Jina API
_jina_session = requests.Session()
_jina_session.mount("https://", _IPv4HTTPAdapter())

# Load .env from project root
load_dotenv(Path(__file__).parent.parent / ".env")
load_dotenv(Path.home() / ".hermes" / ".env")  # Fallback for shared keys

logger = logging.getLogger(__name__)

from src.utils import get_project_root

_DATA_DIR = get_project_root() / "data"


def _ensure_data_dir() -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def _load_json(filepath: Path, default: Optional[Any] = None) -> Any:
    try:
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {filepath}: {e}")
    return default if default is not None else {}


def _save_json(filepath: Path, data: Any) -> bool:
    try:
        _ensure_data_dir()
        tmp_path = filepath.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(filepath)
        return True
    except Exception as e:
        logger.error(f"Failed to save {filepath}: {e}")
        return False


class MarketResearcher:
    """Deep research on coins that pass the initial scan threshold.

    Research pipeline:
    1. News sentiment (keyword-based + source credibility)
    2. On-chain metrics (whale movements, exchange flows via public APIs)
    3. Fundamental catalysts (upcoming events, partnerships, upgrades)
    4. Score adjustment based on research findings

    All results persisted to data/research/{symbol}_{date}.json
    """

    # Score adjustment bounds
    MAX_ADJUSTMENT = 15.0  # max points added
    MIN_ADJUSTMENT = -15.0  # max points subtracted

    # Research cache TTL (avoid re-researching same coin within window)
    CACHE_TTL = 3600  # 1 hour

    def __init__(self):
        self._research_dir = _DATA_DIR / "research"
        self._research_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, Dict] = {}
        self._cache_ts: Dict[str, float] = {}
        self._load_recent_cache()

    def _load_recent_cache(self):
        """Load recent research files from disk into in-memory cache.

        On process restart, this avoids redundant LLM API calls for research
        that was completed within the CACHE_TTL window.
        """
        now = time.time()
        try:
            for f in self._research_dir.glob("*.json"):
                try:
                    mtime = f.stat().st_mtime
                    age = now - mtime
                    if age < self.CACHE_TTL:
                        data = json.loads(f.read_text())
                        # Extract coin from filename: {COIN}_{DATE}.json
                        coin = f.stem.split("_")[0]
                        self._cache[coin] = data
                        self._cache_ts[coin] = mtime
                except Exception:
                    continue
            if self._cache:
                logger.info(
                    f"Loaded {len(self._cache)} cached research results from disk"
                )
        except Exception:
            logger.debug("Failed to load research cache from disk")

    # Total research timeout (seconds) — prevents scan from exceeding cron limit
    RESEARCH_TIMEOUT = 60  # Raised from 45: Jina retry+LLM dual-model needs headroom

    def research(
        self, symbol: str, binance_client: Optional["ExchangeClient"] = None
    ) -> Dict:
        """Run full research pipeline on a symbol.

        All three research stages run in parallel with a hard timeout.

        Returns:
            {
                symbol: str,
                news: [{title, summary, sentiment, source, url}],
                onchain: {whale_activity, exchange_flow},
                catalysts: [{event, date, impact}],
                sentiment_summary: str,
                score_adjustment: float,  # -15 to +15
                confidence: str,  # HIGH/MEDIUM/LOW
                timestamp: str,
            }
        """
        coin = symbol.replace("USDT", "").replace("/", "").upper()

        # Check cache
        cache_key = coin
        now = time.time()
        if (
            cache_key in self._cache
            and (now - self._cache_ts.get(cache_key, 0)) < self.CACHE_TTL
        ):
            logger.info(f"MarketResearcher: returning cached research for {coin}")
            return self._cache[cache_key]

        logger.info(f"MarketResearcher: starting deep research on {coin}")

        # Pipeline — news + onchain in parallel, catalysts from news (instant)
        news = []
        onchain = {}
        catalysts = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures: Dict[Future[Any], str] = {
                pool.submit(self._research_news, coin): "news",
                pool.submit(self._research_onchain, coin, binance_client): "onchain",
            }
            try:
                for fut in as_completed(futures, timeout=self.RESEARCH_TIMEOUT):
                    key = futures[fut]
                    try:
                        if key == "news":
                            news = fut.result()
                        elif key == "onchain":
                            onchain = fut.result()
                    except Exception as e:
                        logger.warning(
                            f"MarketResearcher: {key} failed for {coin}: {e}"
                        )
            except FuturesTimeout:
                logger.warning(
                    f"MarketResearcher: research timeout ({self.RESEARCH_TIMEOUT}s) for {coin}"
                )
                for fut in futures:
                    fut.cancel()

        # Catalysts: extract from news articles (no external API, instant)
        catalysts = self._research_catalysts(coin, news_articles=news)

        # Calculate score adjustment
        score_adj, confidence = self._calculate_adjustment(news, onchain, catalysts)

        result = {
            "symbol": symbol,
            "coin": coin,
            "news": news[:5],
            "onchain": onchain,
            "catalysts": catalysts,
            "sentiment_summary": self._summarize(news, onchain, catalysts),
            "score_adjustment": round(score_adj, 2),
            "confidence": confidence,
            "timestamp": datetime.now().isoformat(),
        }

        # Cache
        self._cache[cache_key] = result
        self._cache_ts[cache_key] = now

        # Persist
        date_str = datetime.now().strftime("%Y%m%d")
        filepath = self._research_dir / f"{coin}_{date_str}.json"
        _save_json(filepath, result)

        logger.info(
            f"MarketResearcher: {coin} complete – adj={score_adj:+.1f} confidence={confidence}"
        )
        return result

    def _research_news(self, coin: str) -> List[Dict]:
        """Search recent news for the coin using Jina Search API."""
        api_key = os.environ.get("JINA_API_KEY")
        if not api_key:
            logger.warning("MarketResearcher: JINA_API_KEY not set, skipping news")
            return []

        # FIX-10: Retry up to 3 times with exponential backoff (2s, 4s, 8s = 14s total)
        # Reduced from 5 retries (60s worst-case) — 3 retries fits within RESEARCH_TIMEOUT
        import time as _time

        for attempt in range(3):
            try:
                if attempt > 0:
                    _time.sleep(
                        min(2**attempt, 30)
                    )  # exponential: 2s, 4s, 8s, 16s (cap 30s)
                resp = _jina_session.get(
                    f"https://s.jina.ai/{coin}+crypto+latest+news",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Accept": "application/json",
                    },
                    timeout=12,
                )
                data = resp.json()

                articles = []
                for r in data.get("data", [])[:5]:  # Cap at 5 articles
                    text = r.get("description", "") or r.get("content", "")
                    articles.append(
                        {
                            "title": r.get("title", ""),
                            "summary": text[:300],
                            "sentiment": 0.0,  # placeholder — filled by batch LLM below
                            "source": (
                                r.get("url", "").split("/")[2]
                                if "/" in r.get("url", "")
                                else ""
                            ),
                            "url": r.get("url", ""),
                        }
                    )

                # Batch sentiment: one LLM call for all articles instead of per-article
                if articles:
                    sentiments = self._batch_llm_sentiment(articles)
                    for i, s in enumerate(sentiments):
                        if i < len(articles):
                            if isinstance(s, dict):
                                # New structured format
                                articles[i]["sentiment"] = round(
                                    s.get("sentiment", 0.0), 2
                                )
                                articles[i]["sentiment_score"] = s.get("score", 5)
                                articles[i]["sentiment_confidence"] = s.get(
                                    "confidence", 0.5
                                )
                                articles[i]["primary_score"] = s.get("primary_score")
                                articles[i]["secondary_score"] = s.get(
                                    "secondary_score"
                                )
                            else:
                                # Backward compatibility: old float format
                                articles[i]["sentiment"] = round(s, 2)

                # Sort by absolute sentiment (most impactful first)
                articles.sort(key=lambda x: abs(x["sentiment"]), reverse=True)
                return articles

            except Exception as e:
                if attempt == 1:
                    logger.error(
                        f"MarketResearcher: news search failed for {coin}: {e}"
                    )
                continue

        # Jina failed after 3 retries — fall back to DDGS
        logger.warning(
            f"MarketResearcher: Jina search exhausted for {coin}, trying DDGS fallback"
        )
        return self._research_news_ddgs(coin)

    def _research_news_ddgs(self, coin: str) -> List[Dict]:
        """Fallback news search using DuckDuckGo when Jina is unavailable."""
        try:
            from ddgs import DDGS

            ddgs = DDGS()
            raw = list(ddgs.text(f"{coin} crypto news", max_results=5))
            if not raw:
                return []

            articles = []
            for r in raw:
                articles.append(
                    {
                        "title": r.get("title", ""),
                        "summary": (r.get("body", "") or "")[:300],
                        "sentiment": 0.0,
                        "source": (
                            r.get("href", "").split("/")[2]
                            if "/" in r.get("href", "")
                            else ""
                        ),
                        "url": r.get("href", ""),
                    }
                )

            # Batch sentiment analysis on DDGS results
            if articles:
                sentiments = self._batch_llm_sentiment(articles)
                for i, s in enumerate(sentiments):
                    if i < len(articles):
                        if isinstance(s, dict):
                            articles[i]["sentiment"] = round(s.get("sentiment", 0.0), 2)
                        else:
                            articles[i]["sentiment"] = round(s, 2)

            articles.sort(key=lambda x: abs(x["sentiment"]), reverse=True)
            logger.info(
                f"MarketResearcher: DDGS fallback returned {len(articles)} articles for {coin}"
            )
            return articles
        except Exception as e:
            logger.warning(
                f"MarketResearcher: DDGS fallback also failed for {coin}: {e}"
            )
            return []

    def _research_onchain(
        self, coin: str, binance_client: Optional["ExchangeClient"] = None
    ) -> Dict:
        """Gather on-chain/exchange metrics from Binance API."""
        result: Dict[str, Any] = {
            "whale_activity": "UNKNOWN",
            "exchange_flow": "UNKNOWN",
            "volume_trend": "UNKNOWN",
            "funding_rate": None,
            "oi_change": None,
        }

        if not binance_client:
            return result

        try:
            symbol = f"{coin}USDT"

            # 24h stats for volume trend
            stats = binance_client.get_24hr_stats(symbol)
            if stats:
                float(stats.get("volume", 0))
                quote_vol = float(stats.get("quote_volume", 0))
                price_change = float(stats.get("price_change_percent", 0))
                int(stats.get("count", 0))

                # Volume trend heuristic
                if quote_vol > 50_000_000 and price_change > 3:
                    result["volume_trend"] = "SURGE_UP"
                elif quote_vol > 50_000_000 and price_change < -3:
                    result["volume_trend"] = "SURGE_DOWN"
                elif quote_vol > 10_000_000:
                    result["volume_trend"] = "ACTIVE"
                else:
                    result["volume_trend"] = "LOW"

            # Whale activity detection via recent trades
            # (replaces futures-based whale detection for SPOT-only system)
            try:
                trades = binance_client.get_trades(symbol, limit=1000)
                if trades and len(trades) >= 50:
                    trade_vals = []
                    for t in trades:
                        qty = float(t.get("qty", 0))
                        price = float(t.get("price", 0))
                        trade_vals.append(qty * price)

                    if trade_vals:
                        avg_trade = sum(trade_vals) / len(trade_vals)
                        threshold = max(avg_trade * 5.0, 10_000)  # 5x avg or $10k min

                        whale_buys = 0.0
                        whale_sells = 0.0
                        whale_count = 0
                        for i, val in enumerate(trade_vals):
                            if val >= threshold:
                                whale_count += 1
                                # isBuyerMaker=True means seller initiated (market sell)
                                if trades[i].get("isBuyerMaker", False):
                                    whale_sells += val
                                else:
                                    whale_buys += val

                        if whale_count >= 3:
                            total_whale = whale_buys + whale_sells
                            buy_ratio = whale_buys / total_whale if total_whale > 0 else 0.5
                            if buy_ratio > 0.65:
                                result["whale_activity"] = "ACCUMULATING"
                            elif buy_ratio < 0.35:
                                result["whale_activity"] = "DISTRIBUTING"
                            else:
                                result["whale_activity"] = "ACTIVE"
                        else:
                            result["whale_activity"] = "DORMANT"

                        logger.debug(
                            "Whale analysis %s: %d whale trades (>$%.0f), "
                            "buys=$%.0f sells=$%.0f → %s",
                            symbol, whale_count, threshold,
                            whale_buys, whale_sells, result["whale_activity"],
                        )
            except Exception as e:
                logger.debug(f"Whale activity detection failed for {symbol}: {e}")

            # Exchange flow estimation via order book depth imbalance
            try:
                order_book = binance_client.get_order_book(symbol, limit=20)
                bids = order_book.get("bids", [])
                asks = order_book.get("asks", [])
                if bids and asks:
                    bid_total = sum(p * q for p, q in bids)
                    ask_total = sum(p * q for p, q in asks)
                    total = bid_total + ask_total
                    if total > 0:
                        bid_pct = bid_total / total
                        if bid_pct > 0.58:
                            result["exchange_flow"] = "NET_INFLOW"
                        elif bid_pct < 0.42:
                            result["exchange_flow"] = "NET_OUTFLOW"
                        else:
                            result["exchange_flow"] = "BALANCED"

                        logger.debug(
                            "Order book %s: bid=$%.0f ask=$%.0f bid_pct=%.1f%% → %s",
                            symbol, bid_total, ask_total, bid_pct * 100,
                            result["exchange_flow"],
                        )
            except Exception as e:
                logger.debug(f"Order book flow detection failed for {symbol}: {e}")

        except Exception as e:
            logger.error(f"MarketResearcher: onchain research failed for {coin}: {e}")

        return result

    def _research_catalysts(
        self, coin: str, news_articles: Optional[List[Dict]] = None
    ) -> List[Dict]:
        """Extract catalyst signals from news articles (no external API call).

        Reuses news results instead of making a flaky separate Jina search.
        """
        if not news_articles:
            return []

        positive_kw = [
            "launch",
            "upgrade",
            "partnership",
            "mainnet",
            "listing",
            "adoption",
            "bullish",
            "growth",
            "breakout",
            "rally",
        ]
        negative_kw = [
            "hack",
            "exploit",
            "ban",
            "regulation",
            "delist",
            "lawsuit",
            "crash",
            "bearish",
            "warning",
            "risk",
        ]

        catalysts = []
        for r in news_articles:
            text = (r.get("summary", "") or "").lower()
            title = (r.get("title", "") or "").lower()
            combined = title + " " + text

            pos_count = sum(1 for kw in positive_kw if kw in combined)
            neg_count = sum(1 for kw in negative_kw if kw in combined)

            impact = "NEUTRAL"
            if pos_count > neg_count + 1:
                impact = "POSITIVE"
            elif neg_count > pos_count + 1:
                impact = "NEGATIVE"

            if impact != "NEUTRAL":
                catalysts.append(
                    {
                        "event": r.get("title", ""),
                        "source": r.get("source", ""),
                        "impact": impact,
                    }
                )

        return catalysts[:3]

    def _score_text_sentiment(self, text: str) -> float:
        """Score text sentiment: returns -1 to +1.

        Uses LLM for deep analysis when available, falls back to keyword matching.
        """
        if not text:
            return 0.0

        # Try LLM-based sentiment analysis
        try:
            llm_score = self._llm_sentiment(text)
            if llm_score is not None:
                return llm_score
        except Exception:
            logger.error("LLM sentiment analysis failed", exc_info=True)

        # Fallback: keyword matching
        text_lower = text.lower()

        positive = [
            "bullish",
            "buy",
            "surge",
            "pump",
            "rally",
            "breakout",
            "growth",
            "adoption",
            "partnership",
            "launch",
            "upgrade",
            "all-time high",
            "ath",
            "moon",
            "gain",
            "positive",
        ]
        negative = [
            "bearish",
            "sell",
            "crash",
            "dump",
            "decline",
            "hack",
            "exploit",
            "ban",
            "regulation",
            "delist",
            "lawsuit",
            "fraud",
            "scam",
            "risk",
            "warning",
            "drop",
            "fall",
        ]

        score = 0.0
        for word in positive:
            if word in text_lower:
                score += 0.1
        for word in negative:
            if word in text_lower:
                score -= 0.1

        return max(-1.0, min(1.0, score))

    def _batch_llm_sentiment(self, articles: List[Dict]) -> List[Dict]:
        """Batch sentiment analysis with dual model cross-verification.

        Returns list of dicts with structured scoring:
        [{"score": 1-10, "confidence": 0.0-1.0, "sentiment": -1.0 to 1.0}, ...]

        Uses mimo-v2.5-pro as primary, DeepSeek as verification.
        Both models must agree (within 2 points) for HIGH confidence.
        Falls back to keyword matching on failure.
        """
        if not articles:
            return []

        xiaomi_key = os.environ.get("XIAOMI_API_KEY")
        if not xiaomi_key:
            return [
                self._keyword_sentiment_structured(a.get("summary", ""))
                for a in articles
            ]

        # Build batch prompt — numbered list of article texts
        numbered = []
        for i, a in enumerate(articles):
            text = (a.get("title", "") + " " + a.get("summary", ""))[:200]
            numbered.append(f"[{i+1}] {text}")

        prompt = (
            "Rate each crypto news sentiment on a scale of 1-10 (1=extremely bearish, 5=neutral, 10=extremely bullish). "
            "Return ONLY a JSON array of objects with 'score' (1-10) and 'confidence' (0.0-1.0). "
            'Example: [{"score": 7, "confidence": 0.8}, {"score": 3, "confidence": 0.6}]\n\n'
            + "\n".join(numbered)
        )

        # --- Primary model (mimo-v2.5-pro) ---
        primary_scores = self._call_llm_for_sentiment(
            prompt, articles, "mimo-v2.5-pro", "second"
        )

        # --- Verification (DeepSeek) — best-effort ---
        verification_scores = None
        try:
            verification_scores = self._call_llm_for_sentiment(
                prompt, articles, "deepseek-v4-pro", "primary"
            )
        except Exception as e:
            logger.info(
                f"MarketResearcher: DeepSeek verification unavailable ({e}), using primary only"
            )

        # --- Cross-verify ---
        if primary_scores and verification_scores:
            return self._cross_verify_sentiment(
                primary_scores, verification_scores, articles
            )
        elif primary_scores:
            logger.info(
                "MarketResearcher: DeepSeek verification unavailable, using primary only"
            )
            return primary_scores
        else:
            logger.warning(
                "MarketResearcher: both LLMs failed, falling back to keywords"
            )
            return [
                self._keyword_sentiment_structured(a.get("summary", ""))
                for a in articles
            ]

    def _call_llm_for_sentiment(
        self, prompt: str, articles: List[Dict], model: str, client_type: str
    ) -> Optional[List[Dict]]:
        """Call a single LLM for sentiment analysis."""
        try:
            llm: Any = None
            if client_type == "primary":
                llm = get_llm_client()
            else:
                from src.llm_client import get_second_opinion_client

                llm = get_second_opinion_client()
                if llm is None:
                    return None

            assert llm is not None
            result = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                system_prompt="You are a crypto sentiment rater. Return ONLY a JSON array of objects.",
                max_tokens=300,
                temperature=0.0,
            )

            if result is not None:
                content = result.get("content", "")

                # mimo-v2.5-pro uses reasoning_content — if content is empty, check reasoning
                if not content and result.get("reasoning_content"):
                    logger.info(
                        "MarketResearcher: mimo-v2.5-pro returned reasoning only, extracting from reasoning_content"
                    )
                    reasoning = result["reasoning_content"]
                    import re

                    match = re.search(r"\[.*?\]", reasoning, re.DOTALL)
                    if match:
                        content = match.group()

                if not content:
                    logger.warning(
                        f"MarketResearcher: {client_type} returned empty content"
                    )
                    return None

                import re

                match = re.search(r"\[.*?\]", content, re.DOTALL)
                if match:
                    scores = json.loads(match.group())
                    result_list = []
                    for i, s in enumerate(scores):
                        if isinstance(s, dict):
                            score = max(1, min(10, int(s.get("score", 5))))
                            conf = max(0.0, min(1.0, float(s.get("confidence", 0.5))))
                        else:
                            # Backward compatibility: if LLM returns floats
                            score = max(1, min(10, int((float(s) + 1) * 4.5 + 1)))
                            conf = 0.5
                        result_list.append(
                            {
                                "score": score,
                                "confidence": conf,
                                "sentiment": (score - 5) / 5.0,
                            }
                        )
                    # Pad with keyword fallback if LLM returned fewer scores
                    while len(result_list) < len(articles):
                        idx = len(result_list)
                        result_list.append(
                            self._keyword_sentiment_structured(
                                articles[idx].get("summary", "")
                            )
                        )
                    return result_list[: len(articles)]
        except Exception as e:
            logger.warning(f"MarketResearcher: {client_type} LLM sentiment failed: {e}")
        return None

    def _cross_verify_sentiment(
        self, primary: List[Dict], secondary: List[Dict], articles: List[Dict]
    ) -> List[Dict]:
        """Cross-verify sentiment from two models.

        If scores agree within 2 points: HIGH confidence (0.9)
        If scores agree within 4 points: MEDIUM confidence (0.7)
        Otherwise: LOW confidence (0.5), average the scores
        """
        result = []
        for i in range(len(articles)):
            p = (
                primary[i]
                if i < len(primary)
                else self._keyword_sentiment_structured(articles[i].get("summary", ""))
            )
            s = (
                secondary[i]
                if i < len(secondary)
                else self._keyword_sentiment_structured(articles[i].get("summary", ""))
            )

            score_diff = abs(p["score"] - s["score"])
            avg_score = (p["score"] + s["score"]) / 2

            if score_diff <= 2:
                # Models agree — HIGH confidence
                confidence = 0.9
            elif score_diff <= 4:
                # Models somewhat agree — MEDIUM confidence
                confidence = 0.7
            else:
                # Models disagree — LOW confidence
                confidence = 0.5
                logger.info(
                    f"MarketResearcher: sentiment disagreement article {i+1}: primary={p['score']}, secondary={s['score']}"
                )

            result.append(
                {
                    "score": round(avg_score),
                    "confidence": confidence,
                    "sentiment": (avg_score - 5) / 5.0,
                    "primary_score": p["score"],
                    "secondary_score": s["score"],
                }
            )
        return result

    def _keyword_sentiment_structured(self, text: str) -> Dict:
        """Keyword-based sentiment with structured output."""
        if not text:
            return {"score": 5, "confidence": 0.3, "sentiment": 0.0}
        text_lower = text.lower()
        positive = [
            "bullish",
            "buy",
            "surge",
            "pump",
            "rally",
            "breakout",
            "growth",
            "adoption",
            "partnership",
            "launch",
            "upgrade",
            "all-time high",
            "ath",
            "moon",
            "gain",
            "positive",
        ]
        negative = [
            "bearish",
            "sell",
            "crash",
            "dump",
            "decline",
            "hack",
            "exploit",
            "ban",
            "regulation",
            "delist",
            "lawsuit",
            "fraud",
            "scam",
            "risk",
            "warning",
            "drop",
            "fall",
        ]
        score = 0.0
        for word in positive:
            if word in text_lower:
                score += 0.1
        for word in negative:
            if word in text_lower:
                score -= 0.1
        # Convert from [-1, 1] to [1, 10]
        normalized_score = max(1, min(10, int((score + 1) * 4.5 + 1)))
        return {"score": normalized_score, "confidence": 0.3, "sentiment": score}

    def _keyword_sentiment(self, text: str) -> float:
        """Fast keyword-based sentiment (no LLM)."""
        if not text:
            return 0.0
        text_lower = text.lower()
        positive = [
            "bullish",
            "buy",
            "surge",
            "pump",
            "rally",
            "breakout",
            "growth",
            "adoption",
            "partnership",
            "launch",
            "upgrade",
            "all-time high",
            "ath",
            "moon",
            "gain",
            "positive",
        ]
        negative = [
            "bearish",
            "sell",
            "crash",
            "dump",
            "decline",
            "hack",
            "exploit",
            "ban",
            "regulation",
            "delist",
            "lawsuit",
            "fraud",
            "scam",
            "risk",
            "warning",
            "drop",
            "fall",
        ]
        score = 0.0
        for word in positive:
            if word in text_lower:
                score += 0.1
        for word in negative:
            if word in text_lower:
                score -= 0.1
        return max(-1.0, min(1.0, score))

    def _calculate_adjustment(
        self,
        news: List[Dict],
        onchain: Dict,
        catalysts: List[Dict],
    ) -> tuple:
        """Calculate score adjustment from research data.

        Returns (adjustment: float, confidence: str).
        """
        adj = 0.0
        confidence_signals = 0
        total_signals = 0

        # --- News sentiment --- (updated for structured scoring)
        if news:
            # Calculate weighted average sentiment using confidence
            total_weight = 0.0
            weighted_sentiment = 0.0
            avg_confidence = 0.0
            for n in news:
                sentiment = n.get("sentiment", 0.0)
                confidence = n.get("sentiment_confidence", 0.5)
                weighted_sentiment += sentiment * confidence
                total_weight += confidence
                avg_confidence += confidence

            if total_weight > 0:
                avg_sentiment = weighted_sentiment / total_weight
                avg_confidence = avg_confidence / len(news)
            else:
                avg_sentiment = sum(n.get("sentiment", 0.0) for n in news) / len(news)
                avg_confidence = 0.5

            total_signals += 1
            # Use confidence-weighted adjustment
            if abs(avg_sentiment) > 0.2:
                confidence_signals += 1
                # Scale by confidence: high confidence = full weight, low = reduced
                confidence_multiplier = 0.5 + (avg_confidence * 0.5)  # 0.5-1.0 range
                adj += avg_sentiment * 8 * confidence_multiplier  # max ±8 from news
            else:
                adj += avg_sentiment * 3

        # --- On-chain metrics ---
        # Whale activity
        whale = onchain.get("whale_activity", "UNKNOWN")
        if whale != "UNKNOWN":
            total_signals += 1
            whale_map = {
                "LONG_HEAVY": 3.0,
                "SLIGHT_LONG": 2.0,
                "NEUTRAL": 0.0,
                "SLIGHT_SHORT": -2.0,
                "SHORT_HEAVY": -3.0,
                "WHALE_LONG": 4.5,  # Top trader long positioning (highest-quality signal)
                "WHALE_SHORT": -4.5,  # Top trader short positioning
            }
            whale_adj = whale_map.get(whale, 0.0)
            adj += whale_adj
            if abs(whale_adj) >= 2:
                confidence_signals += 1

        # Funding rate
        funding = onchain.get("funding_rate")
        if funding is not None:
            total_signals += 1
            # Contrarian: very high funding = crowded long = risk
            if funding > 0.1:
                adj -= 3.0  # Overcrowded, likely pullback
                confidence_signals += 1
            elif funding > 0.03:
                adj -= 1.0
            elif funding < -0.1:
                adj += 3.0  # Overcrowded short = squeeze potential
                confidence_signals += 1
            elif funding < -0.03:
                adj += 1.0

        # Volume trend
        vol_trend = onchain.get("volume_trend", "UNKNOWN")
        if vol_trend != "UNKNOWN":
            total_signals += 1
            if vol_trend == "SURGE_UP":
                adj += 2.0
                confidence_signals += 1
            elif vol_trend == "SURGE_DOWN":
                adj -= 2.0
                confidence_signals += 1

        # --- Catalysts ---
        if catalysts:
            total_signals += 1
            cat_adj = 0.0
            for c in catalysts:
                if c["impact"] == "POSITIVE":
                    cat_adj += 2.0
                elif c["impact"] == "NEGATIVE":
                    cat_adj -= 2.0
            adj += max(-5.0, min(5.0, cat_adj))
            if abs(cat_adj) >= 2:
                confidence_signals += 1

        # Confidence level
        if total_signals == 0:
            confidence = "LOW"
        elif confidence_signals / total_signals >= 0.6:
            confidence = "HIGH"
        elif confidence_signals / total_signals >= 0.3:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        # Clamp
        adj = max(self.MIN_ADJUSTMENT, min(self.MAX_ADJUSTMENT, adj))

        return adj, confidence

    def _summarize(self, news, onchain, catalysts) -> str:
        """Generate one-line summary of research."""
        parts = []

        if news:
            avg = sum(n["sentiment"] for n in news) / len(news)
            if avg > 0.3:
                parts.append("新聞偏正面")
            elif avg < -0.3:
                parts.append("新聞偏負面")
            else:
                parts.append("新聞中性")

        whale = onchain.get("whale_activity", "UNKNOWN")
        if whale != "UNKNOWN":
            whale_cn = {
                "LONG_HEAVY": "鯨魚做多",
                "SLIGHT_LONG": "鯨魚略偏多",
                "NEUTRAL": "鯨魚中立",
                "SLIGHT_SHORT": "鯨魚略偏空",
                "SHORT_HEAVY": "鯨魚做空",
                "WHALE_LONG": "頂級交易者做多",
                "WHALE_SHORT": "頂級交易者做空",
            }
            parts.append(whale_cn.get(whale, whale))

        funding = onchain.get("funding_rate")
        if funding is not None:
            if funding > 0.05:
                parts.append("資金費率偏高（多頭擁擠）")
            elif funding < -0.05:
                parts.append("資金費率偏低（空頭擁擠）")

        vol = onchain.get("volume_trend", "UNKNOWN")
        if vol != "UNKNOWN":
            vol_cn = {
                "SURGE_UP": "成交量暴增↑",
                "SURGE_DOWN": "成交量暴增↓",
                "ACTIVE": "成交活躍",
                "LOW": "成交低迷",
            }
            parts.append(vol_cn.get(vol, vol))

        pos_catalysts = sum(1 for c in catalysts if c["impact"] == "POSITIVE")
        neg_catalysts = sum(1 for c in catalysts if c["impact"] == "NEGATIVE")
        if pos_catalysts > neg_catalysts:
            parts.append(f"催化劑偏正面({pos_catalysts})")
        elif neg_catalysts > pos_catalysts:
            parts.append(f"催化劑偏負面({neg_catalysts})")

        return " | ".join(parts) if parts else "數據不足"

    def get_research_history(
        self, coin: Optional[str] = None, days: int = 7
    ) -> List[Dict]:
        """Load past research for a coin or all coins."""
        results = []
        for f in sorted(self._research_dir.glob("*.json"), reverse=True):
            data = _load_json(f)
            if data:
                if coin and data.get("coin") != coin.upper():
                    continue
                results.append(data)
        return results[: days * 5]  # max 5 per day

    def _llm_sentiment(self, text: str, max_length: int = 500) -> Optional[float]:
        """Use LLM (mimo-v2.5-pro primary, auto-fallback to DeepSeek) for deep sentiment analysis.

        Returns float -1 to +1, or None if all LLM providers unavailable.
        """

        # Truncate long text
        text = text[:max_length]

        try:
            from src.llm_client import get_second_opinion_client

            llm = get_second_opinion_client() or get_llm_client()
            result = llm.chat(
                messages=[
                    {
                        "role": "user",
                        "content": f"Analyze this crypto news sentiment:\n\n{text}",
                    }
                ],
                model="mimo-v2.5-pro",
                system_prompt="You are a crypto news sentiment analyzer. Return ONLY a single float number between -1.0 (extremely bearish) and 1.0 (extremely bullish). No explanation, just the number.",
                max_tokens=10,
                temperature=0.0,
            )

            if result is not None:
                content = result["content"].strip()
                score = float(content)
                return max(-1.0, min(1.0, score))
        except Exception:
            logger.error("LLM sentiment scoring failed", exc_info=True)

        return None
