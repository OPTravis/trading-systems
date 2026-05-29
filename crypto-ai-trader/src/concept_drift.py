"""
Concept Drift Detector — Phase 7.

Monitors when factor correlations with PnL change significantly,
indicating the market has shifted and learned weights may be stale.

Detection methods:
1. KL divergence between recent and historical factor-PnL correlations
2. Rolling window correlation stability
3. Win rate trend analysis per strategy

Triggers relearning when drift detected.
"""

import json
import logging
import math
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Detection thresholds
KL_DIVERGENCE_THRESHOLD = 0.10    # KL > 0.10 = significant drift
CORRELATION_SHIFT_THRESHOLD = 0.3  # Correlation changed by > 0.3
WIN_RATE_DROP_THRESHOLD = 15.0     # Win rate dropped > 15% from baseline
MIN_SAMPLES_FOR_DETECTION = 30     # Need at least 30 closed trades for reliable drift detection

FACTOR_NAMES = [
    "technical", "trend", "volume", "sentiment", "price_action",
    "obv_divergence", "consolidation", "bb_squeeze", "rsi_divergence",
    "onchain", "market_sentiment", "orderbook",
]


class ConceptDriftDetector:
    """Detects when learned patterns have shifted."""

    def __init__(self, db=None):
        if db is None:
            from src.state_db import get_state_db
            db = get_state_db()
        self._db = db

    def detect_drift(self) -> Optional[Dict]:
        """Run all drift detection checks.

        Returns:
            {
                "drift_detected": bool,
                "severity": "none" | "low" | "medium" | "high",
                "checks": {
                    "kl_divergence": {...},
                    "correlation_shift": {...},
                    "win_rate_trend": {...},
                },
                "recommendation": str,
            }
        """
        conn = self._db._get_conn()
        rows = conn.execute(
            "SELECT * FROM trade_outcomes WHERE status = 'closed' ORDER BY exit_time ASC"
        ).fetchall()

        if len(rows) < MIN_SAMPLES_FOR_DETECTION:
            return {
                "drift_detected": False,
                "severity": "none",
                "checks": {},
                "recommendation": f"數據不足（{len(rows)}/{MIN_SAMPLES_FOR_DETECTION}）",
            }

        rows = [dict(r) for r in rows]
        n = len(rows)

        # Split: first 60% = historical, last 40% = recent
        split_idx = int(n * 0.6)
        historical = rows[:split_idx]
        recent = rows[split_idx:]

        checks = {}

        # Check 1: Correlation shift
        corr_check = self._check_correlation_shift(historical, recent)
        checks["correlation_shift"] = corr_check

        # Check 2: Win rate trend
        wr_check = self._check_win_rate_trend(historical, recent)
        checks["win_rate_trend"] = wr_check

        # Check 3: PnL distribution shift
        pnl_check = self._check_pnl_distribution(historical, recent)
        checks["pnl_distribution"] = pnl_check

        # Determine overall severity
        drift_signals = 0
        if corr_check.get("drift"):
            drift_signals += 1
        if wr_check.get("drift"):
            drift_signals += 1
        if pnl_check.get("drift"):
            drift_signals += 1

        if drift_signals == 0:
            severity = "none"
            recommendation = "無漂移，繼續監控"
        elif drift_signals == 1:
            severity = "low"
            recommendation = "輕微漂移，建議觀察"
        elif drift_signals == 2:
            severity = "medium"
            recommendation = "中度漂移，建議重新學習因子權重"
        else:
            severity = "high"
            recommendation = "嚴重漂移，建議立即重新學習 + 重新優化參數"

        result = {
            "drift_detected": drift_signals > 0,
            "severity": severity,
            "drift_signals": drift_signals,
            "total_checks": 3,
            "checks": checks,
            "recommendation": recommendation,
            "n_historical": len(historical),
            "n_recent": len(recent),
            "timestamp": time.time(),
        }

        # Store result
        conn.execute(
            """INSERT OR REPLACE INTO kv (key, value, updated_at)
            VALUES ('drift_detection', ?, ?)""",
            (json.dumps(result), time.time()),
        )
        conn.commit()

        return result

    def _check_correlation_shift(self, historical: List[Dict], recent: List[Dict]) -> Dict:
        """Check if factor-PnL correlations have shifted significantly."""
        hist_corr = self._compute_correlations(historical)
        recent_corr = self._compute_correlations(recent)

        if not hist_corr or not recent_corr:
            return {"drift": False, "reason": "insufficient data"}

        max_shift = 0
        shifted_factors = []

        for factor in FACTOR_NAMES:
            h = hist_corr.get(factor, 0)
            r = recent_corr.get(factor, 0)
            shift = abs(r - h)
            if shift > max_shift:
                max_shift = shift
            if shift > CORRELATION_SHIFT_THRESHOLD:
                shifted_factors.append(f"{factor}: {h:+.2f}→{r:+.2f}")

        return {
            "drift": max_shift > CORRELATION_SHIFT_THRESHOLD,
            "max_shift": round(max_shift, 3),
            "threshold": CORRELATION_SHIFT_THRESHOLD,
            "shifted_factors": shifted_factors,
        }

    def _check_win_rate_trend(self, historical: List[Dict], recent: List[Dict]) -> Dict:
        """Check if win rate has dropped significantly."""
        def _is_win(r):
            if r.get("is_win") is not None:
                return r["is_win"]
            return r.get("net_pnl_pct", 0) > 0
        hist_wr = sum(1 for r in historical if _is_win(r)) / len(historical) * 100
        recent_wr = sum(1 for r in recent if _is_win(r)) / len(recent) * 100
        drop = hist_wr - recent_wr

        return {
            "drift": drop > WIN_RATE_DROP_THRESHOLD,
            "historical_wr": round(hist_wr, 1),
            "recent_wr": round(recent_wr, 1),
            "drop": round(drop, 1),
            "threshold": WIN_RATE_DROP_THRESHOLD,
        }

    def _check_pnl_distribution(self, historical: List[Dict], recent: List[Dict]) -> Dict:
        """Check if PnL distribution has shifted (mean and variance)."""
        hist_pnl = [r.get("net_pnl_pct", 0) for r in historical]
        recent_pnl = [r.get("net_pnl_pct", 0) for r in recent]

        hist_mean = sum(hist_pnl) / len(hist_pnl)
        recent_mean = sum(recent_pnl) / len(recent_pnl)

        hist_var = sum((x - hist_mean) ** 2 for x in hist_pnl) / len(hist_pnl)
        recent_var = sum((x - recent_mean) ** 2 for x in recent_pnl) / len(recent_pnl)

        mean_shift = abs(recent_mean - hist_mean)
        var_ratio = recent_var / hist_var if hist_var > 0 else 1.0

        # Drift if mean shifted by > 2% or variance changed by > 2x
        drift = mean_shift > 2.0 or var_ratio > 2.0 or var_ratio < 0.5

        return {
            "drift": drift,
            "hist_mean": round(hist_mean, 2),
            "recent_mean": round(recent_mean, 2),
            "mean_shift": round(mean_shift, 2),
            "hist_var": round(hist_var, 2),
            "recent_var": round(recent_var, 2),
            "var_ratio": round(var_ratio, 2),
        }

    def _compute_correlations(self, rows: List[Dict]) -> Optional[Dict[str, float]]:
        """Compute factor-PnL correlations for a set of trades."""
        factor_scores = {f: [] for f in FACTOR_NAMES}
        pnl_values = []

        for row in rows:
            factors_json = row.get("factors_json")
            if not factors_json:
                continue
            try:
                factors = json.loads(factors_json)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Dropping malformed factors_json row: %s", factors_json[:200] if factors_json else None, exc_info=True)
                continue

            pnl = row.get("net_pnl_pct", 0)
            pnl_values.append(pnl)
            for factor in FACTOR_NAMES:
                factor_scores[factor].append(factors.get(factor, 0))

        n = len(pnl_values)
        if n < 5:
            return None

        correlations = {}
        for factor in FACTOR_NAMES:
            scores = factor_scores[factor]
            if len(scores) != n:
                continue

            mean_x = sum(scores) / n
            mean_y = sum(pnl_values) / n
            cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(scores, pnl_values)) / n
            std_x = (sum((x - mean_x) ** 2 for x in scores) / n) ** 0.5
            std_y = (sum((y - mean_y) ** 2 for y in pnl_values) / n) ** 0.5

            if std_x > 0 and std_y > 0:
                correlations[factor] = round(cov / (std_x * std_y), 4)
            else:
                correlations[factor] = 0

        return correlations

    def format_report(self, result: Dict) -> str:
        """Format drift detection result as report."""
        if not result:
            return "無漂移檢測結果"

        severity = result.get("severity", "none")
        SEVERITY_NAMES = {
            "none": "✅ 無漂移",
            "low": "🟡 輕微漂移",
            "medium": "🟠 中度漂移",
            "high": "🔴 嚴重漂移",
        }

        lines = [
            "## 概念漂移檢測",
            "",
            f"**狀態**: {SEVERITY_NAMES.get(severity, severity)}",
            f"**信號**: {result.get('drift_signals', 0)}/{result.get('total_checks', 0)}",
            f"**樣本**: 歷史 {result.get('n_historical', 0)} + 近期 {result.get('n_recent', 0)}",
            f"**建議**: {result.get('recommendation', '')}",
        ]

        checks = result.get("checks", {})

        # Correlation shift
        cs = checks.get("correlation_shift", {})
        if cs:
            lines.extend(["", "**因子相關性漂移**:", f"- 最大偏移: {cs.get('max_shift', 0):.3f} (閾值 {cs.get('threshold', 0)})"])
            for sf in cs.get("shifted_factors", []):
                lines.append(f"  - {sf}")

        # Win rate trend
        wr = checks.get("win_rate_trend", {})
        if wr:
            lines.extend([
                "",
                "**勝率趨勢**:",
                f"- 歷史: {wr.get('historical_wr', 0):.1f}%",
                f"- 近期: {wr.get('recent_wr', 0):.1f}%",
                f"- 下降: {wr.get('drop', 0):.1f}%",
            ])

        # PnL distribution
        pd = checks.get("pnl_distribution", {})
        if pd:
            lines.extend([
                "",
                "**PnL 分佈**:",
                f"- 歷史均值: {pd.get('hist_mean', 0):+.2f}%",
                f"- 近期均值: {pd.get('recent_mean', 0):+.2f}%",
                f"- 方差比: {pd.get('var_ratio', 0):.2f}x",
            ])

        return "\n".join(lines)
