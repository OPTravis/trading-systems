"""
Freqtrade Risk Management Patterns - Extracted for Integration
==============================================================

Key patterns extracted from freqtrade (https://github.com/freqtrade/freqtrade)
for integration into the crypto-ai-trader risk_manager.py.

Sources analyzed:
  - freqtrade/wallets.py         → Position sizing & stake calculation
  - freqtrade/persistence/trade_model.py → Stop-loss adjustment logic
  - freqtrade/strategy/interface.py → Trailing stoploss & dynamic stoploss
  - freqtrade/plugins/protections/  → Cooldown, drawdown, stoploss guards
  - freqtrade/freqtradebot.py       → Max open trades, trade creation flow

Each section below is a self-contained, well-commented Python function
that can be adapted into the existing RiskManager class.
"""

import logging
import time
from datetime import datetime, timedelta, UTC
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)


# =============================================================================
# PATTERN 1: Position Sizing Based on Risk-Per-Trade
# Source: freqtrade/wallets.py — get_trade_stake_amount, _calculate_unlimited_stake_amount
#
# Freqtrade's approach: Instead of a flat max_position_pct, calculate position
# size so that IF the stop-loss is hit, you only lose X% of your portfolio.
# Formula: stake_amount = (risk_per_trade * portfolio) / stoploss_pct
#
# This is the gold standard for risk-based position sizing.
# =============================================================================

def calculate_risk_per_trade_position_size(
    portfolio_value: float,
    risk_pct: float,           # e.g., 0.02 = 2% max loss per trade
    entry_price: float,
    stoploss_price: float,     # absolute price where SL is set
    max_position_pct: float = 0.15,  # hard cap: never risk more than 15% on one trade
    min_stake: float = 10.0,   # minimum trade size in USDT
) -> float:
    """
    Calculate position size so that if stop-loss is hit, you only lose
    `risk_pct` of your portfolio.

    Freqtrade equivalent:
        stake_amount = (available_balance * risk_pct) / abs(stoploss_pct)

    Args:
        portfolio_value: Total portfolio value in USDT
        risk_pct: Maximum fraction of portfolio to risk per trade (e.g., 0.02 = 2%)
        entry_price: Expected entry price
        stoploss_price: Absolute stop-loss price
        max_position_pct: Hard cap on position size as fraction of portfolio
        min_stake: Minimum trade size in USDT

    Returns:
        Position size in USDT

    Example:
        Portfolio = $10,000, risk = 2%, entry = $100, stoploss = $95
        Risk amount = $10,000 * 0.02 = $200
        Stop distance = ($100 - $95) / $100 = 5%
        Position size = $200 / 0.05 = $4,000 (40% of portfolio)
        Capped at max_position_pct = 15% → $1,500
    """
    if entry_price <= 0 or stoploss_price <= 0 or entry_price == stoploss_price:
        logger.warning("Invalid entry/stoploss prices, returning min_stake")
        return min_stake

    # Calculate the percentage distance to stop-loss
    stoploss_pct = abs(entry_price - stoploss_price) / entry_price

    if stoploss_pct <= 0:
        logger.warning("Stoploss distance is zero, returning min_stake")
        return min_stake

    # Core formula: risk_amount / stoploss_distance
    risk_amount = portfolio_value * risk_pct
    stake_amount = risk_amount / stoploss_pct

    # Hard cap: never exceed max_position_pct of portfolio
    max_allowed = portfolio_value * max_position_pct
    stake_amount = min(stake_amount, max_allowed)

    # Floor: respect minimum trade size
    stake_amount = max(stake_amount, min_stake)

    logger.info(
        f"Risk-based sizing: portfolio=${portfolio_value:.0f} risk={risk_pct:.1%} "
        f"SL_dist={stoploss_pct:.2%} → stake=${stake_amount:.2f} "
        f"(capped at ${max_allowed:.0f})"
    )
    return stake_amount


def calculate_unlimited_stake_amount(
    available_amount: float,
    tied_up_amount: float,
    max_open_trades: int,
) -> float:
    """
    Freqtrade's "unlimited stake amount" calculation from wallets.py.
    When stake_amount is set to 'unlimited', freqtrade divides available
    capital equally among max_open_trades slots.

    Source: wallets.py _calculate_unlimited_stake_amount()

    Args:
        available_amount: Currently available (free) capital
        tied_up_amount: Capital currently locked in open trades
        max_open_trades: Maximum number of concurrent trades

    Returns:
        Suggested stake amount per trade
    """
    if max_open_trades <= 0:
        return 0

    # Distribute total capital (free + tied) equally across slots
    possible_stake = (available_amount + tied_up_amount) / max_open_trades
    # Never exceed what's actually available
    return min(possible_stake, available_amount)


def get_available_stake_amount(
    free_balance: float,
    tied_up_stakes: float,
    tradable_balance_ratio: float = 0.99,
) -> float:
    """
    Freqtrade's available stake calculation from wallets.py.
    Ensures a small buffer is always kept (tradable_balance_ratio).

    Source: wallets.py get_total_stake_amount() + get_available_stake_amount()

    Args:
        free_balance: Free (unused) balance in stake currency
        tied_up_stakes: Total stake amount in open trades
        tradable_balance_ratio: Fraction of total balance that's tradable (e.g., 0.99)

    Returns:
        Available amount for new trades
    """
    # Total usable = (tied + free) * ratio
    total_usable = (tied_up_stakes + free_balance) * tradable_balance_ratio
    # Available = total usable minus what's already tied up
    available = total_usable - tied_up_stakes
    return min(available, free_balance)


# =============================================================================
# PATTERN 2: Trailing Stop-Loss with Profit Thresholds
# Source: strategy/interface.py — ft_stoploss_adjust(), trade_model.py — adjust_stop_loss()
#
# Freqtrade's trailing stoploss has these key concepts:
#   1. trailing_stop_positive: the stop distance when in profit (e.g., -0.02 = 2% below high)
#   2. trailing_stop_positive_offset: profit threshold to switch to positive trailing
#   3. trailing_only_offset_is_reached: only trail AFTER offset is reached
#   4. Stop losses ONLY walk up (for longs), never down — this is critical
# =============================================================================

class FreqtradeStyleTrailingStop:
    """
    A trailing stop-loss implementation inspired by freqtrade's strategy/interface.py
    and persistence/trade_model.py.

    Key principles from freqtrade:
    1. Initial stoploss is set at entry and stored as initial_stop_loss
    2. Stoploss only moves UP for longs (never down) — "ratcheting"
    3. When profit exceeds trailing_stop_positive_offset, switch to tighter trailing
    4. is_stop_loss_trailing flag tracks whether stop has moved from initial
    """

    def __init__(
        self,
        initial_stoploss_pct: float = -0.05,    # -5% initial stop
        trailing_stop_positive: float = -0.02,    # -2% trailing when in profit
        trailing_stop_positive_offset: float = 0.03,  # trigger at +3% profit
        trailing_only_offset_is_reached: bool = True,
    ):
        """
        Args:
            initial_stoploss_pct: Initial stop loss as ratio (e.g., -0.05 = -5%)
            trailing_stop_positive: Stop distance when in profit (e.g., -0.02 = -2%)
            trailing_stop_positive_offset: Profit threshold to activate positive trailing
            trailing_only_offset_is_reached: If True, only trail after offset is reached
        """
        self.initial_stoploss_pct = initial_stoploss_pct
        self.trailing_stop_positive = trailing_stop_positive
        self.trailing_stop_positive_offset = trailing_stop_positive_offset
        self.trailing_only_offset_is_reached = trailing_only_offset_is_reached

    def calculate_stoploss(
        self,
        entry_price: float,
        current_price: float,
        highest_price: float,
        current_stoploss: float,
        initial_stoploss: float,
        is_short: bool = False,
    ) -> Dict[str, Any]:
        """
        Calculate the new stop-loss value based on freqtrade's trailing logic.

        Adapted from:
          - strategy/interface.py ft_stoploss_adjust() lines 1516-1588
          - persistence/trade_model.py adjust_stop_loss() lines 814-883

        Args:
            entry_price: Trade entry price
            current_price: Current market price
            highest_price: Highest price since entry (for longs)
            current_stoploss: Current stop-loss price
            initial_stoploss: Original stop-loss price at entry
            is_short: Whether this is a short trade

        Returns:
            Dict with new_stoploss, is_trailing, reason
        """
        # Calculate current profit ratio (freqtrade style)
        if is_short:
            current_profit = (entry_price - current_price) / entry_price
        else:
            current_profit = (current_price - entry_price) / entry_price

        # Start with initial stoploss
        new_stoploss_pct = self.initial_stoploss_pct

        # --- Freqtrade's trailing logic (from ft_stoploss_adjust) ---

        # Determine if we should apply trailing
        should_trail = True
        if self.trailing_only_offset_is_reached and current_profit < self.trailing_stop_positive_offset:
            should_trail = False

        if should_trail:
            # If profit exceeds offset, use the tighter positive trailing stop
            if (self.trailing_stop_positive is not None and
                    current_profit > self.trailing_stop_positive_offset):
                new_stoploss_pct = self.trailing_stop_positive

        # Calculate absolute stop price
        leverage = 1.0  # adjust for leveraged trades
        if is_short:
            new_stoploss_price = current_price * (1 + abs(new_stoploss_pct / leverage))
        else:
            new_stoploss_price = current_price * (1 - abs(new_stoploss_pct / leverage))

        # --- CRITICAL: Stop losses only walk up (longs) or down (shorts) ---
        # This is the ratchet mechanism from trade_model.py adjust_stop_loss()
        is_trailing = False
        reason = "unchanged"

        if is_short:
            # For shorts: stop only moves DOWN (lower = better protection)
            if new_stoploss_price < current_stoploss or current_stoploss == 0:
                reason = "trailing_short_ratchet"
                is_trailing = True
        else:
            # For longs: stop only moves UP (higher = better protection)
            if new_stoploss_price > current_stoploss or current_stoploss == 0:
                reason = "trailing_long_ratchet"
                is_trailing = True

        # Mark as trailing if it moved from initial
        if current_stoploss != initial_stoploss and is_trailing:
            reason = "trailing_stop_active"

        final_stoploss = new_stoploss_price if is_trailing else current_stoploss

        return {
            "new_stoploss": final_stoploss,
            "new_stoploss_pct": new_stoploss_pct,
            "is_trailing": is_trailing or (current_stoploss != initial_stoploss),
            "current_profit_pct": round(current_profit * 100, 2),
            "reason": reason,
        }


# =============================================================================
# PATTERN 3: Dynamic Breakeven Stop-Loss
# Source: strategy/interface.py custom_stoploss() + ft_stoploss_adjust()
#
# A common freqtrade strategy pattern: when profit reaches a threshold,
# move stop-loss to breakeven (entry price). This is done via the
# custom_stoploss callback in strategies.
# =============================================================================

def dynamic_breakeven_stoploss(
    entry_price: float,
    current_price: float,
    current_stoploss: float,
    initial_stoploss: float,
    breakeven_offset_pct: float = 0.005,  # 0.5% above entry for fees
    is_short: bool = False,
) -> Dict[str, Any]:
    """
    Move stop-loss to breakeven when profit reaches a threshold.

    This is the most common custom_stoploss implementation in freqtrade
    strategies. Adapted from the pattern in strategy/interface.py.

    Args:
        entry_price: Trade entry price
        current_price: Current market price
        current_stoploss: Current stop-loss price
        initial_stoploss: Original stop-loss at entry
        breakeven_offset_pct: Offset above entry to cover fees (e.g., 0.5%)
        is_short: Whether this is a short trade

    Returns:
        Dict with suggested_stoploss, moved_to_breakeven, reason
    """
    if is_short:
        current_profit = (entry_price - current_price) / entry_price
    else:
        current_profit = (current_price - entry_price) / entry_price

    # Default: keep current stoploss
    result = {
        "suggested_stoploss": current_stoploss,
        "moved_to_breakeven": False,
        "current_profit_pct": round(current_profit * 100, 2),
        "reason": "below_threshold",
    }

    # Move to breakeven when profit >= 2% (common freqtrade pattern)
    if current_profit >= 0.02:
        if is_short:
            breakeven_price = entry_price * (1 - breakeven_offset_pct)
            # For shorts, breakeven stop should be ABOVE entry (higher = tighter)
            if breakeven_price > current_stoploss or current_stoploss == 0:
                result["suggested_stoploss"] = breakeven_price
                result["moved_to_breakeven"] = True
                result["reason"] = f"breakeven_at_{breakeven_offset_pct:.1%}_above_entry"
        else:
            breakeven_price = entry_price * (1 + breakeven_offset_pct)
            # For longs, breakeven stop should be BELOW entry (higher = better, ratchet up)
            if breakeven_price > current_stoploss or current_stoploss == 0:
                result["suggested_stoploss"] = breakeven_price
                result["moved_to_breakeven"] = True
                result["reason"] = f"breakeven_at_{breakeven_offset_pct:.1%}_above_entry"

    # Move to take-profit bracket when profit >= 5%
    if current_profit >= 0.05:
        if is_short:
            tp_stop = entry_price * (1 - 0.03)  # lock in 3% profit
            if tp_stop < result["suggested_stoploss"]:
                result["suggested_stoploss"] = tp_stop
                result["reason"] = "locked_3pct_profit"
        else:
            tp_stop = entry_price * (1 + 0.03)  # lock in 3% profit
            if tp_stop > result["suggested_stoploss"]:
                result["suggested_stoploss"] = tp_stop
                result["reason"] = "locked_3pct_profit"

    return result


# =============================================================================
# PATTERN 4: Max Open Trades with Free Slot Check
# Source: freqtradebot.py — get_free_open_trades()
#
# Simple but effective: count open trades and compare against max_open_trades.
# Freqtrade also extends the whitelist with pairs of open trades to ensure
# candle data is always downloaded for open positions.
# =============================================================================

def get_free_open_trade_slots(
    open_trade_count: int,
    max_open_trades: int,
) -> int:
    """
    Calculate how many more trades can be opened.

    Source: freqtradebot.py get_free_open_trades()

    Args:
        open_trade_count: Current number of open trades
        max_open_trades: Maximum allowed concurrent trades

    Returns:
        Number of free slots (0 if at max)
    """
    return max(0, max_open_trades - open_trade_count)


def should_allow_new_trade(
    open_trade_count: int,
    max_open_trades: int,
    current_pair: str,
    open_trade_pairs: List[str],
    correlation_check: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Comprehensive check before opening a new trade.

    Combines freqtrade's max_open_trades check with correlation awareness.

    Args:
        open_trade_count: Current number of open trades
        max_open_trades: Maximum allowed
        current_pair: Pair we want to trade
        open_trade_pairs: List of pairs already in trades
        correlation_check: Optional correlation check result

    Returns:
        Dict with allowed, reason, free_slots
    """
    free_slots = get_free_open_trade_slots(open_trade_count, max_open_trades)

    if free_slots <= 0:
        return {
            "allowed": False,
            "reason": f"Max open trades reached ({open_trade_count}/{max_open_trades})",
            "free_slots": 0,
        }

    # Check if already trading this pair (freqtrade doesn't double-trade same pair)
    if current_pair in open_trade_pairs:
        return {
            "allowed": False,
            "reason": f"Already trading {current_pair}",
            "free_slots": free_slots,
        }

    # Optional correlation check
    if correlation_check and not correlation_check.get("allowed", True):
        return {
            "allowed": False,
            "reason": f"Correlation: {correlation_check.get('reason', 'unknown')}",
            "free_slots": free_slots,
        }

    return {
        "allowed": True,
        "reason": f"OK ({free_slots} slots available)",
        "free_slots": free_slots,
    }


# =============================================================================
# PATTERN 5: Cooldown After Loss (Per-Pair)
# Source: plugins/protections/cooldown_period.py
#
# After ANY trade closes on a pair, lock that pair for a configurable
# cooldown period. This prevents rapid re-entry on a losing pair.
# Freqtrade stores locks in a PairLocks table with expiry timestamps.
# =============================================================================

class CooldownAfterLoss:
    """
    Per-pair cooldown after trade closure.

    Adapted from freqtrade's CooldownPeriod protection:
    - plugins/protections/cooldown_period.py
    - plugins/protections/iprotection.py (calculate_lock_end)

    Freqtrade locks the pair until: trade_close_time + stop_duration
    """

    def __init__(self, cooldown_minutes: int = 60):
        """
        Args:
            cooldown_minutes: How long to lock a pair after any trade closes
        """
        self.cooldown_minutes = cooldown_minutes
        # {pair: unlock_timestamp}
        self._locks: Dict[str, float] = {}

    def record_trade_close(self, pair: str, close_time: Optional[float] = None) -> None:
        """
        Lock a pair after a trade closes.

        Source: cooldown_period.py _cooldown_period() + calculate_lock_end()
        """
        close_ts = close_time or time.time()
        unlock_at = close_ts + (self.cooldown_minutes * 60)
        self._locks[pair] = unlock_at
        logger.info(
            f"CooldownAfterLoss: locked {pair} until "
            f"{datetime.fromtimestamp(unlock_at).strftime('%H:%M:%S')} "
            f"({self.cooldown_minutes}min cooldown)"
        )

    def is_pair_locked(self, pair: str) -> bool:
        """Check if a pair is currently in cooldown."""
        unlock_at = self._locks.get(pair, 0)
        if time.time() < unlock_at:
            remaining = (unlock_at - time.time()) / 60
            logger.debug(f"CooldownAfterLoss: {pair} locked for {remaining:.1f} more min")
            return True
        # Clean up expired locks
        if pair in self._locks:
            del self._locks[pair]
        return False

    def get_lock_until(self, pair: str) -> Optional[float]:
        """Get the unlock timestamp for a pair."""
        unlock_at = self._locks.get(pair, 0)
        if time.time() < unlock_at:
            return unlock_at
        return None

    def get_all_locks(self) -> Dict[str, float]:
        """Return all active locks."""
        now = time.time()
        return {p: t for p, t in self._locks.items() if t > now}


# =============================================================================
# PATTERN 6: Stoploss Guard (Daily Loss Limit)
# Source: plugins/protections/stoploss_guard.py
#
# If too many stoplosses hit within a lookback period, stop trading.
# This is freqtrade's equivalent of a "daily loss limit" but based on
# stoploss hit count rather than absolute loss amount.
# =============================================================================

class StoplossGuard:
    """
    Stop trading if too many stoplosses trigger within a time window.

    Source: plugins/protections/stoploss_guard.py

    freqtrade counts trades that closed via STOP_LOSS, TRAILING_STOP_LOSS,
    STOPLOSS_ON_EXCHANGE, or LIQUIDATION exit types within the lookback period.
    If count >= trade_limit, locks all pairs.
    """

    def __init__(
        self,
        trade_limit: int = 3,              # max stoploss hits before guard triggers
        lookback_period_minutes: int = 60,  # time window to check
        stop_duration_minutes: int = 240,   # how long to pause trading
        required_profit: float = 0.0,       # only count trades with profit < this
        only_per_pair: bool = False,        # if True, only lock the offending pair
    ):
        self.trade_limit = trade_limit
        self.lookback_minutes = lookback_period_minutes
        self.stop_duration_minutes = stop_duration_minutes
        self.required_profit = required_profit
        self.only_per_pair = only_per_pair
        # List of (timestamp, pair, profit) for closed trades
        self._trade_log: List[Dict] = []
        # Global lock expiry
        self._global_lock_until: float = 0

    def record_stoploss_hit(self, pair: str, profit_pct: float, timestamp: Optional[float] = None) -> None:
        """Record a stoploss-triggered trade closure."""
        ts = timestamp or time.time()
        self._trade_log.append({
            "time": ts,
            "pair": pair,
            "profit_pct": profit_pct,
        })
        # Trim old entries
        cutoff = ts - (self.lookback_minutes * 60 * 2)  # keep 2x lookback
        self._trade_log = [t for t in self._trade_log if t["time"] > cutoff]

    def check(self, now: Optional[float] = None) -> Dict[str, Any]:
        """
        Check if the stoploss guard should trigger.

        Source: stoploss_guard.py _stoploss_guard()

        Returns:
            Dict with triggered, locked_pairs, reason
        """
        now = now or time.time()
        lookback_until = now - (self.lookback_minutes * 60)

        # Count recent stoploss hits
        recent_stops = [
            t for t in self._trade_log
            if t["time"] >= lookback_until and t["profit_pct"] < self.required_profit
        ]

        if len(recent_stops) < self.trade_limit:
            return {"triggered": False, "count": len(recent_stops), "limit": self.trade_limit}

        # Guard triggered!
        if self.only_per_pair:
            # Only lock the pair with most stoplosses
            from collections import Counter
            pair_counts = Counter(t["pair"] for t in recent_stops)
            worst_pair = pair_counts.most_common(1)[0][0]
            locked = [worst_pair]
            reason = f"{len(recent_stops)} stoplosses on {worst_pair} in {self.lookback_minutes}min"
        else:
            locked = ["*"]  # global lock
            reason = f"{len(recent_stops)} stoplosses in {self.lookback_minutes}min (limit={self.trade_limit})"

        lock_until = now + (self.stop_duration_minutes * 60)

        logger.warning(f"StoplossGuard: {reason} → pausing until {datetime.fromtimestamp(lock_until).strftime('%H:%M')}")

        return {
            "triggered": True,
            "locked_pairs": locked,
            "until": lock_until,
            "reason": reason,
            "count": len(recent_stops),
        }


# =============================================================================
# PATTERN 7: Max Drawdown Protection (Equity-Based)
# Source: plugins/protections/max_drawdown_protection.py
#
# Calculate max drawdown from recent trades. If it exceeds the threshold,
# stop all trading for a cooldown period.
#
# Freqtrade supports two modes:
#   - "ratios": drawdown based on trade profit ratios
#   - "equity": drawdown based on actual account equity curve
# =============================================================================

class MaxDrawdownProtection:
    """
    Stop trading if drawdown exceeds threshold within a lookback window.

    Source: plugins/protections/max_drawdown_protection.py

    Freqtrade calculates drawdown as the maximum peak-to-trough decline
    in the equity curve (or cumulative profit ratios) over the lookback period.
    """

    def __init__(
        self,
        max_allowed_drawdown: float = 0.10,  # 10% max drawdown
        lookback_period_minutes: int = 1440,  # 24 hours
        stop_duration_minutes: int = 60,      # pause for 1 hour
        starting_balance: float = 10000.0,
    ):
        self.max_allowed_drawdown = max_allowed_drawdown
        self.lookback_minutes = lookback_period_minutes
        self.stop_duration_minutes = stop_duration_minutes
        self.starting_balance = starting_balance
        # List of (timestamp, profit_abs) for closed trades
        self._trade_log: List[Dict] = []
        self._peak_equity: float = starting_balance
        self._lock_until: float = 0

    def record_trade(self, profit_abs: float, timestamp: Optional[float] = None) -> None:
        """Record a closed trade's absolute profit."""
        ts = timestamp or time.time()
        self._trade_log.append({"time": ts, "profit_abs": profit_abs})

    def calculate_max_drawdown(self, now: Optional[float] = None) -> float:
        """
        Calculate max drawdown from recent trades.

        Source: max_drawdown_protection.py _max_drawdown()

        Uses the equity-curve method:
        1. Calculate cumulative equity from starting_balance + trade profits
        2. Find the max peak-to-trough decline as a percentage

        Returns:
            Max drawdown as a ratio (e.g., 0.10 = 10%)
        """
        now = now or time.time()
        lookback_until = now - (self.lookback_minutes * 60)

        # Get trades in window
        recent = [t for t in self._trade_log if t["time"] >= lookback_until]
        if len(recent) < 2:
            return 0.0

        # Build equity curve
        cumulative_profit = 0.0
        peak = self.starting_balance
        max_dd = 0.0

        for trade in sorted(recent, key=lambda t: t["time"]):
            cumulative_profit += trade["profit_abs"]
            equity = self.starting_balance + cumulative_profit
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        return max_dd

    def check(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Check if drawdown protection should trigger."""
        now = now or time.time()
        dd = self.calculate_max_drawdown(now)

        if dd > self.max_allowed_drawdown:
            lock_until = now + (self.stop_duration_minutes * 60)
            logger.warning(
                f"MaxDrawdownProtection: {dd:.2%} > {self.max_allowed_drawdown:.2%} "
                f"→ pausing until {datetime.fromtimestamp(lock_until).strftime('%H:%M')}"
            )
            return {
                "triggered": True,
                "drawdown_pct": round(dd * 100, 2),
                "threshold_pct": round(self.max_allowed_drawdown * 100, 2),
                "until": lock_until,
            }

        return {
            "triggered": False,
            "drawdown_pct": round(dd * 100, 2),
            "threshold_pct": round(self.max_allowed_drawdown * 100, 2),
        }


# =============================================================================
# PATTERN 8: Daily Loss Limit
# Source: Combines stoploss_guard.py + drawdown_protection.py concepts
#
# Freqtrade doesn't have a single "daily loss limit" — it combines:
#   - stoploss_guard (count-based: N stoplosses in window)
#   - max_drawdown (equity-based: X% decline in window)
#
# Below is a clean daily loss limit implementation that tracks cumulative
# daily PnL and stops trading when the limit is breached.
# =============================================================================

class DailyLossLimit:
    """
    Stop trading when cumulative daily loss exceeds threshold.

    This is a simplified daily loss limit that combines freqtrade's
    stoploss_guard and max_drawdown concepts into a single daily check.
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 0.05,  # 5% max daily loss
        starting_balance: float = 10000.0,
        reset_hour: int = 0,  # UTC hour to reset (midnight)
    ):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.starting_balance = starting_balance
        self.reset_hour = reset_hour
        self._daily_pnl: float = 0.0
        self._last_reset_date: Optional[str] = None

    def _check_reset(self) -> None:
        """Reset daily PnL if a new day has started."""
        now = datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_pnl = 0.0
            self._last_reset_date = today
            logger.info(f"DailyLossLimit: reset daily PnL for {today}")

    def record_pnl(self, pnl_usdt: float) -> None:
        """Record a trade's PnL (positive or negative)."""
        self._check_reset()
        self._daily_pnl += pnl_usdt
        logger.info(
            f"DailyLossLimit: PnL={pnl_usdt:+.2f} → daily_total={self._daily_pnl:+.2f}"
        )

    def check(self, current_equity: Optional[float] = None) -> Dict[str, Any]:
        """
        Check if daily loss limit is breached.

        Returns:
            Dict with blocked, daily_pnl, daily_pnl_pct, threshold_pct
        """
        self._check_reset()

        equity = current_equity or self.starting_balance
        daily_loss_pct = abs(min(0, self._daily_pnl)) / equity if equity > 0 else 0
        threshold = self.max_daily_loss_pct

        if self._daily_pnl < 0 and daily_loss_pct >= threshold:
            logger.warning(
                f"DailyLossLimit: BLOCKED — daily loss {daily_loss_pct:.2%} >= {threshold:.2%}"
            )
            return {
                "blocked": True,
                "daily_pnl": round(self._daily_pnl, 2),
                "daily_pnl_pct": round(daily_loss_pct * 100, 2),
                "threshold_pct": round(threshold * 100, 2),
                "reason": f"Daily loss {daily_loss_pct:.2%} exceeds {threshold:.2%} limit",
            }

        return {
            "blocked": False,
            "daily_pnl": round(self._daily_pnl, 2),
            "daily_pnl_pct": round(daily_loss_pct * 100, 2),
            "threshold_pct": round(threshold * 100, 2),
        }


# =============================================================================
# PATTERN 9: Tradable Balance Ratio
# Source: wallets.py — get_total_stake_amount()
#
# Freqtrade keeps a small buffer (tradable_balance_ratio, default 0.99)
# to ensure fees and slippage are covered. This prevents over-leveraging.
# =============================================================================

def apply_tradable_balance_ratio(
    total_balance: float,
    tied_up_in_trades: float,
    tradable_balance_ratio: float = 0.99,
) -> float:
    """
    Calculate available balance respecting the tradable balance ratio.

    Source: wallets.py get_total_stake_amount()

    Freqtrade formula:
        available = (tied_up + free) * ratio - tied_up

    This ensures that even with all positions open, we maintain
    a small buffer for fees and unexpected costs.

    Args:
        total_balance: Total account balance
        tied_up_in_trades: Amount currently in open trades
        tradable_balance_ratio: Fraction of balance that's tradable

    Returns:
        Available balance for new trades
    """
    free = total_balance - tied_up_in_trades
    total_usable = (tied_up_in_trades + free) * tradable_balance_ratio
    available = total_usable - tied_up_in_trades
    return min(available, free)


# =============================================================================
# PATTERN 10: Protection Manager (Unified Entry Point)
# Source: plugins/protectionmanager.py
#
# Freqtrade uses a ProtectionManager that runs all protections in sequence:
#   1. Global stops (affect ALL pairs) — checked first
#   2. Per-pair stops (affect individual pairs)
#   3. Each protection returns a lock with expiry time
#   4. Locks are stored in a PairLocks table
# =============================================================================

class ProtectionManager:
    """
    Unified protection manager that orchestrates all risk protections.

    Source: plugins/protectionmanager.py

    In freqtrade, after each trade closes, the ProtectionManager runs:
      1. global_stop() — checks if ALL trading should stop
      2. stop_per_pair() — checks if specific pairs should be locked
    """

    def __init__(self):
        self.cooldown = CooldownAfterLoss(cooldown_minutes=60)
        self.stoploss_guard = StoplossGuard(
            trade_limit=3, lookback_period_minutes=60, stop_duration_minutes=240
        )
        self.drawdown_protection = MaxDrawdownProtection(
            max_allowed_drawdown=0.10, lookback_period_minutes=1440
        )
        self.daily_loss = DailyLossLimit(max_daily_loss_pct=0.05)
        # {pair: unlock_timestamp} — unified lock storage
        self._pair_locks: Dict[str, float] = {}
        self._global_lock_until: float = 0

    def on_trade_closed(
        self,
        pair: str,
        profit_pct: float,
        profit_abs: float,
        exit_reason: str,
    ) -> None:
        """
        Called after a trade closes. Runs all protections.

        This mirrors freqtrade's handle_protections() in freqtradebot.py.
        """
        # 1. Record for cooldown
        self.cooldown.record_trade_close(pair)

        # 2. If it was a stoploss exit, record for stoploss guard
        if "stoploss" in exit_reason.lower() or "stop_loss" in exit_reason.lower():
            self.stoploss_guard.record_stoploss_hit(pair, profit_pct)

        # 3. Record for drawdown protection
        self.drawdown_protection.record_trade(profit_abs)

        # 4. Record for daily loss limit
        self.daily_loss.record_pnl(profit_abs)

    def check_all_protections(self, pair: str) -> Dict[str, Any]:
        """
        Run all protections and return combined result.

        Returns:
            Dict with allowed, reasons, locks
        """
        reasons = []
        allowed = True

        # Check global lock
        now = time.time()
        if now < self._global_lock_until:
            remaining = (self._global_lock_until - now) / 60
            return {
                "allowed": False,
                "reasons": [f"Global lock active ({remaining:.0f}min remaining)"],
            }

        # 1. Cooldown check
        if self.cooldown.is_pair_locked(pair):
            allowed = False
            unlock = self.cooldown.get_lock_until(pair)
            reasons.append(f"Pair {pair} in cooldown until {datetime.fromtimestamp(unlock).strftime('%H:%M')}")

        # 2. Stoploss guard check
        sl_check = self.stoploss_guard.check()
        if sl_check["triggered"]:
            allowed = False
            if "*" in sl_check.get("locked_pairs", []):
                self._global_lock_until = sl_check["until"]
            reasons.append(sl_check["reason"])

        # 3. Drawdown check
        dd_check = self.drawdown_protection.check()
        if dd_check["triggered"]:
            allowed = False
            self._global_lock_until = dd_check["until"]
            reasons.append(f"Drawdown {dd_check['drawdown_pct']:.1f}% > {dd_check['threshold_pct']:.1f}%")

        # 4. Daily loss check
        dl_check = self.daily_loss.check()
        if dl_check["blocked"]:
            allowed = False
            reasons.append(dl_check["reason"])

        return {
            "allowed": allowed,
            "reasons": reasons,
            "checks": {
                "cooldown": not self.cooldown.is_pair_locked(pair),
                "stoploss_guard": not sl_check["triggered"],
                "drawdown": not dd_check["triggered"],
                "daily_loss": not dl_check["blocked"],
            },
        }


# =============================================================================
# INTEGRATION GUIDE
# =============================================================================
#
# To integrate these patterns into the existing RiskManager:
#
# 1. POSITION SIZING (replace flat max_position_pct):
#    - Add calculate_risk_per_trade_position_size() to RiskManager
#    - Call it in pre_trade_check() instead of using a fixed percentage
#    - Pass entry_price and stoploss_price from the signal
#
# 2. TRAILING STOP (enhance existing TrailingStop class):
#    - Add FreqtradeStyleTrailingStop as an alternative trailing mode
#    - Key improvement: ratchet mechanism (stop only moves in favorable direction)
#    - Add profit-threshold-based stop tightening
#
# 3. BREAKEVEN STOP (new feature):
#    - Add dynamic_breakeven_stoploss() to TrailingStop
#    - Call it on each price update when profit > 2%
#
# 4. COOLDOWN (add per-pair cooldown):
#    - Add CooldownAfterLoss to RiskManager
#    - Call record_trade_close() in post_trade_update()
#    - Check is_pair_locked() in pre_trade_check()
#
# 5. DAILY LOSS LIMIT (new feature):
#    - Add DailyLossLimit to RiskManager
#    - Call record_pnl() in post_trade_update()
#    - Check in pre_trade_check() — block all new trades if breached
#
# 6. STOPLOSS GUARD (new feature):
#    - Add StoplossGuard to RiskManager
#    - Call record_stoploss_hit() when a SL exit occurs
#    - Prevents "death spiral" of repeated stop-outs
#
# 7. PROTECTION MANAGER (unified entry):
#    - Add ProtectionManager to RiskManager
#    - Single check_all_protections() call replaces individual checks
#    - Cleaner code, all protections in one place
#
# Example integration in pre_trade_check():
#
#   def pre_trade_check(self, symbol, price, atr, positions, ...):
#       reasons = []
#       allowed = True
#
#       # ... existing checks ...
#
#       # NEW: Risk-based position sizing
#       portfolio_value = self._get_portfolio_value()
#       stoploss_price = price * (1 - abs(self.stoploss_pct))
#       stake = calculate_risk_per_trade_position_size(
#           portfolio_value=portfolio_value,
#           risk_pct=0.02,  # 2% risk per trade
#           entry_price=price,
#           stoploss_price=stoploss_price,
#       )
#       adjustments["stake_amount"] = stake
#
#       # NEW: Protection manager check
#       prot_check = self.protection_manager.check_all_protections(symbol)
#       if not prot_check["allowed"]:
#           allowed = False
#           reasons.extend(prot_check["reasons"])
#
#       return {"allowed": allowed, "reasons": reasons, "adjustments": adjustments}
