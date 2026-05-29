"""
CVaR Risk Manager — Phase 8.

Conditional Value at Risk (CVaR) for portfolio-level risk management.

CVaR measures the expected loss in the worst α% of cases, providing
a more conservative risk measure than VaR (which only measures the
threshold, not the tail).

For SPOT ONLY: max loss = 100% of position, no leverage tail risk.

Key metrics:
- CVaR_95: expected loss in worst 5% of cases
- CVaR_99: expected loss in worst 1% of cases
- Portfolio VaR: maximum expected loss at confidence level
- Dynamic position sizing based on CVaR
"""

import json
import logging
import math
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Risk thresholds
CVAR_95_WARNING = -8.0    # Warn if CVaR_95 < -8%
CVAR_95_CRITICAL = -15.0  # Critical if CVaR_95 < -15%
MAX_PORTFOLIO_CVAR = -12.0  # Max allowed portfolio CVaR
POSITION_SCALE_LOW_RISK = 1.2   # Scale up when risk is low
POSITION_SCALE_HIGH_RISK = 0.5  # Scale down when risk is high


class CVaRRiskManager:
    """Portfolio-level CVaR risk management."""

    def __init__(self, db=None):
        if db is None:
            from ..core.state_db import get_state_db
            db = get_state_db()
        self._db = db

    def compute_cvar(self, returns: List[float], alpha: float = 0.05) -> float:
        """Compute CVaR (Expected Shortfall) from return series.

        CVaR_alpha = E[Loss | Loss > VaR_alpha]
        = average of the worst alpha% of returns

        Args:
            returns: list of period returns (e.g., daily %)
            alpha: confidence level (0.05 = 95% CVaR)

        Returns: CVaR as a negative percentage (e.g., -12.5)
        """
        if not returns or len(returns) < 10:
            return 0.0

        sorted_returns = sorted(returns)
        n = len(sorted_returns)
        cutoff_idx = max(1, int(n * alpha))

        # Average of the worst alpha% returns
        tail_returns = sorted_returns[:cutoff_idx]
        cvar = sum(tail_returns) / len(tail_returns)

        return round(cvar, 2)

    def compute_var(self, returns: List[float], alpha: float = 0.05) -> float:
        """Compute VaR (Value at Risk) from return series.

        VaR_alpha = the loss threshold that is exceeded with probability alpha.

        Returns: VaR as a negative percentage.
        """
        if not returns or len(returns) < 10:
            return 0.0

        sorted_returns = sorted(returns)
        n = len(sorted_returns)
        idx = max(0, int(n * alpha) - 1)

        return round(sorted_returns[idx], 2)

    def compute_portfolio_risk(self, positions: List[Dict]) -> Dict:
        """Compute portfolio-level risk metrics from current positions.

        Args:
            positions: list of position dicts with at least:
                - symbol, entry_price, current_price, quantity
                - historical_returns (optional, for CVaR)

        Returns:
            {
                "portfolio_cvar_95": float,
                "portfolio_cvar_99": float,
                "portfolio_var_95": float,
                "max_position_risk": float,
                "concentration_risk": float,
                "risk_level": "low" | "medium" | "high" | "critical",
                "position_scale": float,
                "recommendations": [str],
            }
        """
        if not positions:
            return {
                "portfolio_cvar_95": 0,
                "portfolio_cvar_99": 0,
                "portfolio_var_95": 0,
                "risk_level": "low",
                "position_scale": 1.0,
                "recommendations": [],
            }

        # Collect returns from trade outcomes
        conn = self._db._get_conn()
        rows = conn.execute(
            """SELECT net_pnl_pct FROM trade_outcomes
            WHERE status = 'closed' AND net_pnl_pct IS NOT NULL
            ORDER BY exit_time DESC LIMIT 100"""
        ).fetchall()

        returns = [r["net_pnl_pct"] for r in rows] if rows else []

        # If insufficient history, use position-level estimates
        if len(returns) < 10:
            returns = self._estimate_returns_from_positions(positions)

        # Compute CVaR
        cvar_95 = self.compute_cvar(returns, 0.05)
        cvar_99 = self.compute_cvar(returns, 0.01)
        var_95 = self.compute_var(returns, 0.05)

        # Concentration risk (% in largest position)
        total_value = sum(
            float(p.get("quantity", 0)) * float(p.get("current_price", 0))
            for p in positions
        )
        if total_value > 0:
            max_position_pct = max(
                float(p.get("quantity", 0)) * float(p.get("current_price", 0)) / total_value
                for p in positions
            ) * 100
        else:
            max_position_pct = 0

        # Determine risk level
        if cvar_95 < CVAR_95_CRITICAL:
            risk_level = "critical"
            position_scale = 0.3
        elif cvar_95 < CVAR_95_WARNING:
            risk_level = "high"
            position_scale = POSITION_SCALE_HIGH_RISK
        elif cvar_95 < -3.0:
            risk_level = "medium"
            position_scale = 0.8
        else:
            risk_level = "low"
            position_scale = POSITION_SCALE_LOW_RISK

        # Generate recommendations
        recommendations = []
        if max_position_pct > 40:
            recommendations.append(f"集中風險：最大倉位 {max_position_pct:.0f}% > 40%")
        if cvar_95 < CVAR_95_CRITICAL:
            recommendations.append(f"CVaR_95 = {cvar_95:.1f}% 觸及臨界值，建議減倉")
        if cvar_99 < -20:
            recommendations.append(f"CVaR_99 = {cvar_99:.1f}%，極端情況下可能虧損超 20%")

        return {
            "portfolio_cvar_95": cvar_95,
            "portfolio_cvar_99": cvar_99,
            "portfolio_var_95": var_95,
            "max_position_pct": round(max_position_pct, 1),
            "concentration_risk": round(max_position_pct, 1),
            "risk_level": risk_level,
            "position_scale": position_scale,
            "recommendations": recommendations,
            "n_samples": len(returns),
            "timestamp": time.time(),
        }

    def _estimate_returns_from_positions(self, positions: List[Dict]) -> List[float]:
        """Estimate return series from current position PnL."""
        returns = []
        for p in positions:
            entry = float(p.get("entry_price", 0))
            current = float(p.get("current_price", 0))
            if entry > 0:
                ret = (current - entry) / entry * 100
                returns.append(ret)
        return returns

    def get_dynamic_sl(self, base_sl_pct: float, cvar_95: float) -> float:
        """Adjust stop-loss based on CVaR.

        Higher CVaR (more risk) → tighter SL
        Lower CVaR (less risk) → wider SL (allow more room)
        """
        if cvar_95 < CVAR_95_CRITICAL:
            return base_sl_pct * 0.6  # Much tighter
        elif cvar_95 < CVAR_95_WARNING:
            return base_sl_pct * 0.8  # Tighter
        elif cvar_95 > -2.0:
            return base_sl_pct * 1.2  # Wider (low risk)
        else:
            return base_sl_pct  # Normal

    def format_report(self, risk: Dict) -> str:
        """Format risk report."""
        if not risk:
            return "無風險數據"

        LEVEL_NAMES = {
            "low": "🟢 低風險",
            "medium": "🟡 中風險",
            "high": "🟠 高風險",
            "critical": "🔴 臨界風險",
        }

        lines = [
            "## CVaR 風險報告",
            "",
            f"**風險等級**: {LEVEL_NAMES.get(risk.get('risk_level', ''), risk.get('risk_level', ''))}",
            f"**CVaR_95**: {risk.get('portfolio_cvar_95', 0):+.1f}%",
            f"**CVaR_99**: {risk.get('portfolio_cvar_99', 0):+.1f}%",
            f"**VaR_95**: {risk.get('portfolio_var_95', 0):+.1f}%",
            f"**最大倉位**: {risk.get('max_position_pct', 0):.0f}%",
            f"**倉位縮放**: {risk.get('position_scale', 1.0):.1f}x",
            f"**樣本數**: {risk.get('n_samples', 0)}",
        ]

        recs = risk.get("recommendations", [])
        if recs:
            lines.extend(["", "**建議**:"])
            for r in recs:
                lines.append(f"- {r}")

        return "\n".join(lines)
