"""
TWAP / VWAP Order Splitter

Splits large orders into smaller slices to reduce market impact:
  - TWAP: equal time-weighted slices
  - VWAP: slices proportional to historical volume profile

Usage:
    from src.twap_vwap import plan_twap, execute_twap, plan_vwap, execute_vwap, should_use_twap
"""

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

def _order_qty(o):
    """Get order quantity from either Binance SDK ('origQty') or ccxt ('amount')."""
    return float(o.get('origQty') or o.get('amount') or 0)

def _order_id(o):
    """Get order ID from either Binance SDK ('orderId') or ccxt ('id')."""
    return o.get('orderId') or o.get('id')

def _is_stop_order(o):
    """Check if order is a stop/stop-loss order (case-insensitive for ccxt compat)."""
    t = o.get('type', '')
    return 'STOP' in t.upper() or 'stop' in t.lower()


# ---------------------------------------------------------------------------
# Minimum order filters (Binance defaults; overridden by exchange info)
# ---------------------------------------------------------------------------
DEFAULT_MIN_QTY = 0.001
DEFAULT_MIN_NOTIONAL = 5.0

# Price offset for limit orders (0.05%)
PRICE_OFFSET_PCT = 0.0005


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def should_use_twap(total_value_usdt: float, threshold: float = 100.0) -> bool:
    """Return True when order value exceeds *threshold* USDT (default $100)."""
    return total_value_usdt >= threshold


# ---------------------------------------------------------------------------
# TWAP
# ---------------------------------------------------------------------------


def plan_twap(
    total_qty: float,
    duration_minutes: int = 10,
    num_slices: int = 5,
) -> List[Dict[str, Any]]:
    """Split *total_qty* into *num_slices* equal parts spread over *duration_minutes*.

    Returns a list of ``{'qty': float, 'delay_seconds': int}`` dicts.
    The last slice gets any rounding remainder.
    """
    if num_slices < 1:
        raise ValueError("num_slices must be >= 1")
    if duration_minutes < 1:
        raise ValueError("duration_minutes must be >= 1")

    interval = (duration_minutes * 60) // num_slices
    base_qty = total_qty / num_slices

    slices: List[Dict[str, Any]] = []
    accumulated = 0.0
    for i in range(num_slices):
        if i == num_slices - 1:
            qty = total_qty - accumulated
        else:
            qty = base_qty
            accumulated += base_qty
        slices.append(
            {
                "qty": round(qty, 8),
                "delay_seconds": interval * i,
            }
        )
    return slices


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------


def plan_vwap(
    total_qty: float,
    symbol: str = "BTCUSDT",
    duration_minutes: int = 10,
    client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Distribute *total_qty* proportional to historical hourly volume.

    When *client* is None or fails to return klines, falls back to equal
    distribution (TWAP-like).
    """
    num_slices = _infer_num_slices(duration_minutes)
    volumes = _get_hourly_volumes(client, symbol, count=num_slices)

    if volumes is None or sum(volumes) == 0:
        # Fallback: equal distribution
        return plan_twap(total_qty, duration_minutes, num_slices)

    total_vol = sum(volumes)
    interval = (duration_minutes * 60) // num_slices

    slices: List[Dict[str, Any]] = []
    for i in range(num_slices):
        fraction = volumes[i] / total_vol
        qty = total_qty * fraction
        slices.append(
            {
                "qty": round(qty, 8),
                "delay_seconds": interval * i,
            }
        )
    # Fix rounding: adjust last slice so total equals total_qty exactly
    current_total = round(sum(s["qty"] for s in slices), 8)
    if slices:
        slices[-1]["qty"] = round(slices[-1]["qty"] + (total_qty - current_total), 8)
    return slices


def _infer_num_slices(duration_minutes: int) -> int:
    """One slice per minute, capped at reasonable bounds."""
    return max(2, min(duration_minutes, 60))


def _get_hourly_volumes(
    client: Any, symbol: str, count: int = 5
) -> Optional[List[float]]:
    """Fetch recent 1 h klines and return list of quote volumes."""
    if client is None:
        return None
    try:
        klines = client.get_klines(symbol, "1h", limit=count + 2)
        if not klines:
            return None
        # quote_volume is the USDT-denominated volume (needed for VWAP)
        volumes = [float(k["quote_volume"]) for k in klines[-count:]]
        return volumes
    except Exception:
        logger.error("Failed to fetch VWAP volumes for %s", symbol, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _get_symbol_filters(client: Any, symbol: str) -> Dict[str, Any]:
    """Extract minQty and minNotional from exchange info."""
    try:
        info = client.get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                filters = {}
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        filters["minQty"] = float(f["minQty"])
                    elif f["filterType"] == "NOTIONAL":
                        filters["minNotional"] = float(
                            f.get(
                                "minNotional", f.get("notional", DEFAULT_MIN_NOTIONAL)
                            )
                        )
                return filters
    except Exception:
        logger.error(
            "Failed to get exchange symbol filters for %s", symbol, exc_info=True
        )
    return {"minQty": DEFAULT_MIN_QTY, "minNotional": DEFAULT_MIN_NOTIONAL}


def _check_min_order(qty: float, price: float, filters: Dict[str, Any]) -> bool:
    """Return True if the order meets minimum size requirements."""
    min_qty = filters.get("minQty", DEFAULT_MIN_QTY)
    min_notional = filters.get("minNotional", DEFAULT_MIN_NOTIONAL)
    if qty < min_qty:
        return False
    if qty * price < min_notional:
        return False
    return True


def _place_limit_slice(
    client: Any,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """Place a single limit order at price ± offset. Returns order dict."""
    if side.upper() == "BUY":
        fill_price = round(price * (1 + PRICE_OFFSET_PCT), 8)
        if dry_run:
            return {"orderId": -1, "price": fill_price, "qty": qty, "status": "DRY_RUN"}
        return client.place_limit_buy(symbol, qty, fill_price)
    else:
        fill_price = round(price * (1 - PRICE_OFFSET_PCT), 8)
        if dry_run:
            return {"orderId": -1, "price": fill_price, "qty": qty, "status": "DRY_RUN"}
        return client.place_limit_sell(symbol, qty, fill_price)


def _execute_slices(
    client: Any,
    symbol: str,
    side: str,
    slices: List[Dict[str, Any]],
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Walk through slices with delays, placing orders and tracking slippage."""
    filters = _get_symbol_filters(client, symbol)
    results: List[Dict[str, Any]] = []

    for idx, sl in enumerate(slices):
        qty = sl["qty"]
        delay = sl["delay_seconds"]

        if delay > 0 and not dry_run:
            time.sleep(delay)

        # Get current price
        if dry_run:
            price = 100.0  # placeholder
        else:
            price = client.get_ticker_price(symbol)

        # Min-order check
        if not _check_min_order(qty, price, filters):
            results.append(
                {
                    "slice": idx,
                    "qty": qty,
                    "expected_price": price,
                    "actual_price": None,
                    "slippage_pct": None,
                    "status": "SKIPPED_BELOW_MIN",
                }
            )
            continue

        order = _place_limit_slice(client, symbol, side, qty, price, dry_run)

        actual_price = price
        if order and "price" in order:
            actual_price = order["price"]

        slippage = abs(actual_price - price) / price * 100

        results.append(
            {
                "slice": idx,
                "qty": qty,
                "expected_price": price,
                "actual_price": actual_price,
                "slippage_pct": round(slippage, 6),
                "status": order.get("status", "FILLED") if order else "FAILED",
                "order_id": order.get("orderId") if order else None,
            }
        )

    # Cancel only orders placed by THIS TWAP execution (not user orders)
    if not dry_run:
        twap_order_ids = {
            str(r.get("order_id"))
            for r in results
            if r.get("order_id") and r.get("status") not in ("SKIPPED_BELOW_MIN",)
        }
        if twap_order_ids:
            try:
                open_orders = client.get_open_orders(symbol)
                for o in open_orders:
                    oid = str(_order_id(o))
                    if oid in twap_order_ids:
                        try:
                            client.cancel_order(symbol, _order_id(o))
                        except Exception:
                            logger.error(
                                "Failed to cancel TWAP order %s for %s",
                                _order_id(o), symbol,
                                exc_info=True,
                            )
            except Exception:
                logger.error(
                    "Failed to get open orders for TWAP cleanup",
                    exc_info=True,
                )

    return results


def execute_twap(
    client: Any,
    symbol: str,
    side: str,
    total_qty: float,
    duration_minutes: int = 10,
    num_slices: int = 5,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    """Plan and execute a TWAP order. Returns per-slice results."""
    slices = plan_twap(total_qty, duration_minutes, num_slices)
    return _execute_slices(client, symbol, side, slices, dry_run)


def execute_vwap(
    client: Any,
    symbol: str,
    side: str,
    total_qty: float,
    duration_minutes: int = 10,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    """Plan and execute a VWAP order. Returns per-slice results."""
    slices = plan_vwap(total_qty, symbol, duration_minutes, client)
    return _execute_slices(client, symbol, side, slices, dry_run)
