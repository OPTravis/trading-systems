"""
Composite Ranker - Factor orthogonalization and IC-weighted ranking.
"""
import logging
from typing import Dict, List
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class CompositeRanker:
    """Composite ranking with Gram-Schmidt orthogonalization and IC weighting."""

    # Factor priority for orthogonalization (highest priority first)
    FACTOR_PRIORITY = ['momentum', 'value', 'quality', 'fundamental', 'technical', 'sentiment']

    def __init__(self, ic_tracker=None):
        self.ic_tracker = ic_tracker

    def rank_universe(self, universe: List[str], factor_scores: Dict[str, Dict[str, float]]) -> pd.DataFrame:
        """Rank universe using orthogonalized, IC-weighted composite scores.
        
        Args:
            universe: List of symbols to rank
            factor_scores: {symbol: {factor: score}} nested dict
            
        Returns:
            DataFrame with columns [symbol, composite_score, factors...] sorted descending
        """
        if not universe or not factor_scores:
            return pd.DataFrame(columns=['symbol', 'composite_score'])

        # Build factor matrix
        factors = self.FACTOR_PRIORITY
        data = []
        for symbol in universe:
            scores = factor_scores.get(symbol, {})
            row = {'symbol': symbol}
            for f in factors:
                row[f] = scores.get(f, 50.0)
            data.append(row)

        df = pd.DataFrame(data)
        if df.empty:
            return df

        # Gram-Schmidt orthogonalization
        factor_matrix = df[factors].values.astype(float)
        orthogonal = self._gram_schmidt(factor_matrix)

        # Get IC weights
        weights = self._get_ic_weights(factors)

        # Compute composite score
        weight_vec = np.array([weights.get(f, 1.0 / len(factors)) for f in factors])
        composite = orthogonal @ weight_vec

        # Normalize to 0-100
        if composite.std() > 0:
            composite = 50 + (composite - composite.mean()) / composite.std() * 15
        composite = np.clip(composite, 0, 100)

        df['composite_score'] = composite
        df = df.sort_values('composite_score', ascending=False).reset_index(drop=True)

        # Return top 20% (SPOT ONLY - long only)
        top_n = max(1, int(len(df) * 0.20))
        return df.head(top_n)

    def _gram_schmidt(self, matrix: np.ndarray) -> np.ndarray:
        """Gram-Schmidt orthogonalization of factor matrix."""
        n_cols = matrix.shape[1]
        orthogonal = np.zeros_like(matrix)

        for i in range(n_cols):
            v = matrix[:, i].copy()
            for j in range(i):
                proj = np.dot(orthogonal[:, j], matrix[:, i])
                denom = np.dot(orthogonal[:, j], orthogonal[:, j])
                if denom > 0:
                    v -= (proj / denom) * orthogonal[:, j]
            orthogonal[:, i] = v

        # Normalize columns
        for i in range(n_cols):
            norm = np.linalg.norm(orthogonal[:, i])
            if norm > 0:
                orthogonal[:, i] /= norm
            else:
                logger.warning(
                    "Gram-Schmidt produced zero-norm column at index %d — "
                    "collinear factors detected, factor '%s' is redundant",
                    i,
                    self.FACTOR_PRIORITY[i] if i < len(self.FACTOR_PRIORITY) else f"col_{i}",
                )

        return orthogonal

    def _get_ic_weights(self, factors: List[str]) -> Dict[str, float]:
        """Get IC-based factor weights."""
        if self.ic_tracker:
            weights = self.ic_tracker.get_weights()
            if weights:
                total = sum(weights.get(f, 0) for f in factors)
                if total > 0:
                    return {f: weights.get(f, 0) / total for f in factors}
        # Equal weights fallback
        n = len(factors)
        return {f: 1.0 / n for f in factors}
