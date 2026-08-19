"""
Kelly Criterion Position Sizing - Optimal bet sizing based on edge and risk.

The Kelly formula: f* = (bp - q) / b
  where b = odds (reward/risk), p = win probability, q = 1-p

In trading context:
  - p = estimated win rate from backtest or signal history
  - b = average win / average loss (reward-to-risk ratio)
  - f* = optimal fraction of bankroll to bet

We use HALF-KELLY for safety (reduces volatility by 50%,
only sacrifices 25% of expected growth).
"""

import logging
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Kelly safety factor
KELLY_FRACTION = 0.5  # Half-Kelly: safer, smoother equity curve
MAX_POSITION_PCT = (
    0.12  # Hard cap: never bet more than 12% of balance (winners avg 6.3% Kelly)
)
MIN_POSITION_PCT = 0.01  # Minimum position floor (1% — Kelly is a scale, not a gate)

# Exploration positions: when SurgeDetector signals IMMINENT/CONFIRMED but Kelly
# can't produce a reliable estimate (cold start), allow a tiny exploratory bet
# to bootstrap historical data. Think epsilon-greedy exploration budget.
EXPLORATION_MIN_PCT = 0.01   # 1% minimum exploration position
EXPLORATION_MAX_PCT = 0.02   # 2% maximum exploration position
BINANCE_MIN_NOTIONAL = 5.0   # Binance minNotional for most pairs (USDT)


class KellyPositionSizer:
    """Calculate optimal position size using Kelly Criterion."""

    def __init__(self, state_db=None):
        self.db = state_db
        self._cache: Dict[str, Dict] = {}
        self._cache_ts: float = 0
        self._cache_ttl = 300  # 5 minutes

    def _get_trade_history(
        self, symbol: Optional[str] = None, min_trades: int = 5
    ) -> List[Dict]:
        """Fetch recent trade history for win rate calculation."""
        if self.db:
            try:
                # Read from trade_outcomes (has actual PnL data)
                conn = self.db._get_conn()
                rows = conn.execute(
                    """SELECT symbol, net_pnl_pct, is_win, strategy
                       FROM trade_outcomes
                       WHERE status = 'closed' AND net_pnl_pct IS NOT NULL
                       ORDER BY entry_time DESC LIMIT ?""",
                    (100,),
                ).fetchall()
                trades = [
                    {"symbol": r[0], "pnl": r[1], "is_win": r[2], "strategy": r[3]}
                    for r in rows
                ]
                return trades
            except Exception as e:
                logger.warning(f"Failed to get trade history from DB: {e}")
        return []

    def calculate_kelly_fraction(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """Calculate Kelly fraction.

        Args:
            win_rate: Probability of winning (0-1)
            avg_win: Average winning trade return (positive)
            avg_loss: Average losing trade return (positive number, e.g., 0.05 = 5%)

        Returns:
            Optimal fraction of bankroll to allocate (0-1)
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return 0.0

        # b = reward/risk ratio
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p

        # Kelly formula: f* = (bp - q) / b
        kelly = (b * p - q) / b

        # Apply safety fraction
        kelly *= KELLY_FRACTION

        # Clamp to safe bounds
        kelly = max(0, min(kelly, MAX_POSITION_PCT))

        return kelly

    def get_position_size(
        self,
        symbol: str,
        balance: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        signal_score: float = 70,
        use_historical: bool = True,
        surge_alert_level: str = "SILENCE",
    ) -> Dict:
        """Calculate optimal position size for a trade.

        Kelly is a **scale** (how much to bet), not a **gate** (whether to bet).
        Whether to trade is decided by RegimeGuard upstream. Kelly only decides size.

        Args:
            symbol: Trading pair
            balance: Available USDT balance
            stop_loss_pct: Stop loss percentage (e.g., 5.0 for 5%)
            take_profit_pct: Take profit percentage (e.g., 10.0 for 10%)
            signal_score: Signal confidence score (0-100)
            use_historical: Whether to use historical trade data for win rate
            surge_alert_level: SurgeDetector alert level — when IMMINENT/CONFIRMED
                and Kelly has insufficient data, allows a small exploratory position

        Returns:
            {
                position_pct: float,
                win_rate: float,
                reward_risk: float,
                avg_win: float,
                avg_loss: float,
                confidence: str,
                reason: str,
                is_exploration: bool,  # True if this is a cold-start exploratory bet
            }
        """
        # Base reward-to-risk from this trade's SL/TP
        reward_risk = take_profit_pct / max(stop_loss_pct, 0.1)

        win_rate = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        confidence = "LOW"

        MIN_RELIABLE_TRADES = (
            30  # need this many for Kelly to be statistically reliable
        )

        if use_historical:
            trades = self._get_trade_history(symbol)
            if len(trades) >= 5:
                wins = [t["pnl"] for t in trades if t["pnl"] > 0]
                losses = [t["pnl"] for t in trades if t["pnl"] < 0]

                if wins and losses:
                    win_rate = len(wins) / len(trades)
                    avg_win = np.mean(wins) if wins else 0
                    avg_loss = abs(np.mean(losses)) if losses else 0
                    confidence = (
                        "HIGH" if len(trades) >= MIN_RELIABLE_TRADES else "MEDIUM"
                    )

        # Fallback: estimate win rate from signal score
        # Triggered when: no historical data, OR Kelly would be negative with insufficient sample
        if win_rate == 0:
            # Map score 60-100 to win rate 0.45-0.65
            win_rate = 0.35 + (signal_score / 100) * 0.30
            avg_win = take_profit_pct / 100
            avg_loss = stop_loss_pct / 100
            confidence = "LOW (estimated from score)"

        # Calculate Kelly fraction
        kelly = self.calculate_kelly_fraction(win_rate, avg_win, avg_loss)

        # If Kelly is zero or negative, decide whether to block or explore.
        #
        # Principle: Kelly is a scale, not a gate. RegimeGuard already decided
        # this trade is worth considering. Kelly's job is to size it.
        #
        # - HIGH confidence + Kelly ≤ 0  → genuinely bad edge, block (the only
        #   case where Kelly still acts as gate: we have enough data to know
        #   this specific coin/strategy loses money)
        # - Insufficient data + Surge IMMINENT/CONFIRMED → exploration position
        #   (epsilon-greedy: small bet to bootstrap historical data)
        # - Insufficient data + Surge SILENCE/WATCH/ACCUMULATE → block
        #   (correct conservatism when no surge signal supports the risk)
        is_exploration = False
        if kelly <= 0:
            if confidence == "HIGH":
                # Sufficient data and Kelly is negative — genuinely bad edge
                reason = (
                    f"Kelly={kelly:.1%} ≤ 0, 不建議交易 "
                    f"(win_rate={win_rate:.1%}, R/R={reward_risk:.1f})"
                )
                return {
                    "position_pct": 0.0,
                    "win_rate": round(win_rate, 4),
                    "reward_risk": round(reward_risk, 2),
                    "avg_win": round(avg_win, 4),
                    "avg_loss": round(avg_loss, 4),
                    "confidence": confidence,
                    "reason": reason,
                    "is_exploration": False,
                }
            elif surge_alert_level in ("IMMINENT", "CONFIRMED"):
                # Surge signals support the trade, but Kelly has insufficient data.
                # Allow a tiny exploratory position to bootstrap history.
                # Ensure we meet exchange minimum notional ($5 on Binance).
                kelly = max(
                    EXPLORATION_MIN_PCT,
                    BINANCE_MIN_NOTIONAL / balance if balance > 0 else EXPLORATION_MIN_PCT,
                )
                kelly = min(kelly, EXPLORATION_MAX_PCT)
                is_exploration = True
                confidence = (
                    f"EXPLORATION (cold start, surge={surge_alert_level}, "
                    f"bootstrapping history)"
                )
            else:
                # Kelly ≤ 0, no surge support — correct conservatism, block
                reason = (
                    f"Kelly={kelly:.1%} ≤ 0, no surge signal "
                    f"(surge={surge_alert_level}), 保守不交易"
                )
                return {
                    "position_pct": 0.0,
                    "win_rate": round(win_rate, 4),
                    "reward_risk": round(reward_risk, 2),
                    "avg_win": round(avg_win, 4),
                    "avg_loss": round(avg_loss, 4),
                    "confidence": "BLOCKED",
                    "reason": reason,
                    "is_exploration": False,
                }

        # Apply minimum floor (1%) only for normal (non-exploration) positions
        if not is_exploration and kelly < MIN_POSITION_PCT:
            kelly = MIN_POSITION_PCT
            reason = (
                f"Kelly={kelly:.1%} below floor, using min {MIN_POSITION_PCT:.0%}"
            )
        elif is_exploration:
            reason = (
                f"Exploration position {kelly:.1%} "
                f"(surge={surge_alert_level}, cold start)"
            )
        else:
            reason = (
                f"Kelly={kelly:.1%} (win_rate={win_rate:.1%}, R/R={reward_risk:.1f})"
            )

        return {
            "position_pct": round(kelly, 4),
            "win_rate": round(win_rate, 4),
            "reward_risk": round(reward_risk, 2),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "confidence": confidence,
            "reason": reason,
            "is_exploration": is_exploration,
        }

    def adjust_for_portfolio(
        self,
        kelly_result: Dict,
        current_positions: int,
        max_positions: int = 5,
    ) -> Dict:
        """Light adjustment for portfolio context. Main scaling is in trade_executor."""
        kelly = kelly_result["position_pct"]

        # Only apply a mild reduction — trade_executor already has per-position tier scaling
        if current_positions >= max_positions:
            scale = 0.0
        elif current_positions >= max_positions - 1:
            scale = 0.6
        else:
            scale = 1.0

        adjusted = kelly * scale
        kelly_result["position_pct"] = round(adjusted, 4)
        kelly_result["portfolio_scale"] = scale
        kelly_result[
            "reason"
        ] += f" | portfolio_scale={scale:.0%} ({current_positions}/{max_positions} positions)"

        return kelly_result
