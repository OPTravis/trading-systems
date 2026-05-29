#!/usr/bin/env python3
"""
Trailing Take-Profit — dynamically adjust TP orders upward as price rises.

When a position's current price exceeds the original TP target by 50%+,
cancel the old TP limit order and place a new one higher — capturing
more profit in trending markets.

Integrated into crypto cron pipeline — called by ensure_tp_sl.py.
"""
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.binance_client import BinanceClient
from src.state_db import get_state_db
from src.smart_order import SmartOrder

logger = logging.getLogger(__name__)

# Thresholds
TRAIL_ACTIVATION_MULT = 1.5  # Only trail if price > highest TP * 1.5
TRAIL_STEP_MULT = 0.5        # Move TP by 0.5× ATR each step
MIN_TRAIL_INTERVAL = 3600    # Don't trail same TP within 1 hour
MAX_TRAIL_COUNT = 3          # P1: Max times a single TP order can be trailed


def trailing_tp_check(client: BinanceClient = None, dry_run: bool = False):
    """
    Check all open positions and adjust TP orders upward where warranted.

    Logic:
      1. Get all open limit-sell orders (TP orders)
      2. Compare current price vs each TP order price
      3. If price > TP_order_price * TRAIL_ACTIVATION_MULT:
         cancel old TP order, place new TP at current_price + TRAIL_STEP_MULT * ATR
    """
    if client is None:
        client = BinanceClient(testnet=False)

    db = get_state_db()
    so = SmartOrder(client)

    # Get all open orders (fetch per-symbol since no-symbol path is broken)
    try:
        positions = db.portfolio_get_all()
        open_orders = []
        for sym in positions:
            try:
                open_orders.extend(client.get_open_orders(sym))
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Failed to fetch open orders: {e}")
        return {"trailed": 0, "skipped": 0, "errors": [str(e)]}

    # Filter: only LIMIT SELL orders (TP orders)
    tp_orders = [
        o for o in open_orders
        if o.get("type", "").upper() in ("LIMIT", "LIMIT_MAKER") and o.get("side", "").upper() == "SELL"
    ]

    if not tp_orders:
        logger.debug("No TP orders to trail")
        return {"trailed": 0, "skipped": 0, "errors": []}

    # Get positions from portfolio for ATR context
    positions = db.portfolio_get_all()

    trailed = 0
    skipped = 0
    errors = []

    for order in tp_orders:
        symbol = order["symbol"].replace("/", "")  # ccxt "CFX/USDT" -> "CFXUSDT"
        order_id = order.get("id", order.get("orderId"))
        order_price = float(order["price"])
        order_qty = float(order.get("amount", order.get("origQty", 0)))

        # Skip if not a USDT pair
        if not symbol.endswith("USDT"):
            continue

        asset = symbol.replace("USDT", "")
        pos = positions.get(symbol, {})

        # Get current price
        try:
            current_price = client.get_ticker_price(symbol)
            if not current_price:
                errors.append(f"{symbol}: price fetch failed")
                continue
        except Exception as e:
            logger.warning(f"Cannot get price for {symbol}: {e}")
            errors.append(f"{symbol}: price fetch failed")
            continue

        # Check activation: price must be well past the TP target
        if current_price <= order_price * TRAIL_ACTIVATION_MULT:
            skipped += 1
            continue

        # Check cooldown (avoid thrashing)
        cooldown_key = f"trailing_tp:last:{symbol}:{order_id}"
        last_trail = db.kv_get(cooldown_key, 0)
        if time.time() - last_trail < MIN_TRAIL_INTERVAL:
            logger.debug(f"{symbol}: TP trail cooldown active ({time.time() - last_trail:.0f}s ago)")
            skipped += 1
            continue

        # P1: Check max trail count — stop trailing after MAX_TRAIL_COUNT adjustments
        trail_count_key = f"trailing_tp:count:{symbol}:{order_id}"
        trail_count = db.kv_get(trail_count_key, 0)
        if trail_count >= MAX_TRAIL_COUNT:
            logger.debug(f"{symbol}: TP trail count limit reached ({trail_count}/{MAX_TRAIL_COUNT})")
            skipped += 1
            continue

        # Calculate ATR for step size
        try:
            klines = client.get_klines(symbol, "1h", limit=14)
            atr = _calc_atr(klines) if klines else current_price * 0.02
        except Exception:
            atr = current_price * 0.02  # 2% fallback

        # New TP price: current + step
        new_tp_price = current_price + TRAIL_STEP_MULT * atr

        if dry_run:
            logger.info(
                f"[DRY RUN] Would trail {symbol}: "
                f"TP ${order_price:.6f} → ${new_tp_price:.6f} "
                f"(qty={order_qty}, price_above={current_price/order_price:.2f}×)"
            )
            continue

        # Cancel old TP order
        try:
            cancel_result = client.cancel_order(symbol=symbol, order_id=order_id)
            logger.info(f"Cancelled old TP for {symbol}: order_id={order_id}")
        except Exception as e:
            logger.error(f"Failed to cancel TP {order_id} for {symbol}: {e}")
            errors.append(f"{symbol}: cancel TP failed: {e}")
            continue

        # Place new TP order
        try:
            filters = so.get_symbol_filters(symbol)
            if filters:
                from decimal import Decimal
                step = Decimal(str(filters.get("stepSize", 1)))
                tick = Decimal(str(filters.get("tickSize", 0.01)))
                d_qty = Decimal(str(order_qty))
                d_price = Decimal(str(new_tp_price))
                order_qty = float((d_qty // step) * step)
                new_tp_price = float((d_price // tick) * tick)

            new_order = client.place_limit_sell(
                symbol=symbol,
                quantity=order_qty,
                price=new_tp_price,
            )
            logger.info(
                f"Trailed TP for {symbol}: "
                f"${order_price:.6f} → ${new_tp_price:.6f} "
                f"(gain: +{new_tp_price/order_price - 1:.1%})"
            )
            db.kv_set(cooldown_key, time.time())
            db.kv_set(trail_count_key, trail_count + 1)
            trailed += 1
        except Exception as e:
            logger.error(f"Failed to place new TP for {symbol}: {e}")
            errors.append(f"{symbol}: place new TP failed: {e}")
            # Try to re-place original TP to avoid leaving position unprotected
            try:
                client.place_limit_sell(symbol=symbol, quantity=order_qty, price=order_price)
                logger.warning(f"Re-placed original TP for {symbol} after trail failure")
            except Exception:
                logger.critical(f"UNPROTECTED: {symbol} has no TP order! Manual intervention needed.")
                errors.append(f"{symbol}: UNPROTECTED — no TP after trail failure")

    return {
        "trailed": trailed,
        "skipped": skipped,
        "total_tp_orders": len(tp_orders),
        "errors": errors,
    }


def _calc_atr(klines: list, period: int = 14) -> float:
    """Calculate ATR from kline data."""
    if len(klines) < 2:
        return 0.0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs[-period:]) / min(period, len(trs))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("=== Trailing Take-Profit Check ===")
    result = trailing_tp_check(dry_run="--dry-run" in sys.argv)
    print(f"Trailed: {result['trailed']} | Skipped: {result['skipped']} | Total: {result['total_tp_orders']}")
    if result["errors"]:
        for e in result["errors"]:
            print(f"  ⚠️ {e}")
