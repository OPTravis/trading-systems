"""
Sentiment analysis using FinBERT (ProsusAI/finbert).
Provides single-text and batch sentiment scoring from -1 (bearish) to +1 (bullish).
"""
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class SentimentFeed:
    """
    Financial sentiment analysis using the FinBERT transformer model.
    Scores range from -1.0 (very negative) to +1.0 (very positive).
    """

    def __init__(self, model_name: str = "ProsusAI/finbert", device: Optional[str] = None):
        """
        Args:
            model_name: HuggingFace model identifier.
            device: 'cpu', 'cuda', or None (auto-detect).
        """
        self.model_name = model_name
        self.device = device
        self._pipeline = None

    def _load_pipeline(self):
        """Lazy-load the FinBERT pipeline to avoid startup overhead."""
        if self._pipeline is not None:
            return

        logger.info("Loading FinBERT model: %s", self.model_name)
        try:
            from transformers import pipeline as hf_pipeline

            self._pipeline = hf_pipeline(
                "sentiment-analysis",
                model=self.model_name,
                tokenizer=self.model_name,
                device=self.device,
                top_k=None,  # return all label scores
            )
            logger.info("FinBERT loaded successfully")
        except ImportError:
            logger.error("transformers package not installed – install with: pip install transformers torch")
            raise
        except Exception as exc:
            logger.error("Failed to load FinBERT: %s", exc)
            raise

    def _score_to_float(self, label: str, score: float) -> float:
        """Convert FinBERT label + score to a single float in [-1, 1]."""
        label_lower = label.lower()
        if label_lower == "positive":
            return score
        elif label_lower == "negative":
            return -score
        else:  # neutral
            return 0.0

    def analyze_sentiment(self, text: str) -> float:
        """
        Analyze sentiment of a single text string.

        Args:
            text: Financial text (headline, sentence, etc.)

        Returns:
            Float from -1.0 (very bearish) to +1.0 (very bullish).
        """
        if not text or not text.strip():
            return 0.0

        self._load_pipeline()

        try:
            # FinBERT handles up to 512 tokens; truncate to ~2000 chars
            # (~450 words, safely under 512 token limit)
            truncated = text[:2000]
            results = self._pipeline(truncated)

            # results is [[{label, score}, ...]] with top_k=None
            if results and isinstance(results[0], list):
                label_scores = {r["label"].lower(): r["score"] for r in results[0]}
            elif results and isinstance(results[0], dict):
                label_scores = {r["label"].lower(): r["score"] for r in results}
            else:
                return 0.0

            positive = label_scores.get("positive", 0.0)
            negative = label_scores.get("negative", 0.0)
            return float(positive - negative)

        except Exception as exc:
            logger.error("Sentiment analysis error: %s", exc)
            return 0.0

    def analyze_batch(self, texts: list[str]) -> list[float]:
        """
        Analyze sentiment for a batch of texts.

        Args:
            texts: List of text strings.

        Returns:
            List of sentiment floats in [-1, 1].
        """
        if not texts:
            return []

        self._load_pipeline()

        try:
            truncated = [t[:2000] for t in texts]
            results = self._pipeline(truncated, batch_size=16)

            scores = []
            for result in results:
                if isinstance(result, list):
                    label_scores = {r["label"].lower(): r["score"] for r in result}
                elif isinstance(result, dict):
                    label_scores = {result["label"].lower(): result["score"]}
                else:
                    scores.append(0.0)
                    continue

                positive = label_scores.get("positive", 0.0)
                negative = label_scores.get("negative", 0.0)
                scores.append(float(positive - negative))

            return scores

        except Exception as exc:
            logger.error("Batch sentiment analysis error: %s", exc)
            return [0.0] * len(texts)

    def get_sentiment_score(self, texts: list[str]) -> dict:
        """
        Get aggregate sentiment for a collection of texts.

        Returns:
            Dict with keys: mean, median, min, max, count.
        """
        scores = self.analyze_batch(texts)
        if not scores:
            return {"mean": 0.0, "median": 0.0, "min": 0.0, "max": 0.0, "count": 0}

        arr = np.array(scores)
        return {
            "mean": float(arr.mean()),
            "median": float(np.median(arr)),
            "min": float(arr.min()),
            "max": float(arr.max()),
            "count": len(scores),
        }
