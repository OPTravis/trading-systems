"""
Trade Executor — Order placement via BrokerProtocol with smart routing.

Handles:
- Market/Limit/Stop order types
- Smart routing (NYSE vs NASDAQ based on listing)
- Order tracking and fill confirmation
- Retry logic for failed orders
- Position sizing via HybridPositionSizer (Kelly × CVaR × Vol Target)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Exchange routing map ────────────────────────────────────────────────────

# Common NASDAQ-listed symbols
NASDAQ_SYMBOLS = {
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "META", "NVDA", "TSLA",
    "AVGO", "COST", "NFLX", "AMD", "INTC", "CSCO", "ADBE", "QCOM",
    "CMCSA", "PEP", "TMUS", "CHTR", "BKNG", "SBUX", "MELI", "PYPL",
    "MRVL", "LRCX", "KLAC", "SNPS", "CDNS", "MNST", "ORLY", "IDXX",
    "NXPI", "WDAY", "FTNT", "DDOG", "PANW", "CRWD", "ZS", "TEAM",
    "ABNB", "COIN", "HOOD", "PLTR", "SOFI", "RIVN", "LCID",
}


class RoutingDecision(str, Enum):
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"
    SMART = "SMART"  # Let broker decide


# ─── Order result model ─────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Result of an order execution attempt."""
    success: bool
    symbol: str
    side: str
    order_type: str
    requested_qty: float
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    commission: float = 0.0
    order_id: Optional[int] = None
    exchange: str = ""
    error: str = ""
    retry_count: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_qty": self.requested_qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "commission": self.commission,
            "order_id": self.order_id,
            "exchange": self.exchange,
            "error": self.error,
            "retry_count": self.retry_count,
            "timestamp": self.timestamp,
        }


# ─── Position Sizer ─────────────────────────────────────────────────────────

class HybridPositionSizer:
    """
    Hybrid position sizer: Kelly × CVaR × Vol Target.

    Combines three sizing methods:
    1. Kelly Criterion: Optimal bet size based on win rate and payoff ratio
    2. CVaR (Conditional Value at Risk): Tail-risk-aware sizing
    3. Volatility Targeting: Normalize to target portfolio volatility

    Final size = min(Kelly, CVaR, VolTarget) × regime_multiplier
    """

    TARGET_VOL = 0.15  # 15% annualized portfolio volatility target
    MAX_POSITION_PCT = 0.20  # Max 20% in single position
    MIN_POSITION_PCT = 0.01  # Min 1%

    def __init__(
        self,
        win_rate: float = 0.55,
        payoff_ratio: float = 2.0,
        cvar_confidence: float = 0.95,
        cvar_max_loss: float = 0.05,
    ):
        self.win_rate = win_rate
        self.payoff_ratio = payoff_ratio
        self.cvar_confidence = cvar_confidence
        self.cvar_max_loss = cvar_max_loss

    def size_position(
        self,
        symbol: str,
        nav: float,
        stock_vol: float = 0.25,
        n_positions: int = 10,
        regime_multiplier: float = 1.0,
        vix_multiplier: float = 1.0,
    ) -> Dict:
        """
        Calculate position size using hybrid approach.

        Args:
            symbol: Stock symbol.
            nav: Net Asset Value (total portfolio value).
            stock_vol: Annualized volatility of the stock.
            n_positions: Current number of open positions.
            regime_multiplier: Regime-based sizing adjustment.
            vix_multiplier: VIX-based sizing adjustment.

        Returns:
            Dict with position_size (fraction), position_usd, method breakdown.
        """
        import math

        # 1. Kelly Criterion (Half-Kelly for safety)
        kelly_frac = self._kelly_fraction()

        # 2. CVaR-based sizing
        cvar_frac = self._cvar_fraction(stock_vol)

        # 3. Volatility Target sizing
        vol_frac = self._vol_target_fraction(stock_vol, n_positions)

        # Take the minimum (most conservative)
        raw_frac = min(kelly_frac, cvar_frac, vol_frac)

        # Apply multipliers
        adjusted_frac = raw_frac * regime_multiplier * vix_multiplier

        # Clamp to limits
        final_frac = max(self.MIN_POSITION_PCT, min(self.MAX_POSITION_PCT, adjusted_frac))

        position_usd = nav * final_frac

        logger.info(
            "PositionSizer %s: Kelly=%.2f%% CVaR=%.2f%% Vol=%.2f%% → raw=%.2f%% "
            "× regime=%.2f × vix=%.2f → final=%.2f%% ($%.0f)",
            symbol,
            kelly_frac * 100, cvar_frac * 100, vol_frac * 100,
            raw_frac * 100,
            regime_multiplier, vix_multiplier,
            final_frac * 100, position_usd,
        )

        return {
            "position_pct": final_frac,
            "position_usd": position_usd,
            "kelly_pct": kelly_frac,
            "cvar_pct": cvar_frac,
            "vol_target_pct": vol_frac,
            "regime_multiplier": regime_multiplier,
            "vix_multiplier": vix_multiplier,
        }

    def _kelly_fraction(self) -> float:
        """Half-Kelly criterion: f* = (p*b - q) / b, halved for safety."""
        p = self.win_rate
        q = 1 - p
        b = self.payoff_ratio
        if b <= 0:
            return 0.0
        full_kelly = (p * b - q) / b
        half_kelly = full_kelly / 2
        return max(0.0, min(0.25, half_kelly))  # Cap at 25%

    def _cvar_fraction(self, stock_vol: float) -> float:
        """CVaR-based sizing: limit expected tail loss to cvar_max_loss of NAV."""
        if stock_vol <= 0:
            return 0.0
        # Simplified: CVaR ≈ vol × z_score for 95% confidence
        z_95 = 1.645
        cvar_per_unit = stock_vol * z_95
        if cvar_per_unit <= 0:
            return 0.0
        return min(0.25, self.cvar_max_loss / cvar_per_unit)

    def _vol_target_fraction(self, stock_vol: float, n_positions: int) -> float:
        """Volatility-target sizing: size = target_vol / (stock_vol × sqrt(n))."""
        import math
        if stock_vol <= 0:
            return 0.0
        n = max(1, n_positions)
        return min(0.25, (self.TARGET_VOL / stock_vol) / math.sqrt(n))


# ─── Trade Executor ─────────────────────────────────────────────────────────

class TradeExecutor:
    """
    Order execution engine using BrokerProtocol.

    Features:
    - Market / Limit / Stop order types
    - Smart exchange routing (NYSE vs NASDAQ)
    - Order fill confirmation with timeout
    - Retry logic with exponential backoff
    - Position sizing via HybridPositionSizer
    """

    MAX_RETRIES = 3
    FILL_TIMEOUT_SEC = 30
    RETRY_BACKOFF = [1, 3, 10]  # seconds between retries

    def __init__(
        self,
        broker=None,
        position_sizer: HybridPositionSizer = None,
        portfolio=None,
    ):
        """
        Args:
            broker: BrokerProtocol instance.
            position_sizer: HybridPositionSizer for sizing.
            portfolio: PortfolioManager for portfolio context.
        """
        self.broker = broker
        self.sizer = position_sizer or HybridPositionSizer()
        self.portfolio = portfolio
        self._execution_log: List[ExecutionResult] = []

    # ── Smart Routing ───────────────────────────────────────────────────

    @staticmethod
    def route_exchange(symbol: str) -> RoutingDecision:
        """Determine best exchange for a symbol based on listing."""
        clean_symbol = symbol.replace(".", "-").upper()
        if clean_symbol in NASDAQ_SYMBOLS:
            return RoutingDecision.NASDAQ
        # Default to SMART (let broker route optimally)
        return RoutingDecision.SMART

    # ── Order Execution ─────────────────────────────────────────────────

    def execute(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float = 0.0,
        order_type: str = "MKT",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        time_in_force: str = "DAY",
    ) -> dict:
        """
        Execute a trade order.

        Args:
            symbol: Stock symbol.
            side: 'BUY' or 'SELL'.
            quantity: Number of shares.
            price: Limit price (for LMT/STPLMT orders).
            order_type: 'MKT', 'LMT', 'STP', 'STPLMT'.
            stop_loss: Optional stop-loss price (placed as separate order).
            take_profit: Optional take-profit price.
            time_in_force: 'DAY', 'GTC', 'IOC', 'FOK'.

        Returns:
            Dict with success status and execution details.
        """
        from src.brokers.broker_protocol import (
            Contract, Order, OrderSide, OrderType, TimeInForce,
        )

        if not self.broker:
            return {"success": False, "error": "No broker configured"}

        # Route to exchange
        exchange = self.route_exchange(symbol)
        contract = Contract(symbol=symbol, exchange=exchange.value, currency="USD")

        # Map order type
        order_type_map = {
            "MKT": OrderType.MARKET,
            "LMT": OrderType.LIMIT,
            "STP": OrderType.STOP,
            "STPLMT": OrderType.STOP_LIMIT,
        }
        ot = order_type_map.get(order_type.upper(), OrderType.MARKET)
        side_enum = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        tif_map = {
            "DAY": TimeInForce.DAY,
            "GTC": TimeInForce.GTC,
            "IOC": TimeInForce.IOC,
            "FOK": TimeInForce.FOK,
        }
        tif = tif_map.get(time_in_force.upper(), TimeInForce.DAY)

        order = Order(
            contract=contract,
            side=side_enum,
            order_type=ot,
            quantity=quantity,
            limit_price=price if ot in (OrderType.LIMIT, OrderType.STOP_LIMIT) else None,
            stop_price=price if ot in (OrderType.STOP, OrderType.STOP_LIMIT) else None,
            time_in_force=tif,
        )

        # Execute with retry
        result = self._execute_with_retry(order, symbol, side, quantity)

        # Place stop-loss if requested and main order filled
        if result.success and stop_loss and side.upper() == "BUY":
            self._place_stop_loss(symbol, quantity, stop_loss)

        # Place take-profit if requested and main order filled
        if result.success and take_profit and side.upper() == "BUY":
            self._place_take_profit(symbol, quantity, take_profit)

        self._execution_log.append(result)
        return result.to_dict()

    def _execute_with_retry(
        self, order, symbol: str, side: str, quantity: float
    ) -> ExecutionResult:
        """Place order with retry logic."""
        result = None

        for attempt in range(self.MAX_RETRIES):
            try:
                placed = self.broker.place_order(order)

                if placed.status in ("FILLED", "PARTIALLY_FILLED"):
                    result = ExecutionResult(
                        success=True,
                        symbol=symbol,
                        side=side,
                        order_type=order.order_type.value,
                        requested_qty=quantity,
                        filled_qty=placed.filled_qty,
                        avg_fill_price=placed.avg_fill_price,
                        commission=placed.commission,
                        order_id=placed.order_id,
                        exchange=order.contract.exchange,
                        retry_count=attempt,
                        timestamp=datetime.now().isoformat(),
                    )
                    logger.info(
                        "ORDER FILLED: %s %s %.2f @ %.2f (order_id=%s, attempt=%d)",
                        side, symbol, placed.filled_qty,
                        placed.avg_fill_price, placed.order_id, attempt + 1,
                    )
                    return result

                elif placed.status == "REJECTED":
                    result = ExecutionResult(
                        success=False, symbol=symbol, side=side,
                        order_type=order.order_type.value, requested_qty=quantity,
                        error="Order rejected by broker",
                        retry_count=attempt,
                        timestamp=datetime.now().isoformat(),
                    )
                    logger.warning("ORDER REJECTED: %s %s — %s", side, symbol, result.error)
                    return result  # Don't retry rejections

                elif placed.status == "CANCELLED":
                    result = ExecutionResult(
                        success=False, symbol=symbol, side=side,
                        order_type=order.order_type.value, requested_qty=quantity,
                        error="Order cancelled",
                        retry_count=attempt,
                        timestamp=datetime.now().isoformat(),
                    )
                    return result

                else:
                    # Pending/submitted — wait for fill
                    fill_result = self._wait_for_fill(placed.order_id, symbol, side, quantity, attempt)
                    if fill_result:
                        return fill_result

            except Exception as e:
                logger.warning(
                    "Order attempt %d/%d failed for %s: %s",
                    attempt + 1, self.MAX_RETRIES, symbol, e,
                )
                if attempt < self.MAX_RETRIES - 1:
                    backoff = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                    logger.info("Retrying in %ds...", backoff)
                    time.sleep(backoff)

        # All retries exhausted
        if result is None:
            result = ExecutionResult(
                success=False, symbol=symbol, side=side,
                order_type=order.order_type.value, requested_qty=quantity,
                error=f"All {self.MAX_RETRIES} attempts failed",
                retry_count=self.MAX_RETRIES,
                timestamp=datetime.now().isoformat(),
            )
        return result

    def _wait_for_fill(
        self, order_id: int, symbol: str, side: str, quantity: float, attempt: int
    ) -> Optional[ExecutionResult]:
        """Poll for order fill confirmation."""
        deadline = time.time() + self.FILL_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                open_orders = self.broker.get_open_orders()
                for o in open_orders:
                    if o.order_id == order_id:
                        if o.status == "FILLED":
                            return ExecutionResult(
                                success=True, symbol=symbol, side=side,
                                order_type="LMT", requested_qty=quantity,
                                filled_qty=o.filled_qty,
                                avg_fill_price=o.avg_fill_price,
                                commission=o.commission,
                                order_id=order_id,
                                retry_count=attempt,
                                timestamp=datetime.now().isoformat(),
                            )
                        elif o.status in ("CANCELLED", "REJECTED"):
                            return ExecutionResult(
                                success=False, symbol=symbol, side=side,
                                order_type="LMT", requested_qty=quantity,
                                error=f"Order {o.status.value}",
                                order_id=order_id,
                                retry_count=attempt,
                                timestamp=datetime.now().isoformat(),
                            )
            except Exception:
                pass
            time.sleep(1)

        logger.warning("Fill timeout for order %s (symbol=%s)", order_id, symbol)
        return None

    # ── Stop-Loss / Take-Profit ─────────────────────────────────────────

    def _place_stop_loss(self, symbol: str, quantity: float, stop_price: float):
        """Place a stop-loss order."""
        from src.brokers.broker_protocol import (
            Contract, Order, OrderSide, OrderType, TimeInForce,
        )
        try:
            exchange = self.route_exchange(symbol)
            contract = Contract(symbol=symbol, exchange=exchange.value, currency="USD")
            order = Order(
                contract=contract,
                side=OrderSide.SELL,
                order_type=OrderType.STOP,
                quantity=quantity,
                stop_price=stop_price,
                time_in_force=TimeInForce.GTC,
            )
            placed = self.broker.place_order(order)
            logger.info("Stop-loss placed: %s @ %.2f (order_id=%s)",
                        symbol, stop_price, placed.order_id)
        except Exception as e:
            logger.error("Failed to place stop-loss for %s: %s", symbol, e)

    def _place_take_profit(self, symbol: str, quantity: float, target_price: float):
        """Place a take-profit (limit sell) order."""
        from src.brokers.broker_protocol import (
            Contract, Order, OrderSide, OrderType, TimeInForce,
        )
        try:
            exchange = self.route_exchange(symbol)
            contract = Contract(symbol=symbol, exchange=exchange.value, currency="USD")
            order = Order(
                contract=contract,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                limit_price=target_price,
                time_in_force=TimeInForce.GTC,
            )
            placed = self.broker.place_order(order)
            logger.info("Take-profit placed: %s @ %.2f (order_id=%s)",
                        symbol, target_price, placed.order_id)
        except Exception as e:
            logger.error("Failed to place take-profit for %s: %s", symbol, e)

    # ── Size & Execute ──────────────────────────────────────────────────

    def size_and_execute(
        self,
        symbol: str,
        price: float,
        side: str = "BUY",
        regime_multiplier: float = 1.0,
        vix_multiplier: float = 1.0,
        order_type: str = "LMT",
        stop_loss_pct: float = 5.0,
        take_profit_pct: float = 10.0,
    ) -> dict:
        """
        Full pipeline: calculate position size, then execute.

        Args:
            symbol: Stock symbol.
            price: Current price.
            side: 'BUY' or 'SELL'.
            regime_multiplier: Regime-based sizing adjustment.
            vix_multiplier: VIX-based sizing adjustment.
            order_type: Order type.
            stop_loss_pct: Stop-loss percentage below entry.
            take_profit_pct: Take-profit percentage above entry.

        Returns:
            Execution result dict.
        """
        if not self.portfolio:
            return {"success": False, "error": "No portfolio configured"}

        nav = self.portfolio.get_nav()
        n_positions = self.portfolio.position_count

        # Size
        sizing = self.sizer.size_position(
            symbol=symbol,
            nav=nav,
            n_positions=n_positions,
            regime_multiplier=regime_multiplier,
            vix_multiplier=vix_multiplier,
        )

        position_usd = sizing["position_usd"]
        if position_usd < 10:
            return {"success": False, "error": f"Position too small: ${position_usd:.2f}"}

        quantity = int(position_usd / price)
        if quantity <= 0:
            return {"success": False, "error": "Zero quantity calculated"}

        # Round to 100 shares (board lot) for US stocks
        if quantity >= 100:
            quantity = (quantity // 100) * 100

        stop_loss = price * (1 - stop_loss_pct / 100)
        take_profit = price * (1 + take_profit_pct / 100)

        result = self.execute(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        result["sizing"] = sizing
        return result

    # ── Execution Log ───────────────────────────────────────────────────

    def get_execution_log(self, limit: int = 50) -> List[dict]:
        """Get recent execution log."""
        return [r.to_dict() for r in self._execution_log[-limit:]]

    def get_pending_orders(self) -> list:
        """Get all open orders from broker."""
        if not self.broker:
            return []
        try:
            return self.broker.get_open_orders()
        except Exception as e:
            logger.error("Failed to get open orders: %s", e)
            return []

    def cancel_all_orders(self):
        """Cancel all pending orders."""
        if not self.broker:
            return
        try:
            open_orders = self.broker.get_open_orders()
            for order in open_orders:
                if order.order_id:
                    self.broker.cancel_order(order.order_id)
                    logger.info("Cancelled order %s", order.order_id)
        except Exception as e:
            logger.error("Failed to cancel orders: %s", e)
