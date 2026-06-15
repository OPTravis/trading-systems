"""
Smart Order Module — Pure calculation utilities for exchange filters,
quantity precision, and ATR-based SL/TP price computation.

This module does NOT execute trades. All order placement is handled by
TradeExecutor (trade_executor.py), which calls SmartOrder for filter
fetching and price/quantity calculations.

Responsibilities:
  - get_symbol_filters(): fetch LOT_SIZE / PRICE_FILTER / MIN_NOTIONAL from exchange
  - apply_qty_precision(): floor quantity to stepSize, enforce minQty/maxQty
  - calculate_sl_tp(): ATR-based SL/TP price calculation (static, pure)
  - calculate_sl_tp_pct(): percentage-based SL/TP price calculation (static, pure)

Removed in P2-7 (module overlap elimination):
  - place_buy_with_sl_tp():   dead code — superseded by TradeExecutor.execute_auto_trade()
  - calculate_position_size(): dead code — superseded by KellyPositionSizer in TradeExecutor
  - get_current_positions():   dead code — duplicated by count_active_positions() in TradeExecutor
  - get_price():               dead code — only used by removed place_buy_with_sl_tp()
  - get_usdt_balance():        dead code — only used by removed calculate_position_size()
"""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient


logger = logging.getLogger(__name__)


class SmartOrder:
    """Pure calculation module for exchange filters and SL/TP price computation.

    This class does NOT execute trades. It provides:
      1. Exchange filter data (LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL)
      2. Quantity precision utilities (floor to stepSize)
      3. SL/TP price calculation (ATR-based and percentage-based modes)

    All trade execution is handled by TradeExecutor, which delegates
    filter fetching and price calculations to this module.
    """

    # ATR-based SL/TP multipliers
    SL_ATR_MULTIPLIER = 2.0  # SL = entry - 2*ATR
    TP1_ATR_MULTIPLIER = 2.0  # TP1 = entry + 2*ATR (1:1 risk/reward)
    TP2_ATR_MULTIPLIER = 4.0  # TP2 = entry + 4*ATR (1:2)
    TP3_ATR_MULTIPLIER = 6.0  # TP3 = entry + 6*ATR (1:3)

    # TP sizing (percentage of position to close at each TP)
    TP1_SIZE_PCT = 40
    TP2_SIZE_PCT = 40
    TP3_SIZE_PCT = 20

    # SL/TP distance constraints (ATR-based, not percentage-clamped)
    MIN_SPREAD_ATR_MULT = 0.5  # minimum distance between levels = 0.5 * ATR
    MAX_SL_ATR_MULT = 6.0  # cap SL distance at 6 * ATR (prevents excessive risk)
    # No TP cap — let profits scale naturally with volatility

    def __init__(self, client: "ExchangeClient"):
        """Initialize with an exchange client for filter fetching.

        The client is used ONLY for get_symbol_filters() (data retrieval).
        No trade-execution methods exist in this class.
        """
        self.client = client
        self._symbol_info_cache: Dict[str, Dict] = {}

    def get_symbol_filters(self, symbol: str) -> Optional[Dict]:
        """Get LOT_SIZE and PRICE_FILTER for a symbol (cached).

        Returns a dict with keys: minQty, maxQty, stepSize, qty_decimals,
        minPrice, tickSize, price_decimals, minNotional (if available).

        Called by:
          - TradeExecutor.execute_auto_trade() for qty precision
          - scripts/trailing_tp.py for TP order adjustments
        """
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        try:
            exchange_info = self.client.get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol), None
            )
            if not sym_info:
                return None

            filters = {}
            for f in sym_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    filters["minQty"] = float(f["minQty"])
                    filters["maxQty"] = float(f["maxQty"])
                    filters["stepSize"] = float(f["stepSize"])
                    # Calculate quantity decimals from stepSize
                    step_str = f["stepSize"].rstrip("0").rstrip(".")
                    filters["qty_decimals"] = (
                        len(step_str.split(".")[-1]) if "." in step_str else 0
                    )
                elif f["filterType"] == "PRICE_FILTER":
                    filters["minPrice"] = float(f["minPrice"])
                    filters["tickSize"] = float(f["tickSize"])
                    tick_str = f["tickSize"].rstrip("0").rstrip(".")
                    filters["price_decimals"] = (
                        len(tick_str.split(".")[-1]) if "." in tick_str else 0
                    )
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    filters["minNotional"] = float(f["minNotional"])

            self._symbol_info_cache[symbol] = filters
            return filters
        except Exception as e:
            logger.error(f"Failed to get symbol info for {symbol}: {e}")
            return None

    def apply_qty_precision(self, qty: float, filters: Dict) -> float:
        """Apply LOT_SIZE precision: floor to stepSize, enforce minQty/maxQty.

        Uses Decimal for precision safety (never rounds up — rounding up
        could exceed available balance).

        Pure calculation — does not execute any API calls.
        """
        from decimal import Decimal, InvalidOperation

        step_size = filters.get("stepSize", 0.001)
        min_qty = filters.get("minQty", 0.0)
        max_qty = filters.get("maxQty", 999999999.0)
        qty_decimals = filters.get("qty_decimals", 4)

        try:
            # Floor to step size using Decimal for precision safety (never round up — could exceed balance)
            d_qty = Decimal(str(qty))
            d_step = Decimal(str(step_size))
            if d_step <= 0:
                return 0.0
            qty = float((d_qty // d_step) * d_step)
            qty = round(qty, qty_decimals)
        except (InvalidOperation, ValueError):
            logger.error(
                "Quantity calculation failed for qty=%s step=%s — returning 0.0",
                qty,
                step_size,
                exc_info=True,
            )
            return 0.0

        # Enforce min/max
        if qty < min_qty:
            return 0.0
        if qty > max_qty:
            d_max = Decimal(str(max_qty))
            qty = float((d_max // d_step) * d_step)
            qty = round(qty, qty_decimals)

        return qty

    @staticmethod
    def calculate_sl_tp(price: float, atr: float) -> Dict[str, float]:
        """Calculate dynamic SL and TP levels based on ATR.

        Uses ATR price-distance directly with minimum spread enforcement
        instead of percentage clamping that collapses levels at low volatility.

        Pure static calculation — no API calls, no side effects.

        Returns dict with sl, tp1, tp2, tp3 prices and sizes.
        """
        atr_pct = (atr / price) * 100

        # Minimum distance between any two levels (in price units)
        min_spread = SmartOrder.MIN_SPREAD_ATR_MULT * atr

        # --- SL distance: cap at MAX_SL_ATR_MULT to limit downside risk ---
        sl_distance = SmartOrder.SL_ATR_MULTIPLIER * atr
        max_sl_distance = SmartOrder.MAX_SL_ATR_MULT * atr
        sl_distance = min(sl_distance, max_sl_distance)
        # Ensure SL is at least min_spread away from entry
        sl_distance = max(sl_distance, min_spread)
        sl_price = price - sl_distance

        # --- TP distances: use raw ATR multipliers, enforce monotonic spread ---
        tp1_distance = SmartOrder.TP1_ATR_MULTIPLIER * atr
        tp2_distance = SmartOrder.TP2_ATR_MULTIPLIER * atr
        tp3_distance = SmartOrder.TP3_ATR_MULTIPLIER * atr

        # Enforce minimum spread: each TP must be at least min_spread
        # above the previous level (or above entry for TP1)
        tp1_distance = max(tp1_distance, min_spread)
        tp2_distance = max(tp2_distance, tp1_distance + min_spread)
        tp3_distance = max(tp3_distance, tp2_distance + min_spread)

        tp1_price = price + tp1_distance
        tp2_price = price + tp2_distance
        tp3_price = price + tp3_distance

        # Convert back to percentages for reporting / risk-reward display
        sl_pct = (sl_distance / price) * 100
        tp1_pct = (tp1_distance / price) * 100
        tp2_pct = (tp2_distance / price) * 100
        tp3_pct = (tp3_distance / price) * 100

        return {
            "sl_price": round(sl_price, 6),
            "sl_pct": round(sl_pct, 2),
            "tp1_price": round(tp1_price, 6),
            "tp1_pct": round(tp1_pct, 2),
            "tp1_size_pct": SmartOrder.TP1_SIZE_PCT,
            "tp2_price": round(tp2_price, 6),
            "tp2_pct": round(tp2_pct, 2),
            "tp2_size_pct": SmartOrder.TP2_SIZE_PCT,
            "tp3_price": round(tp3_price, 6),
            "tp3_pct": round(tp3_pct, 2),
            "tp3_size_pct": SmartOrder.TP3_SIZE_PCT,
            "atr": round(atr, 6),
            "atr_pct": round(atr_pct, 2),
        }

    @staticmethod
    def calculate_sl_tp_pct(
        price: float,
        stop_loss_pct: float,
        tp_pcts: List[float],
        price_precision: int = 6,
    ) -> Dict[str, float]:
        """Calculate SL/TP prices using percentage-based approach.

        This is the percentage-based counterpart to calculate_sl_tp().
        TradeExecutor uses this mode (strategies define SL/TP as percentages),
        while calculate_sl_tp() uses ATR-based distances.

        Pure static calculation — no API calls, no side effects.

        Args:
            price: Entry price
            stop_loss_pct: Stop loss percentage (e.g. 5.0 means -5%)
            tp_pcts: List of take-profit percentages (e.g. [10.0, 15.0, 20.0])
            price_precision: Decimal places for rounding (from exchange PRICE_FILTER)

        Returns:
            Dict with sl_price, sl_pct, and tp_levels list of {price, pct}.
        """
        sl_price = round(price * (1 - stop_loss_pct / 100), price_precision)

        tp_levels = []
        for tp_pct in tp_pcts:
            tp_price = round(price * (1 + tp_pct / 100), price_precision)
            tp_levels.append({"price": tp_price, "pct": tp_pct})

        return {
            "sl_price": sl_price,
            "sl_pct": stop_loss_pct,
            "tp_levels": tp_levels,
        }
