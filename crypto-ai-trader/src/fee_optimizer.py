"""
Fee Optimizer - Reduce trading costs through BNB discount and maker orders.

Binance fee structure:
- Standard: 0.1% taker, 0.1% maker (spot)
- BNB discount: 25% off = 0.075% taker, 0.075% maker
- VIP levels: lower fees based on 30d volume + BNB holdings

Strategies:
1. BNB discount: Use BNB for fee payment (25% savings)
2. Maker orders: Place limit orders that add liquidity (same as taker on spot,
   but on futures maker is cheaper)
3. Fee estimation: Accurate pre-trade cost calculation

Usage:
    from src.fee_optimizer import FeeOptimizer
    opt = FeeOptimizer(client)

    # Check if BNB discount is enabled
    status = opt.get_bnb_status()

    # Estimate trade cost
    cost = opt.estimate_cost(symbol='BTCUSDT', quantity=0.1, price=65000, use_bnb=True)

    # Get optimal order type (market vs limit)
    order_type = opt.recommend_order_type(urgency='low')  # 'LIMIT' for maker
"""

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from typing import Dict

logger = logging.getLogger(__name__)

# Binance spot fee tiers (as of 2024)
DEFAULT_TAKER_FEE = 0.001  # 0.1%
DEFAULT_MAKER_FEE = 0.001  # 0.1% (same as taker for spot)
BNB_DISCOUNT_PCT = 0.25  # 25% discount

# Futures fees (for reference)
FUTURES_TAKER = 0.0005  # 0.05%
FUTURES_MAKER = 0.0002  # 0.02%


class FeeOptimizer:
    """Optimize trading fees on Binance."""

    def __init__(self, binance_client: Optional["ExchangeClient"] = None):
        self.client = binance_client
        self._fee_tier: Optional[Dict] = None
        self._bnb_balance: float = 0.0
        self._use_bnb: bool = True  # Prefer BNB discount

    def get_account_fee_tier(self) -> Dict:
        """Fetch current fee tier from Binance API.

        Returns:
            {
                "tier": int,
                "taker_fee": float,
                "maker_fee": float,
                "bnb_discount": bool,
            }
        """
        if self._fee_tier and not self.client:
            return self._fee_tier

        if not self.client:
            # Default tier
            return {
                "tier": 0,
                "taker_fee": DEFAULT_TAKER_FEE,
                "maker_fee": DEFAULT_MAKER_FEE,
                "bnb_discount": False,
            }

        try:
            # Get account info which includes fee tier
            self.client.get_account()
            # Binance doesn't expose fee tier directly in standard API
            # We infer from commission rates or assume default
            return {
                "tier": 0,
                "taker_fee": DEFAULT_TAKER_FEE,
                "maker_fee": DEFAULT_MAKER_FEE,
                "bnb_discount": self._check_bnb_discount(),
            }
        except Exception as e:
            logger.warning("FeeOptimizer: failed to get fee tier: %s", e)
            return {
                "tier": 0,
                "taker_fee": DEFAULT_TAKER_FEE,
                "maker_fee": DEFAULT_MAKER_FEE,
                "bnb_discount": False,
            }

    def _check_bnb_discount(self) -> bool:
        """Check if BNB discount is enabled and sufficient BNB held."""
        if not self.client:
            return False
        try:
            bnb_bal = self.client.get_free_balance("BNB")
            self._bnb_balance = bnb_bal
            # Need at least 0.01 BNB for discount to be useful
            return bnb_bal >= 0.01
        except Exception as e:
            logger.warning("FeeOptimizer: failed to check BNB balance: %s", e)
            return False

    def _get_bnb_price(self):
        """Fetch live BNB price."""
        if not self.client:
            return 600
        try:
            price = self.client.get_ticker_price("BNBUSDT")
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        return 600  # fallback

    def get_effective_fees(self, use_bnb: Optional[bool] = None) -> Dict:
        """Get effective trading fees after discounts.

        Args:
            use_bnb: Whether to apply BNB discount (default: auto-detect)

        Returns:
            {
                "taker_fee": float,  # e.g., 0.00075 for 0.075%
                "maker_fee": float,
                "bnb_discount_applied": bool,
                "savings_vs_standard_pct": float,
            }
        """
        if use_bnb is None:
            use_bnb = self._use_bnb and self._check_bnb_discount()

        tier = self.get_account_fee_tier()
        base_taker = tier["taker_fee"]
        base_maker = tier["maker_fee"]

        if use_bnb:
            taker = base_taker * (1 - BNB_DISCOUNT_PCT)
            maker = base_maker * (1 - BNB_DISCOUNT_PCT)
            savings = BNB_DISCOUNT_PCT * 100
        else:
            taker = base_taker
            maker = base_maker
            savings = 0.0

        return {
            "taker_fee": round(taker, 6),
            "maker_fee": round(maker, 6),
            "bnb_discount_applied": use_bnb,
            "savings_vs_standard_pct": round(savings, 1),
        }

    def estimate_cost(
        self,
        symbol: str,
        quantity: float,
        price: float,
        side: str = "BUY",
        order_type: str = "MARKET",
        use_bnb: Optional[bool] = None,
    ) -> Dict:
        """Estimate total trading cost for a planned trade.

        Args:
            symbol: Trading pair
            quantity: Amount of base asset
            price: Expected fill price
            side: "BUY" or "SELL"
            order_type: "MARKET" or "LIMIT"
            use_bnb: Apply BNB discount

        Returns:
            {
                "notional": float,  # trade value in USDT
                "fee_rate": float,
                "fee_amount": float,  # in USDT
                "fee_asset": str,  # "USDT" or "BNB"
                "net_proceeds": float,  # after fees
                "cost_pct": float,  # fee as % of trade
            }
        """
        notional = quantity * price
        fees = self.get_effective_fees(use_bnb)

        is_maker = order_type.upper() == "LIMIT"
        fee_rate = fees["maker_fee"] if is_maker else fees["taker_fee"]
        fee_amount = notional * fee_rate

        # BNB fee payment: fee is deducted in BNB at BNB/USDT rate
        if fees["bnb_discount_applied"]:
            fee_asset = "BNB"
            # Approximate: fee_amount is in USDT equivalent, paid in BNB
            # Actual BNB amount depends on BNB price at execution time
            fee_bnb_approx = fee_amount / self._get_bnb_price()  # Live BNB price
        else:
            fee_asset = "USDT"
            fee_bnb_approx = 0

        if side == "BUY":
            net_proceeds = notional - fee_amount  # You get less asset value
        else:
            net_proceeds = notional - fee_amount  # You receive less USDT

        return {
            "notional": round(notional, 2),
            "fee_rate": fee_rate,
            "fee_amount": round(fee_amount, 4),
            "fee_asset": fee_asset,
            "fee_bnb_approx": round(fee_bnb_approx, 6) if fee_bnb_approx else None,
            "net_proceeds": round(net_proceeds, 2),
            "cost_pct": round(fee_rate * 100, 4),
            "bnb_discount": fees["bnb_discount_applied"],
            "savings_vs_standard": round(
                notional * DEFAULT_TAKER_FEE * BNB_DISCOUNT_PCT, 4
            ),
        }

    def recommend_order_type(
        self, urgency: str = "normal", spread_pct: Optional[float] = None
    ) -> str:
        """Recommend MARKET vs LIMIT based on urgency and spread.

        Args:
            urgency: "high" (use market), "normal", "low" (use limit)
            spread_pct: Current bid-ask spread (if known)

        Returns:
            "MARKET" or "LIMIT"
        """
        if urgency == "high":
            return "MARKET"

        if urgency == "low" and spread_pct is not None:
            # If spread is tight (< 0.05%), limit order is fine
            if spread_pct < 0.05:
                return "LIMIT"

        # Default: market for reliability
        return "MARKET"

    def get_bnb_status(self) -> Dict:
        """Get BNB discount status and recommendations."""
        has_bnb = self._check_bnb_discount()
        fees = self.get_effective_fees(use_bnb=has_bnb)

        return {
            "bnb_balance": round(self._bnb_balance, 4),
            "bnb_discount_enabled": has_bnb,
            "current_taker_fee": fees["taker_fee"],
            "current_maker_fee": fees["maker_fee"],
            "savings_vs_standard_pct": fees["savings_vs_standard_pct"],
            "recommendation": (
                "Hold >0.01 BNB for 25% fee discount"
                if not has_bnb
                else "BNB discount active"
            ),
        }

    def calculate_break_even(
        self,
        entry_price: float,
        quantity: float,
        side: str = "BUY",
        use_bnb: Optional[bool] = None,
    ) -> Dict:
        """Calculate break-even price after accounting for fees.

        For a round-trip (buy + sell), what price move is needed to break even?

        Returns:
            {
                "entry_cost": float,
                "exit_cost": float,
                "total_fees": float,
                "break_even_price": float,
                "required_move_pct": float,
            }
        """
        entry = self.estimate_cost(
            symbol="",
            quantity=quantity,
            price=entry_price,
            side=side,
            order_type="MARKET",
            use_bnb=use_bnb,
        )
        exit_side = "SELL" if side == "BUY" else "BUY"
        exit_ = self.estimate_cost(
            symbol="",
            quantity=quantity,
            price=entry_price,
            side=exit_side,
            order_type="MARKET",
            use_bnb=use_bnb,
        )

        total_fees = entry["fee_amount"] + exit_["fee_amount"]
        quantity * entry_price

        # Break even: price needs to move enough to cover fees
        if side == "BUY":
            break_even = entry_price + (total_fees / quantity)
        else:
            break_even = entry_price - (total_fees / quantity)

        required_move = (break_even - entry_price) / entry_price * 100

        return {
            "entry_cost": entry["fee_amount"],
            "exit_cost": exit_["fee_amount"],
            "total_fees": round(total_fees, 4),
            "break_even_price": round(break_even, 2),
            "required_move_pct": round(abs(required_move), 4),
        }
