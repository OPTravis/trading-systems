"""
Smart Order Module - Intelligent order placement with dynamic SL/TP
Based on ATR (Average True Range) for adaptive risk management.
"""

import json
import logging
from typing import Dict, Optional, Tuple, List
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from src.binance_client import BinanceClient  # runtime fallback


logger = logging.getLogger(__name__)


class SmartOrder:
    """Intelligent order placement with dynamic SL/TP."""

    # Risk limits
    MAX_POSITIONS = 5
    MAX_SINGLE_POSITION_PCT = 15  # max 15% of USDT per trade
    MAX_TOTAL_EXPOSURE_PCT = 70
    CASH_RESERVE_PCT = 30

    # ATR-based SL/TP multipliers
    SL_ATR_MULTIPLIER = 2.0      # SL = entry - 2*ATR
    TP1_ATR_MULTIPLIER = 2.0     # TP1 = entry + 2*ATR (1:1 risk/reward)
    TP2_ATR_MULTIPLIER = 4.0     # TP2 = entry + 4*ATR (1:2)
    TP3_ATR_MULTIPLIER = 6.0     # TP3 = entry + 6*ATR (1:3)

    # TP sizing (percentage of position to close at each TP)
    TP1_SIZE_PCT = 40
    TP2_SIZE_PCT = 40
    TP3_SIZE_PCT = 20

    # SL/TP distance constraints (ATR-based, not percentage-clamped)
    MIN_SPREAD_ATR_MULT = 0.5  # minimum distance between levels = 0.5 * ATR
    MAX_SL_ATR_MULT = 6.0      # cap SL distance at 6 * ATR (prevents excessive risk)
    # No TP cap — let profits scale naturally with volatility

    def __init__(self, client: 'ExchangeClient'):
        self.client = client
        self._symbol_info_cache: Dict[str, Dict] = {}

    def get_usdt_balance(self) -> float:
        return self.client.get_free_balance('USDT')

    def get_price(self, symbol: str) -> Optional[float]:
        """Get current price via 24hr stats."""
        try:
            stats = self.client.get_24hr_stats(symbol)
            if isinstance(stats, dict) and stats.get('last_price'):
                return float(stats['last_price'])
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
        return None

    def get_symbol_filters(self, symbol: str) -> Optional[Dict]:
        """Get LOT_SIZE and PRICE_FILTER for a symbol (cached)."""
        if symbol in self._symbol_info_cache:
            return self._symbol_info_cache[symbol]

        try:
            exchange_info = self.client.get_exchange_info()
            sym_info = next(
                (s for s in exchange_info['symbols'] if s['symbol'] == symbol),
                None
            )
            if not sym_info:
                return None

            filters = {}
            for f in sym_info.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    filters['minQty'] = float(f['minQty'])
                    filters['maxQty'] = float(f['maxQty'])
                    filters['stepSize'] = float(f['stepSize'])
                    # Calculate quantity decimals from stepSize
                    step_str = f['stepSize'].rstrip('0').rstrip('.')
                    filters['qty_decimals'] = len(step_str.split('.')[-1]) if '.' in step_str else 0
                elif f['filterType'] == 'PRICE_FILTER':
                    filters['minPrice'] = float(f['minPrice'])
                    filters['tickSize'] = float(f['tickSize'])
                    tick_str = f['tickSize'].rstrip('0').rstrip('.')
                    filters['price_decimals'] = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
                elif f['filterType'] in ('MIN_NOTIONAL', 'NOTIONAL'):
                    filters['minNotional'] = float(f['minNotional'])

            self._symbol_info_cache[symbol] = filters
            return filters
        except Exception as e:
            logger.error(f"Failed to get symbol info for {symbol}: {e}")
            return None

    def apply_qty_precision(self, qty: float, filters: Dict) -> float:
        """Apply LOT_SIZE precision: floor to stepSize, enforce minQty/maxQty."""
        from decimal import Decimal, InvalidOperation
        step_size = filters.get('stepSize', 0.001)
        min_qty = filters.get('minQty', 0.0)
        max_qty = filters.get('maxQty', 999999999.0)
        qty_decimals = filters.get('qty_decimals', 4)

        try:
            # Floor to step size using Decimal for precision safety (never round up — could exceed balance)
            d_qty = Decimal(str(qty))
            d_step = Decimal(str(step_size))
            if d_step <= 0:
                return 0.0
            qty = float((d_qty // d_step) * d_step)
            qty = round(qty, qty_decimals)
        except (InvalidOperation, ValueError):
            logger.error("Quantity calculation failed for qty=%s step=%s — returning 0.0", qty, step_size, exc_info=True)
            return 0.0

        # Enforce min/max
        if qty < min_qty:
            return 0.0
        if qty > max_qty:
            d_max = Decimal(str(max_qty))
            qty = float((d_max // d_step) * d_step)
            qty = round(qty, qty_decimals)

        return qty

    def get_current_positions(self) -> List[Dict]:
        """Get all non-USDT positions with qty > 0.
        Uses batch ticker fetch (1 API call) instead of per-asset calls (M7 fix).
        """
        account = self.client.get_account()
        positions = []
        if isinstance(account, dict):
            # Batch fetch all prices in one call
            price_map = {}
            try:
                all_tickers = self.client.get_24hr_stats()
                if isinstance(all_tickers, list):
                    price_map = {
                        t["symbol"]: float(t.get("last_price", 0))
                        for t in all_tickers
                        if "symbol" in t
                    }
            except Exception as e:
                logger.debug(f"get_current_positions: batch ticker fetch failed: {e}")

            for balance in account.get('balances', []):
                asset = balance.get('asset', '')
                free = float(balance.get('free', 0))
                locked = float(balance.get('locked', 0))
                total = free + locked
                if total > 0 and asset != 'USDT':
                    price = price_map.get(asset + 'USDT', 0)
                    if price and total * price >= 5.0:  # exclude dust <$5
                        positions.append({
                            'symbol': asset,
                            'quantity': total,
                            'free': free,
                            'locked': locked,
                            'price': price,
                            'value_usdt': total * price
                        })
        return positions

    def calculate_position_size(
        self, symbol: str, price: float, score: float, volatility_pct: float
    ) -> Tuple[float, float]:
        """
        Calculate position size based on risk management rules.
        Returns (quantity, usdt_amount).
        """
        balance = self.get_usdt_balance()
        positions = self.get_current_positions()

        # Check position count
        if len(positions) >= self.MAX_POSITIONS:
            return 0, 0

        # Calculate total exposure
        total_exposure = sum(p['value_usdt'] for p in positions)
        max_exposure = balance * (self.MAX_TOTAL_EXPOSURE_PCT / 100)

        if total_exposure >= max_exposure:
            return 0, 0

        # Available for this trade
        available = min(
            balance * (self.MAX_SINGLE_POSITION_PCT / 100),  # single position limit
            max_exposure - total_exposure,                     # remaining exposure
            balance * (self.CASH_RESERVE_PCT / 100)           # cash reserve protection
        )

        if available < 10:  # minimum $10 trade
            return 0, 0

        # Adjust for score (higher score = larger position)
        score_factor = min(score / 70, 1.0)  # normalize to 0-1, capped
        if score < 55:
            score_factor *= 0.5
        elif score < 65:
            score_factor *= 0.75

        # Adjust for volatility (higher vol = smaller position)
        if volatility_pct > 10:
            vol_factor = 0.5
        elif volatility_pct > 7:
            vol_factor = 0.7
        elif volatility_pct > 4:
            vol_factor = 0.85
        else:
            vol_factor = 1.0

        usdt_amount = available * score_factor * vol_factor

        # Calculate raw quantity BEFORE filter check (C1 fix)
        quantity = usdt_amount / price

        # Get symbol precision from filters
        filters = self.get_symbol_filters(symbol + 'USDT')
        if not filters:
            logger.warning(f"Cannot get filters for {symbol}, using defaults")
            return quantity, usdt_amount

        # Apply LOT_SIZE precision with minQty enforcement
        quantity = self.apply_qty_precision(quantity, filters)
        if quantity <= 0:
            return 0, 0

        # Check MIN_NOTIONAL
        min_notional = filters.get('minNotional', 10)
        if quantity * price < min_notional:
            return 0, 0

        # Recalculate usdt_amount after precision adjustment
        usdt_amount = quantity * price
        return quantity, usdt_amount

    @staticmethod
    def calculate_sl_tp(
        price: float, atr: float
    ) -> Dict[str, float]:
        """
        Calculate dynamic SL and TP levels based on ATR.
        Uses ATR price-distance directly with minimum spread enforcement
        instead of percentage clamping that collapses levels at low volatility.
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
            'sl_price': round(sl_price, 6),
            'sl_pct': round(sl_pct, 2),
            'tp1_price': round(tp1_price, 6),
            'tp1_pct': round(tp1_pct, 2),
            'tp1_size_pct': SmartOrder.TP1_SIZE_PCT,
            'tp2_price': round(tp2_price, 6),
            'tp2_pct': round(tp2_pct, 2),
            'tp2_size_pct': SmartOrder.TP2_SIZE_PCT,
            'tp3_price': round(tp3_price, 6),
            'tp3_pct': round(tp3_pct, 2),
            'tp3_size_pct': SmartOrder.TP3_SIZE_PCT,
            'atr': round(atr, 6),
            'atr_pct': round(atr_pct, 2),
        }

    def place_buy_with_sl_tp(
        self,
        symbol: str,
        score: float,
        volatility_pct: float,
        atr: float,
        klines: List[Dict]
    ) -> Dict:
        """
        Full flow: calculate size → market buy → set SL and TP orders.
        Returns result dict with order details.
        """
        full_symbol = symbol + 'USDT'
        price = self.get_price(full_symbol)

        if not price:
            return {'success': False, 'reason': f'Cannot get price for {symbol}'}

        # Check if already have position
        positions = self.get_current_positions()
        for p in positions:
            if p['symbol'] == symbol:
                return {'success': False, 'reason': f'Already holding {symbol}'}

        # Calculate position size
        quantity, usdt_amount = self.calculate_position_size(
            symbol, price, score, volatility_pct
        )

        if quantity <= 0:
            return {'success': False, 'reason': 'Position size too small or limits reached'}

        # Calculate SL/TP
        sl_tp = self.calculate_sl_tp(price, atr)

        # Execute market buy
        buy_result = self.client.place_market_buy(full_symbol, quantity)

        if not buy_result:
            return {'success': False, 'reason': f'Market buy failed for {symbol}'}

        # Get actual fill details
        fills = buy_result.get('fills', [])
        if fills:
            filled_price = float(fills[-1].get('price', price))
            filled_qty = float(buy_result.get('executedQty', quantity))
        else:
            filled_price = price
            filled_qty = float(buy_result.get('executedQty', quantity))

        # Recalculate SL/TP based on actual fill price
        sl_tp = self.calculate_sl_tp(filled_price, atr)

        # IMPORTANT: Place SL FIRST, then TP.
        # If TP orders are placed first, they lock the asset balance and
        # the subsequent SL order fails with "insufficient balance".

        # SL quantity = remaining after TP1 and TP2 fills.
        # TP3 is NOT placed as a separate limit order — the remaining 20%
        # "runner" portion is protected by the SL.
        # Apply LOT_SIZE precision to all order quantities
        # NOTE: We need to re-fetch filters for the filled_symbol since
        #       the earlier `filters` variable may not be in scope if
        #       calculate_position_size returned early with defaults.
        filled_symbol_filters = self.get_symbol_filters(full_symbol) or {}
        if not filled_symbol_filters:
            logger.warning(f"No filters for {full_symbol}, using defaults for order sizing")
            filled_symbol_filters = {'stepSize': 0.001, 'minQty': 0.0, 'maxQty': 999999999.0, 'qty_decimals': 4}

        tp1_qty = self.apply_qty_precision(
            filled_qty * (sl_tp['tp1_size_pct'] / 100), filled_symbol_filters
        )
        tp2_qty = self.apply_qty_precision(
            filled_qty * (sl_tp['tp2_size_pct'] / 100), filled_symbol_filters
        )
        remaining_qty = self.apply_qty_precision(
            filled_qty - tp1_qty - tp2_qty, filled_symbol_filters
        )
        sl_qty = max(remaining_qty, 0)

        # FIX A: Enforce minNotional on each child order to avoid API rejection.
        # If an order's notional (qty * price) is below the symbol's minNotional,
        # skip placing it rather than letting Binance reject it.
        min_notional = filled_symbol_filters.get('minNotional', 10.0)
        if tp1_qty > 0 and tp1_qty * sl_tp['tp1_price'] < min_notional:
            logger.warning(
                f"TP1 skipped for {symbol}: notional {tp1_qty * sl_tp['tp1_price']:.2f} < minNotional {min_notional}"
            )
            tp1_qty = 0.0
        if tp2_qty > 0 and tp2_qty * sl_tp['tp2_price'] < min_notional:
            logger.warning(
                f"TP2 skipped for {symbol}: notional {tp2_qty * sl_tp['tp2_price']:.2f} < minNotional {min_notional}"
            )
            tp2_qty = 0.0
        if sl_qty > 0 and sl_qty * sl_tp['sl_price'] < min_notional:
            logger.warning(
                f"SL skipped for {symbol}: notional {sl_qty * sl_tp['sl_price']:.2f} < minNotional {min_notional}"
            )
            sl_qty = 0.0

        # Validate total qty doesn't exceed filled qty (precision can cause drift)
        total_order_qty = tp1_qty + tp2_qty + sl_qty
        if total_order_qty > filled_qty:
            logger.warning(
                f"Order qty drift: total={total_order_qty} > filled={filled_qty} for {symbol}. "
                f"Capping SL qty."
            )
            sl_qty = max(filled_qty - tp1_qty - tp2_qty, 0)
            sl_qty = self.apply_qty_precision(sl_qty, filled_symbol_filters)

        # FIX B: If sl_qty becomes 0 after minNotional/qty-drift fixes but we still
        # have unallocated quantity, try to merge the remainder into the last TP
        # so the position is not left completely unprotected/unclosed.
        if sl_qty <= 0 and filled_qty > tp1_qty + tp2_qty:
            remainder = filled_qty - tp1_qty - tp2_qty
            remainder = self.apply_qty_precision(remainder, filled_symbol_filters)
            if remainder > 0:
                # Try to add to TP2 first (larger chunk), then TP1
                if tp2_qty > 0:
                    tp2_qty = self.apply_qty_precision(tp2_qty + remainder, filled_symbol_filters)
                elif tp1_qty > 0:
                    tp1_qty = self.apply_qty_precision(tp1_qty + remainder, filled_symbol_filters)
                else:
                    # Nothing to merge into — position will have no exit orders
                    logger.error(
                        f"CRITICAL: No viable exit orders for {symbol}. "
                        f"Position of {filled_qty} has no TP/SL due to dust/minNotional constraints."
                    )

        # Re-check minNotional after merging
        if tp1_qty > 0 and tp1_qty * sl_tp['tp1_price'] < min_notional:
            logger.warning(f"TP1 merged qty still below minNotional, zeroing for {symbol}")
            tp1_qty = 0.0
        if tp2_qty > 0 and tp2_qty * sl_tp['tp2_price'] < min_notional:
            logger.warning(f"TP2 merged qty still below minNotional, zeroing for {symbol}")
            tp2_qty = 0.0

        # Place SL order FIRST
        # CRITICAL FIX: Use STOP_LOSS_LIMIT instead of STOP_LOSS because
        # Binance SPOT API does not support STOP_LOSS (market trigger) orders.
        # STOP_LOSS_LIMIT is the only stop-loss order type available on SPOT.
        sl_result = None
        if sl_qty > 0:
            # Provide a small slippage buffer for the limit price (0.5% below stop)
            sl_limit_price = sl_tp['sl_price'] * 0.995
            # Ensure limit price respects tick size
            if filled_symbol_filters.get('tickSize'):
                from decimal import Decimal
                tick = Decimal(str(filled_symbol_filters['tickSize']))
                d_limit = Decimal(str(sl_limit_price))
                sl_limit_price = float((d_limit // tick) * tick)
            sl_result = self.client.place_stop_loss_limit(
                full_symbol, sl_qty, sl_limit_price, sl_tp['sl_price']
            )

        # Then place TP orders
        tp1_result = None
        if tp1_qty > 0:
            tp1_result = self.client.place_limit_sell(full_symbol, tp1_qty, sl_tp['tp1_price'])

        tp2_result = None
        if tp2_qty > 0:
            tp2_result = self.client.place_limit_sell(full_symbol, tp2_qty, sl_tp['tp2_price'])

        # TP3 — runner portion (20% of position, 6× ATR target).
        # Previously tracked but not placed; now placed as limit sell.
        tp3_qty = self.apply_qty_precision(
            filled_qty * (sl_tp['tp3_size_pct'] / 100), filled_symbol_filters
        )
        tp3_result = None
        if tp3_qty > 0:
            tp3_notional = tp3_qty * sl_tp['tp3_price']
            if tp3_notional >= min_notional:
                tp3_result = self.client.place_limit_sell(full_symbol, tp3_qty, sl_tp['tp3_price'])
                if tp3_result:
                    # Reduce SL coverage: TP3 now has its own exit order
                    sl_qty = self.apply_qty_precision(
                        max(sl_qty - tp3_qty, 0), filled_symbol_filters
                    )
                    logger.info(
                        f"TP3 placed for %s: qty=%s @ $%s (6x ATR runner)",
                        symbol, tp3_qty, round(sl_tp['tp3_price'], 6),
                    )
                else:
                    logger.warning(f"TP3 order failed for {symbol}")
            else:
                logger.info(
                    f"TP3 skipped for %s: notional $%s < minNotional $%s",
                    symbol, round(tp3_notional, 2), min_notional,
                )

        # Log warnings for failed orders
        if not sl_result:
            logger.error(f"CRITICAL: SL order failed for {symbol} — position is unprotected!")
        if not tp1_result:
            logger.warning(f"TP1 order failed for {symbol}")
        if not tp2_result:
            logger.warning(f"TP2 order failed for {symbol}")

        return {
            'success': True,
            'symbol': symbol,
            'side': 'BUY',
            'price': filled_price,
            'quantity': filled_qty,
            'usdt_value': round(filled_qty * filled_price, 2),
            'score': score,
            'sl': sl_tp['sl_price'],
            'sl_pct': sl_tp['sl_pct'],
            'tp1': sl_tp['tp1_price'],
            'tp1_pct': sl_tp['tp1_pct'],
            'tp1_qty': tp1_qty,
            'tp2': sl_tp['tp2_price'],
            'tp2_pct': sl_tp['tp2_pct'],
            'tp2_qty': tp2_qty,
            'tp3': sl_tp['tp3_price'],
            'tp3_pct': sl_tp['tp3_pct'],
            'tp3_qty': tp3_qty,
            'atr_pct': sl_tp['atr_pct'],
            'risk_reward': f"1:{round(sl_tp['tp1_pct'] / sl_tp['sl_pct'], 1)}" if sl_tp['sl_pct'] > 0 else "N/A",
            'sl_order': sl_result,
            'tp1_order': tp1_result,
            'tp2_order': tp2_result,
            'tp3_order': tp3_result,
            'buy_order': buy_result,
            'warnings': [],
        }
