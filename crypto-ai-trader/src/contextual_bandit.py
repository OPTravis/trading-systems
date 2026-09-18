"""
Contextual Thompson Sampling Bandit for position sizing.

Lightweight CPU-only alternative to PPO/SAC RL. Uses Beta distribution
priors for Thompson Sampling across discrete (context, action) pairs.

Context features:
  - HMM regime: 4 states (bull/bear/sideways/volatile)
  - Fear & Greed index: 5 buckets
  - BTC trend: 3 states (bullish/neutral/bearish)
  - Portfolio heat: 3 levels (cold/warm/hot)
  = 4 * 5 * 3 * 3 = 180 context arms

Actions: 5 position size multipliers [0.3, 0.5, 0.8, 1.0, 1.2]

Total (context, action) pairs: 180 * 5 = 900
"""

import logging
from typing import Dict, List

import numpy as np

from src.state_db import get_state_db

logger = logging.getLogger(__name__)

STORAGE_KEY = "contextual_bandit:priors"

# Position size multipliers (actions)
ACTION_MULTIPLIERS = [0.3, 0.5, 0.8, 1.0, 1.2]

# Phase 2-A (2026-09-18): SL/TP tightness multipliers applied on top of the
# GARCH dynamic SL/TP. Small steps within +/-20%; hard absolute clamps live
# in strategy_adaptor (SL in [4%, 15%], TP1 in [6%, 25%]).
SLTP_MULTIPLIERS = [0.8, 0.9, 1.0, 1.1, 1.2]
SLTP_STORAGE_KEY = "contextual_bandit:sltp_priors"

# Cold start default
DEFAULT_SIZE = 0.8

# Context discretization mappings
HMM_REGIME_MAP = {
    "bull_trend": 0,
    "bear_trend": 1,
    "range_bound": 2,
    "high_vol": 3,
    "bull": 0,
    "bear": 1,
    "sideways": 2,
    "volatile": 3,
}

BTC_TREND_MAP = {
    "BULLISH": 0,
    "BULL": 0,
    "NEUTRAL": 1,
    "BEARISH": 2,
    "BEAR": 2,
}

PORTFOLIO_HEAT_MAP = {
    "cold": 0,
    "warm": 1,
    "hot": 2,
}


def _discretize_fear_greed(fng: float) -> int:
    """Map Fear & Greed index (0-100) to 5 buckets.

    0-20: extreme_fear (0)
    21-40: fear (1)
    41-60: neutral (2)
    61-80: greed (3)
    81-100: extreme_greed (4)
    """
    fng = max(0.0, min(100.0, float(fng)))
    return min(int(fng / 20), 4)


def _context_to_index(context: Dict) -> int:
    """Convert a context dict to a single integer index.

    Index = hmm * 150 + fng_bucket * 30 + btc_trend * 10 + heat_bucket * 5
    Then map to action slots (multiply by 5 for the action dimension).
    """
    hmm = HMM_REGIME_MAP.get(str(context.get("hmm_regime", "sideways")).lower(), 2)
    fng = _discretize_fear_greed(context.get("fear_greed", 50))
    btc = BTC_TREND_MAP.get(str(context.get("btc_trend", "NEUTRAL")).upper(), 1)
    heat = PORTFOLIO_HEAT_MAP.get(str(context.get("portfolio_heat", "cold")).lower(), 0)
    return hmm * 150 + fng * 30 + btc * 10 + heat * 5


def _action_index(multiplier: float) -> int:
    """Find closest action index for a given multiplier."""
    best_idx = 0
    best_dist = float("inf")
    for i, m in enumerate(ACTION_MULTIPLIERS):
        dist = abs(m - multiplier)
        if dist < best_dist:
            best_dist = dist
            best_idx = i
    return best_idx


class ContextualBandit:
    """Thompson Sampling bandit with Beta priors for position sizing."""

    def __init__(self, db=None):
        self._db = db or get_state_db()
        # priors[context_index] = list of [alpha, beta] per action
        # Flat structure: dict of int -> list of 5 [float, float]
        self._priors: Dict[int, List[List[float]]] = {}
        self._sltp_priors: Dict[int, List[List[float]]] = {}
        self._load()

    def _load(self):
        """Load priors from StateDB."""
        raw = self._db.kv_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            for ctx_str, actions in raw.items():
                ctx_idx = int(ctx_str)
                self._priors[ctx_idx] = [[float(a), float(b)] for a, b in actions]
        logger.info("ContextualBandit: loaded %d context priors", len(self._priors))
        raw_sltp = self._db.kv_get(SLTP_STORAGE_KEY)
        if raw_sltp and isinstance(raw_sltp, dict):
            for ctx_str, groups in raw_sltp.items():
                try:
                    self._sltp_priors[int(ctx_str)] = [
                        [[float(a), float(b)] for a, b in grp] for grp in groups]
                except (TypeError, ValueError):
                    continue

    def _save(self):
        """Persist priors to StateDB."""
        serializable = {}
        for ctx_idx, actions in self._priors.items():
            serializable[str(ctx_idx)] = [[a, b] for a, b in actions]
        self._db.kv_set(STORAGE_KEY, serializable)

    def _get_priors(self, context_index: int) -> List[List[float]]:
        """Get or initialize Beta priors for a context (5 actions)."""
        if context_index not in self._priors:
            self._priors[context_index] = [[1.0, 1.0] for _ in ACTION_MULTIPLIERS]
        return self._priors[context_index]

    def _beta_sample(self, alpha: float, beta_param: float) -> float:
        """Sample from Beta distribution using numpy."""
        return float(np.random.beta(alpha, beta_param))

    def recommend_size(self, context: Dict) -> float:
        """Recommend position size multiplier for given context.

        Uses Thompson Sampling: samples from each action's Beta posterior
        and returns the multiplier with the highest sample.

        Falls back to 0.8 on cold start (no priors for this context).
        """
        ctx_idx = _context_to_index(context)

        # Cold start: if no priors exist for this context, return conservative default
        if ctx_idx not in self._priors:
            return DEFAULT_SIZE

        priors = self._priors[ctx_idx]

        # Thompson Sampling: sample from each action's Beta distribution
        samples = [self._beta_sample(a, b) for a, b in priors]
        best_action_idx = int(np.argmax(samples))

        return ACTION_MULTIPLIERS[best_action_idx]

    def update_from_outcome(self, context: Dict, action_taken: float, pnl_pct: float):
        """Update priors after a trade closes.

        Args:
            context: The context dict at time of trade
            action_taken: The position size multiplier used
            pnl_pct: Percentage PnL of the trade
        """
        ctx_idx = _context_to_index(context)
        priors = self._get_priors(ctx_idx)
        act_idx = _action_index(action_taken)

        if pnl_pct > 0:
            increment = max(0.1, min(3.0, abs(pnl_pct) / 2.0))
            priors[act_idx][0] += increment  # alpha += scaled by PnL magnitude
        else:
            increment = max(0.1, min(3.0, abs(pnl_pct) / 2.0))
            priors[act_idx][1] += increment  # beta += scaled by PnL magnitude

        self._save()

    # ------------------------------------------------- SL/TP tightness arms
    # _sltp_priors[ctx] = [sl_arms, tp_arms] — two INDEPENDENT arm groups,
    # each [alpha, beta] per SLTP_MULTIPLIERS entry.
    def _get_sltp_priors(self, context_index: int) -> List[List[List[float]]]:
        """Get or initialize the (sl, tp) Beta arm groups for a context."""
        if context_index not in self._sltp_priors:
            self._sltp_priors[context_index] = [
                [[1.0, 1.0] for _ in SLTP_MULTIPLIERS],   # SL arm group
                [[1.0, 1.0] for _ in SLTP_MULTIPLIERS],   # TP arm group
            ]
        return self._sltp_priors[context_index]

    @staticmethod
    def _nearest_sltp_idx(mult: float) -> int:
        best_idx, best_dist = 0, float("inf")
        for i, m in enumerate(SLTP_MULTIPLIERS):
            dist = abs(m - float(mult))
            if dist < best_dist:
                best_dist, best_idx = dist, i
        return best_idx

    def recommend_sltp(self, context: Dict) -> tuple:
        """Thompson-sample (sl_mult, tp_mult) independently per arm group.

        Cold start (no priors for this context) returns (1.0, 1.0).
        """
        try:
            ctx_idx = _context_to_index(context)
            if ctx_idx not in self._sltp_priors:
                return (1.0, 1.0)
            sl_arms, tp_arms = self._sltp_priors[ctx_idx]
            sl_best = max(range(len(sl_arms)),
                          key=lambda i: self._beta_sample(*sl_arms[i]))
            tp_best = max(range(len(tp_arms)),
                          key=lambda i: self._beta_sample(*tp_arms[i]))
            return (SLTP_MULTIPLIERS[sl_best], SLTP_MULTIPLIERS[tp_best])
        except Exception:
            return (1.0, 1.0)

    def update_sltp(self, context: Dict, sl_mult: float, tp_mult: float,
                    pnl_pct: float):
        """Update SL and TP arm posteriors from a closed trade's PnL.

        Same reward semantics as update_from_outcome: increment scaled by
        PnL magnitude, clamped to [0.1, 3.0]; win -> alpha, loss -> beta.
        """
        try:
            ctx_idx = _context_to_index(context)
            sl_arms, tp_arms = self._get_sltp_priors(ctx_idx)
            increment = max(0.1, min(3.0, abs(pnl_pct) / 2.0))
            for arms, mult in ((sl_arms, sl_mult), (tp_arms, tp_mult)):
                idx = self._nearest_sltp_idx(mult)
                if pnl_pct > 0:
                    arms[idx][0] += increment
                else:
                    arms[idx][1] += increment
            serializable = {
                str(k): [[[a, b] for a, b in grp] for grp in v]
                for k, v in self._sltp_priors.items()}
            self._db.kv_set(SLTP_STORAGE_KEY, serializable)
        except Exception:
            logger.debug("ContextualBandit.update_sltp failed (non-critical)",
                         exc_info=True)

    def get_stats(self) -> Dict:
        """Return summary statistics of all priors.

        Returns:
            Dict with:
              - total_contexts: number of context arms with data
              - total_updates: total number of updates across all arms
              - contexts: per-context summary
              - actions: per-action aggregate stats
        """
        total_contexts = len(self._priors)
        total_updates = 0
        action_totals = [
            [0.0, 0.0, 0] for _ in ACTION_MULTIPLIERS
        ]  # [sum_alpha, sum_beta, count]

        contexts_summary = {}
        for ctx_idx, actions in self._priors.items():
            ctx_updates = sum((a + b - 2) for a, b in actions)
            total_updates += int(ctx_updates)
            best_action = max(
                range(len(actions)),
                key=lambda i: actions[i][0] / max(actions[i][0] + actions[i][1], 1e-10),
            )
            contexts_summary[str(ctx_idx)] = {
                "priors": [[a, b] for a, b in actions],
                "best_action_index": best_action,
                "best_multiplier": ACTION_MULTIPLIERS[best_action],
                "total_updates": ctx_updates,
            }
            for i, (a, b) in enumerate(actions):
                action_totals[i][0] += a
                action_totals[i][1] += b
                action_totals[i][2] += 1

        actions_summary = {}
        for i, (sum_a, sum_b, cnt) in enumerate(action_totals):
            if cnt > 0:
                win_rate = sum_a / (sum_a + sum_b)
            else:
                win_rate = 0.5
            actions_summary[str(ACTION_MULTIPLIERS[i])] = {
                "mean_alpha": sum_a / max(cnt, 1),
                "mean_beta": sum_b / max(cnt, 1),
                "aggregate_win_rate": round(win_rate, 4),
                "contexts_used": cnt,
            }

        return {
            "total_contexts": total_contexts,
            "total_updates": total_updates,
            "contexts": contexts_summary,
            "actions": actions_summary,
        }


# Singleton instance
_bandit_instance = None


def get_contextual_bandit() -> ContextualBandit:
    """Get or create the global ContextualBandit instance."""
    global _bandit_instance
    if _bandit_instance is None:
        _bandit_instance = ContextualBandit()
    return _bandit_instance
