"""Calculate true average entry price from Binance trade history."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_avg_entry_price(
    client, symbol: str, current_qty: Optional[float] = None
) -> Optional[float]:
    """Calculate weighted average buy price from trade history.

    Iterates through all trades, tracking net position from buys/sells.
    Returns the weighted average price of the *current* holdings.

    Args:
        client: BinanceClient instance
        symbol: Trading pair (e.g. 'ZAMAUSDT')
        current_qty: Actual current holding qty. If provided, validates
                     the calculated qty matches (within 5% tolerance).
                     If they don't match, returns None (incomplete history).

    Returns:
        Weighted average entry price, or None if cannot determine.
    """
    try:
        # FIX H2: Fetch all trades with pagination (not just 100)
        all_trades = []
        from_id = None
        while True:
            batch = client.get_my_trades(symbol=symbol, limit=100, from_id=from_id)
            if not batch:
                break
            all_trades.extend(batch)
            if len(batch) < 100:
                break
            from_id = batch[-1]["id"] + 1
            # Safety limit: max 5000 trades (50 batches)
            if len(all_trades) >= 5000:
                logger.warning(f"Trade history for {symbol} exceeds 5000, truncating")
                break
        trades = all_trades
    except Exception as e:
        logger.warning(f"Cannot fetch trades for {symbol}: {e}")
        return None

    if not trades:
        return None

    # Process trades chronologically (oldest first)
    trades.sort(key=lambda t: t.get("time", 0))

    # Track running position: list of (qty, price) lots
    lots = []

    for t in trades:
        qty = float(t.get("qty", 0))
        price = float(t.get("price", 0))
        is_buyer = t.get("isBuyer", False)

        if qty <= 0 or price <= 0:
            continue

        if is_buyer:
            lots.append((qty, price))
        else:
            # Sell: reduce lots FIFO
            remaining = qty
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                if lot_qty <= remaining:
                    lots.pop(0)
                    remaining -= lot_qty
                else:
                    lots[0] = (lot_qty - remaining, lot_price)
                    remaining = 0

    if not lots:
        return None

    # Calculate weighted average
    total_qty = sum(q for q, _ in lots)
    total_cost = sum(q * p for q, p in lots)

    if total_qty <= 0:
        return None

    # Validate against actual holdings if provided
    if current_qty is not None:
        if current_qty <= 0:
            # No position held — shouldn't have entry price
            return None
        # Use 5% tolerance for dust/rounding
        if abs(total_qty - current_qty) / current_qty > 0.05:
            logger.warning(
                f"Entry price validation failed for {symbol}: "
                f"calculated qty={total_qty:.4f} vs actual={current_qty:.4f} "
                f"(diff > 5%%). History may be incomplete."
            )
            return None

    avg_price = total_cost / total_qty
    logger.info(
        f"Entry price for {symbol}: ${avg_price:.6f} ({total_qty:.4f} units from {len(lots)} lots)"
    )
    return round(avg_price, 8)


def get_avg_entry_price_from_db(
    db, symbol: str, current_qty: Optional[float] = None
) -> Optional[float]:
    """Calculate weighted average entry from the local DB trades ledger.

    Same FIFO lot logic as get_avg_entry_price(), but sourced from our own
    trades table instead of the exchange API. Used as a fallback when exchange
    history is incomplete (bug #6: ETH qty 0.0269 vs actual 0.0027 failed the
    5% validation, and sync fell through to market_estimate which wrote the
    market price as entry — corrupting SL/TP anchors).

    Args:
        db: StateDB instance (source of the trades table)
        symbol: Trading pair, e.g. 'ETHUSDT'
        current_qty: Actual current holding qty for validation (5% tolerance)

    Returns:
        Weighted average entry price, or None if cannot determine.
    """
    try:
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT side, qty, price, timestamp FROM trades "
            "WHERE symbol = ? ORDER BY timestamp ASC",
            (symbol,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"Cannot read DB trades for {symbol}: {e}")
        return None

    if not rows:
        return None

    lots = []
    for r in rows:
        side = (r["side"] or "").upper()
        qty = float(r["qty"] or 0)
        price = float(r["price"] or 0)
        if qty <= 0 or price <= 0:
            continue
        if side == "BUY":
            lots.append((qty, price))
        elif side == "SELL":
            remaining = qty
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                if lot_qty <= remaining:
                    lots.pop(0)
                    remaining -= lot_qty
                else:
                    lots[0] = (lot_qty - remaining, lot_price)
                    remaining = 0

    if not lots:
        return None

    total_qty = sum(q for q, _ in lots)
    total_cost = sum(q * p for q, p in lots)
    if total_qty <= 0:
        return None

    if current_qty is not None and current_qty > 0:
        if abs(total_qty - current_qty) / current_qty > 0.05:
            logger.warning(
                f"DB-ledger entry validation failed for {symbol}: "
                f"ledger qty={total_qty:.4f} vs actual={current_qty:.4f} (diff > 5%)"
            )
            return None

    avg_price = total_cost / total_qty
    logger.info(
        f"DB-ledger entry price for {symbol}: ${avg_price:.6f} "
        f"({total_qty:.4f} units from {len(lots)} lots)"
    )
    return round(avg_price, 8)
