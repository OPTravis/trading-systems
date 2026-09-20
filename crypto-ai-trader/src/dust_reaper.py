"""Dust position reaper — P0-1 (設計 v1.1 §四, Leo 9/20 15:08 拍板).

Single-path disposition (NO convertSmallBalanceToBNB — API key dust-convert
permission is permanently off):

- position HAS protection (any open order on the symbol) → healthy, skip;
  if it was in dust_watch, remove it (can hold protections again).
- position UNPROTECTED and notional >= minNotional x 1.05 → healthy enough to
  place protections; not reaper's business (ensure_tp_sl owns that).
- position UNPROTECTED, notional < minNotional x 1.05, and market-sellable
  (notional >= market floor: minNotional when NOTIONAL.applyMinToMarket=true,
  else minQty) → LIQUIDATE_CANDIDATE.
- position UNPROTECTED and NOT market-sellable → kv dust_watch; re-evaluated
  every round; crossing the sell floor upgrades to LIQUIDATE_CANDIDATE,
  crossing minNotional x 1.05 downgrades back to a normal position.

Modes: 'report' (default, logs verdicts only — 24h verification run) and
'auto' (executes market liquidation). Switch via kv key DUST_REAPER_MODE.

On liquidation (auto mode): market sell → portfolio.close_position bookkeeping
→ live_alerts DUST_LIQUIDATED → audit DUST_REAPER_LIQUIDATED (switch_cost
accounting) → re-entry constraint written to kv DUST_REENTRY_BLOCK
(3 x 4h cooldown + price path <= exit_price x 1.02; structural RVOL path is
M5 scope). switch-side enforcement lives in position_optimizer.

Fail-safe: never raises into the scan pipeline; API/filter failures skip the
symbol for this round and feed the health report counters.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- tunables (kept module-level for testability) ---------------------------
NOTIONAL_BUFFER = 1.05          # liquidate only below minNotional x 1.05
REENTRY_COOLDOWN_S = 3 * 4 * 3600   # 3 x 4H candles
REENTRY_PRICE_PATH = 1.02      # re-entry allowed <= exit_price x 1.02
WATCH_MAX_POSITIONS = 20       # dust_watch cap → WATCH_REPORT (24h throttled)
WATCH_REPORT_THROTTLE_S = 24 * 3600
MODE_DEFAULT = "report"        # first 24h: verdicts only

KV_MODE = "DUST_REAPER_MODE"
KV_WATCH = "dust_watch"
KV_REENTRY = "dust_reentry_block"
KV_WATCH_REPORT_TS = "dust_watch_report_ts"

LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
)
JSONL_PATH = os.path.join(LOG_DIR, "dust_reaper.jsonl")


def _get_mode(db) -> str:
    try:
        mode = db.kv_get(KV_MODE, MODE_DEFAULT)
    except Exception:
        mode = MODE_DEFAULT
    return "auto" if str(mode).lower() == "auto" else "report"


def _market_sell_floor(filters: Dict[str, Any]) -> float:
    """Minimum notional for a MARKET sell.

    Binance NOTIONAL filter with applyMinToMarket=true enforces minNotional
    on market orders too; legacy MIN_NOTIONAL does not, so market sells are
    bounded by minQty only.
    """
    if filters.get("applyMinToMarket"):
        return float(filters.get("minNotional", 0.0))
    return float(filters.get("minQty", 0.0))


def _append_jsonl(record: Dict[str, Any]) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(JSONL_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.debug("dust_reaper: jsonl append failed", exc_info=True)


def _liquidate(client, portfolio, symbol: str, qty: float, price: float,
               filters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Market-sell an unprotectable dust position. Returns order result."""
    order = None
    try:
        order = client.place_order(
            symbol=symbol,
            side="SELL",
            order_type="MARKET",
            quantity=qty,
        )
    except Exception:
        logger.error("dust_reaper: market sell failed for %s", symbol, exc_info=True)
        return None
    if not order or not order.get("orderId"):
        logger.error("dust_reaper: market sell returned no orderId for %s", symbol)
        return None

    fills = order.get("fills") or []
    if fills:
        filled_qty = sum(float(f.get("qty", 0)) for f in fills)
        gross = sum(float(f.get("qty", 0)) * float(f.get("price", 0)) for f in fills)
        avg_fill = (gross / filled_qty) if filled_qty > 0 else price
        fee_est = sum(float(f.get("commission", 0)) for f in fills)
    else:
        avg_fill, fee_est = price, qty * price * 0.001
        filled_qty = qty

    close_price = avg_fill
    client_order_id = str(order.get("clientOrderId") or order.get("orderId"))
    try:
        portfolio.close_position(
            symbol,
            close_price=close_price,
            exit_reason="dust_reaper",
            client_order_id=client_order_id,
        )
    except Exception:
        logger.error("dust_reaper: close_position bookkeeping failed for %s", symbol,
                     exc_info=True)

    db = getattr(portfolio, "_db", None)
    if db is not None:
        try:
            db.audit_log(
                "DUST_REAPER_LIQUIDATED",
                details={
                    "symbol": symbol,
                    "qty": filled_qty,
                    "notional_before": round(qty * price, 6),
                    "proceeds": round(filled_qty * close_price, 6),
                    "avg_fill_price": close_price,
                    "fee_est": round(fee_est, 8),
                    "switch_cost": round(qty * price - filled_qty * close_price + fee_est, 6),
                    "orderId": order.get("orderId"),
                },
                source="dust_reaper",
            )
        except Exception:
            logger.debug("dust_reaper: audit_log failed", exc_info=True)

        # re-entry constraint: cooldown + price path (RVOL path is M5)
        try:
            block = db.kv_get(KV_REENTRY, {}) or {}
            if not isinstance(block, dict):
                block = {}
            block[symbol] = {
                "until": time.time() + REENTRY_COOLDOWN_S,
                "exit_price": close_price,
                "ts": time.time(),
            }
            db.kv_set(KV_REENTRY, block)
        except Exception:
            logger.debug("dust_reaper: re-entry kv write failed", exc_info=True)

    try:
        from src.live_alerts import emit as emit_alert
        emit_alert(
            "DUST_LIQUIDATED",
            symbol,
            {
                "qty": filled_qty,
                "avg_fill_price": close_price,
                "notional_before": round(qty * price, 2),
                "orderId": order.get("orderId"),
                "mode": "auto",
            },
        )
    except Exception:
        pass
    return order


def reentry_allowed(db, symbol: str, ref_price: Optional[float] = None) -> bool:
    """Switch-side gate: True if re-entry into `symbol` is allowed.

    Blocked while inside the 3 x 4H cooldown unless the price path is taken
    (current price <= exit_price x 1.02 — i.e. we are not chasing the bounce).
    RVOL structural-breakthrough path arrives with M5.
    """
    try:
        block = db.kv_get(KV_REENTRY, {}) or {}
    except Exception:
        return True
    if not isinstance(block, dict):
        return True
    info = block.get(symbol)
    if not info:
        return True
    until = float(info.get("until") or 0)
    if time.time() >= until:
        try:
            block.pop(symbol, None)
            db.kv_set(KV_REENTRY, block)
        except Exception:
            pass
        return True
    exit_price = float(info.get("exit_price") or 0)
    if ref_price is not None and exit_price > 0 and ref_price <= exit_price * REENTRY_PRICE_PATH:
        return True   # price path: allowed at/below exit price x 1.02
    return False


def run(client, portfolio, log: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """One reaper round. Returns a summary dict for health_report."""
    log = log or logger
    db = getattr(portfolio, "_db", None)
    mode = _get_mode(db) if db is not None else MODE_DEFAULT

    summary = {
        "mode": mode,
        "positions": 0,
        "protected": 0,
        "liquidate_candidates": 0,
        "liquidated": 0,
        "liquidation_failures": 0,
        "watch": 0,
        "watch_added": 0,
        "watch_removed": 0,
        "watch_upgraded": 0,
        "filter_failures": 0,
        "api_failures": 0,
        "candidates": [],  # report rows for the verification window
    }

    try:
        positions = portfolio.positions or {}
    except Exception:
        positions = {}
    if not positions:
        return summary
    summary["positions"] = len(positions)

    watch = {}
    if db is not None:
        try:
            watch = db.kv_get(KV_WATCH, {}) or {}
            if not isinstance(watch, dict):
                watch = {}
        except Exception:
            watch = {}

    for symbol, pos in list(positions.items()):
        try:
            qty = float(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            price = client.get_ticker_price(symbol)
            if not price or price <= 0:
                summary["api_failures"] += 1
                continue
            notional = qty * price

            filters = client.get_symbol_filters(symbol) or {}
            min_qty = float(filters.get("minQty", 0) or 0)
            min_notional = float(filters.get("minNotional", 0) or 0)
            if min_qty <= 0 or min_notional <= 0:
                # real-filter read failed → do NOT guess (6979198 lesson):
                # skip and count for the health report
                summary["filter_failures"] += 1
                continue

            open_orders = client.get_open_orders(symbol) or []
            has_protection = len(open_orders) > 0

            sell_floor = _market_sell_floor(filters)
            healthy_line = min_notional * NOTIONAL_BUFFER

            if has_protection or notional >= healthy_line:
                if symbol in watch:
                    watch.pop(symbol, None)
                    summary["watch_removed"] += 1
                if has_protection:
                    summary["protected"] += 1
                continue

            if notional >= sell_floor:
                # unprotectable AND market-sellable → candidate
                summary["liquidate_candidates"] += 1
                summary["candidates"].append(
                    {
                        "symbol": symbol,
                        "qty": qty,
                        "notional": round(notional, 6),
                        "min_notional": min_notional,
                        "apply_min_to_market": bool(filters.get("applyMinToMarket")),
                    }
                )
                if symbol in watch:
                    summary["watch_upgraded"] += 1
                    watch.pop(symbol, None)
                _append_jsonl(
                    {
                        "ts": time.time(),
                        "mode": mode,
                        "verdict": "LIQUIDATE_CANDIDATE",
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                        "notional": round(notional, 6),
                        "min_notional": min_notional,
                        "apply_min_to_market": bool(filters.get("applyMinToMarket")),
                    }
                )
                log.info(
                    "dust_reaper: LIQUIDATE_CANDIDATE %s notional=%.4f (< %.4f x%.2f) unprotectable — %s",
                    symbol, notional, min_notional, NOTIONAL_BUFFER,
                    "liquidating" if mode == "auto" else "report-only",
                )
                if mode == "auto":
                    order = _liquidate(client, portfolio, symbol, qty, price, filters)
                    if order:
                        summary["liquidated"] += 1
                    else:
                        summary["liquidation_failures"] += 1
            else:
                # cannot even market-sell → watch
                if symbol not in watch:
                    summary["watch_added"] += 1
                watch[symbol] = {
                    "since": time.time(),
                    "notional": round(notional, 6),
                    "sell_floor": sell_floor,
                }
                _append_jsonl(
                    {
                        "ts": time.time(),
                        "mode": mode,
                        "verdict": "DUST_WATCH",
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                        "notional": round(notional, 6),
                        "sell_floor": sell_floor,
                    }
                )
        except Exception:
            summary["api_failures"] += 1
            log.warning("dust_reaper: %s evaluation failed (skip this round)",
                        symbol, exc_info=True)

    summary["watch"] = len(watch)

    # persist watch state; cap → WATCH_REPORT (throttled 24h)
    if db is not None and watch:
        try:
            db.kv_set(KV_WATCH, watch)
            if len(watch) > WATCH_MAX_POSITIONS:
                last = float(db.kv_get(KV_WATCH_REPORT_TS, 0) or 0)
                if time.time() - last > WATCH_REPORT_THROTTLE_S:
                    db.kv_set(KV_WATCH_REPORT_TS, time.time())
                    from src.live_alerts import emit as emit_alert
                    emit_alert(
                        "WATCH_REPORT",
                        "SYSTEM",
                        {"watch_positions": len(watch), "cap": WATCH_MAX_POSITIONS,
                         "symbols": sorted(watch.keys())[:50]},
                    )
        except Exception:
            log.debug("dust_reaper: watch kv persist failed", exc_info=True)

    return summary
