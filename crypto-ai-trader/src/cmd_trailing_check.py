"""
Trailing check command — extracted from main.py for maintainability.

Check open positions and update trailing stop-loss orders.
"""

import json as _json
import logging
import os
import time

from src.binance_client import BinanceClient
from src.indicators import Indicators
from src.notifier import FeishuNotifier
from src.risk_manager import RiskManager, TrailingStop, get_risk_manager

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


def cmd_trailing_check():
    """Check open positions and update trailing stop-loss orders.

    For each held position:
    1. Get current price and ATR
    2. Run TrailingStop.update() to check activation/move
    3. If SL should move up: cancel old SL order, place new one
    4. If trailing triggered: close position immediately

    Outputs JSON result for cron agent to format.
    """
    client = BinanceClient(testnet=False)
    ts = TrailingStop()
    risk_mgr = get_risk_manager(binance_client=client)  # singleton: reuse across calls
    notifier = FeishuNotifier()

    # Get non-USDT positions (exclude dust < $1 value)
    acct = client.get_account()
    positions = []
    for b in acct['balances']:
        asset = b['asset']
        free = float(b['free'])
        locked = float(b['locked'])
        total = free + locked
        if asset != 'USDT' and total > 0 and asset not in ('NTRN',):
            # Skip grid-managed symbols (their orders are handled by grid_bot)
            if asset in os.environ.get('GRID_MANAGED_ASSETS', '').split(','):
                continue
            # Filter dust: skip assets with < $1 total value
            symbol = f"{asset}USDT"
            try:
                stats = client.get_24hr_stats(symbol)
                price = float(stats.get('last_price', 0))
                if total * price < 1.0:
                    continue  # dust, skip
            except (ConnectionError, TimeoutError, ValueError, KeyError, OSError) as e:
                logger.debug(f"cmd_trailing_check: skipping {symbol}, can't get price: {e}")
                continue  # can't price, skip
            positions.append({"asset": asset, "symbol": symbol, "free": free, "locked": locked, "total": total})

    if not positions:
        # Clean stale trailing data
        for sym in list(ts.get_all().keys()):
            ts.remove(sym)
        print(_json.dumps({"action": "none", "reason": "no_positions"}))
        return

    results = []

    for pos in positions:
        asset = pos['asset']
        symbol = pos['symbol']
        p_prec = client.get_price_precision(symbol)

        # Get current price
        try:
            stats = client.get_24hr_stats(symbol)
            if not isinstance(stats, dict):
                results.append({"asset": asset, "action": "skip", "reason": "no_price_data"})
                continue
            current_price = float(stats.get('last_price', 0))
            if current_price <= 0:
                results.append({"asset": asset, "action": "skip", "reason": "invalid_price"})
                continue
        except Exception as e:
            logger.warning("cmd_trailing_check.cmd_trailing_check: " + str(e))
            results.append({"asset": asset, "action": "skip", "reason": str(e)})
            continue

        # Get ATR from klines
        try:
            klines_raw = client.get_klines(symbol, interval='1h', limit=20)
            if not klines_raw or len(klines_raw) < 15:
                results.append({"asset": asset, "action": "skip", "reason": "insufficient_klines"})
                continue
            # get_klines returns list of dicts
            klines = klines_raw
            atr = Indicators.atr(klines, period=14)
            if atr <= 0:
                results.append({"asset": asset, "action": "skip", "reason": "atr_zero"})
                continue
        except Exception as e:
            logger.warning("cmd_trailing_check.cmd_trailing_check: " + str(e))
            results.append({"asset": asset, "action": "skip", "reason": f"atr_error: {e}"})
            continue

        # Get true entry price from trade history (for new tracking or PnL)
        true_entry = None
        if asset not in ts.get_all():
            try:
                from src.entry_price import get_avg_entry_price
                true_entry = get_avg_entry_price(client, symbol, current_qty=pos['total'])
                if true_entry:
                    logger.info(f"True entry price for {asset}: ${true_entry:.6f}")
            except Exception as e:
                logger.warning(f"Cannot get entry price for {asset}: {e}")

        # Update trailing stop state
        update = ts.update(asset, current_price, atr, entry_price=true_entry)

        # IMPORTANT: Check triggered FIRST (triggered result lacks "activated" key)
        # Case 2: Trailing triggered — close position
        if update.get("triggered"):
            logger.warning("TrailingStop TRIGGERED for %s at $%.6f", asset, current_price)
            # Close all orders and sell remaining
            client.cancel_all_orders(symbol)
            qty_to_sell = pos['free']
            sell_ok = False
            if qty_to_sell > 0:
                for sell_attempt in range(3):
                    try:
                        sell_result = client.place_order(symbol, "SELL", "MARKET", qty_to_sell)
                        sell_ok = True
                        notifier.send_text(f"🔴 追蹤止損觸發 {asset}\n賣出 {qty_to_sell} @ ${current_price:.6f}\n最高: ${update['highest_price']:.6f}")
                        results.append({
                            "asset": asset,
                            "action": "trailing_triggered",
                            "price": current_price,
                            "highest": update["highest_price"],
                            "sl_price": update["sl_price"],
                            "sell_qty": qty_to_sell,
                        })
                        break
                    except Exception as e:
                        if sell_attempt < 2:
                            logger.warning(f"Trailing sell attempt {sell_attempt+1} failed: {e}, retrying in 2s...")
                            time.sleep(2)
                        else:
                            notifier.send_text(f"🔴🔴 追蹤止損觸發但賣出失敗 {asset}！手動處理！\n錯誤: {e}")
                            results.append({"asset": asset, "action": "triggered_sell_failed", "error": str(e)})
            else:
                results.append({"asset": asset, "action": "triggered_no_free_balance"})

            # Record PnL for loss guard
            try:
                entry_price = update.get("entry_price", 0)
                if entry_price > 0 and (sell_ok or qty_to_sell == 0):
                    pnl = (current_price - entry_price) * (qty_to_sell if qty_to_sell > 0 else pos['total'])
                    risk_mgr.post_trade_update(asset, pnl)
                    logger.info(f"Post-trade update: {asset} PnL={pnl:.4f} USDT")
            except Exception as e:
                logger.error(f"Failed to record post-trade update for {asset}: {e}")

            ts.remove(asset)
            continue

        # Case 1: Not yet activated
        if not update.get("activated"):
            results.append({
                "asset": asset,
                "price": current_price,
                "atr": round(atr, 6),
                "action": "tracking",
                "activated": False,
            })
            continue

        # Case 3: Trailing active — check if SL needs to move up
        new_sl = update.get("sl_price", 0)
        if new_sl <= 0:
            results.append({"asset": asset, "action": "tracking", "sl_price": 0})
            continue

        # Find existing SL order
        open_orders = client.get_open_orders(symbol)
        sl_orders = [o for o in open_orders if _is_stop_order(o)]

        old_sl_price = 0
        sl_moved = False

        if sl_orders:
            sl_order = sl_orders[0]
            old_sl_price = float(sl_order.get('stopPrice', 0) or sl_order.get('price', 0))

            # Only move UP
            if new_sl > old_sl_price * 1.001:  # 0.1% buffer to avoid dust moves
                sl_qty = _order_qty(sl_order)
                new_sl_rounded = round(new_sl, p_prec)

                # === Cancel-first SL move with safety net ===
                # When position is fully locked in OCO orders, placing new SL first
                # fails with insufficient balance. Strategy:
                # 1. Cancel old SL to free locked balance
                # 2. Wait briefly for exchange to process
                # 3. Place new SL
                # 4. If new SL fails, re-place old SL as safety net (avoid naked position)

                # Step 1: Cancel old SL
                cancel_ok = False
                for cancel_attempt in range(3):
                    try:
                        cancel_result = client.cancel_order(symbol, _order_id(sl_order))
                        if cancel_result:
                            cancel_ok = True
                            break
                    except Exception as e:
                        logger.warning(
                            "TrailingStop: cancel old SL attempt %d failed for %s: %s",
                            cancel_attempt + 1, asset, e
                        )
                        if cancel_attempt < 2:
                            time.sleep(1)

                if not cancel_ok:
                    logger.error(
                        "TrailingStop: failed to cancel old SL for %s. Aborting SL move, old SL preserved.",
                        asset
                    )
                    notifier.send_text(
                        f"⚠️ SL取消失敗 {asset}！無法移動SL，舊SL(${old_sl_price:.6f})保留。"
                    )
                    results.append({
                        "asset": asset,
                        "action": "sl_cancel_failed",
                        "old_sl": old_sl_price,
                        "target_sl": new_sl_rounded,
                        "msg": "Failed to cancel old SL, old SL preserved",
                    })
                    continue

                # Step 2: Wait for balance release
                time.sleep(0.5)

                # Step 3: Place new SL
                new_sl_order = None
                for sl_attempt in range(3):
                    try:
                        new_sl_order = client.place_order(
                            symbol, "SELL", "STOP_LOSS_LIMIT",
                            sl_qty, price=new_sl_rounded, stop_price=new_sl_rounded
                        )
                        if new_sl_order:
                            break
                    except Exception as e:
                        logger.warning(
                            "TrailingStop: new SL placement attempt %d failed for %s: %s",
                            sl_attempt + 1, asset, e
                        )
                        if sl_attempt < 2:
                            time.sleep(1)

                if new_sl_order:
                    sl_moved = True
                    logger.info(
                        "TrailingStop SL moved %s: $%.6f → $%.6f",
                        asset, old_sl_price, new_sl_rounded
                    )
                    results.append({
                        "asset": asset,
                        "action": "sl_moved",
                        "old_sl": old_sl_price,
                        "new_sl": new_sl_rounded,
                        "highest": update["highest_price"],
                        "current_price": current_price,
                        "callback_pct": update.get("callback_pct", 0),
                    })
                else:
                    # Step 4: SAFETY NET — re-place old SL to avoid naked position
                    old_sl_rounded = round(old_sl_price, p_prec)
                    safety_ok = False
                    try:
                        safety_order = client.place_order(
                            symbol, "SELL", "STOP_LOSS_LIMIT",
                            sl_qty, price=old_sl_rounded, stop_price=old_sl_rounded
                        )
                        if safety_order:
                            safety_ok = True
                    except Exception as safety_err:
                        logger.error(
                            "TrailingStop: safety net SL re-placement failed for %s: %s",
                            asset, safety_err
                        )

                    if safety_ok:
                        logger.warning(
                            "TrailingStop: new SL failed for %s after 3 retries! "
                            "Re-placed old SL at $%.6f as safety net.",
                            asset, old_sl_price
                        )
                        notifier.send_text(
                            f"⚠️ SL移動失敗 {asset}！新SL掛單3次失敗，已恢復舊SL(${old_sl_price:.6f})。請手動確認。"
                        )
                        results.append({
                            "asset": asset,
                            "action": "sl_move_reverted",
                            "old_sl": old_sl_price,
                            "target_sl": new_sl_rounded,
                            "msg": "New SL failed 3x, old SL re-placed as safety net",
                        })
                    else:
                        logger.critical(
                            "TrailingStop: new SL failed AND safety net failed for %s! "
                            "POSITION IS NAKED — no SL protection!",
                            asset
                        )
                        notifier.send_text(
                            f"🔴🔴 SL移動失敗且無法恢復 {asset}！倉位無SL保護！請立即手動處理！"
                        )
                        results.append({
                            "asset": asset,
                            "action": "sl_naked_position",
                            "old_sl": old_sl_price,
                            "target_sl": new_sl_rounded,
                            "msg": "Both new SL and safety net failed — NAKED POSITION",
                        })
            else:
                # SL already at or above target
                results.append({
                    "asset": asset,
                    "action": "sl_unchanged",
                    "sl_price": old_sl_price,
                    "target_sl": new_sl,
                    "highest": update["highest_price"],
                    "current_price": current_price,
                })
        else:
            # No SL found — place one
            new_sl_rounded = round(new_sl, p_prec)
            qty_for_sl = pos['free']
            if qty_for_sl >= 1:
                sl_order = client.place_order(
                    symbol, "SELL", "STOP_LOSS_LIMIT",
                    qty_for_sl, price=new_sl_rounded, stop_price=new_sl_rounded
                )
                if sl_order:
                    sl_moved = True
                    results.append({
                        "asset": asset,
                        "action": "sl_created",
                        "new_sl": new_sl_rounded,
                        "qty": qty_for_sl,
                        "highest": update["highest_price"],
                        "current_price": current_price,
                    })
                else:
                    results.append({"asset": asset, "action": "sl_create_failed"})
            else:
                results.append({"asset": asset, "action": "no_free_balance_for_sl"})

    # Check SL coverage for all positions
    for pos in positions:
        asset = pos['asset']
        symbol = pos['symbol']
        total_qty = pos['total']
        free_qty = pos['free']

        open_orders = client.get_open_orders(symbol)
        sl_orders = [o for o in open_orders if _is_stop_order(o)]
        tp_orders = [o for o in open_orders if not _is_stop_order(o)]
        sl_covered = sum(_order_qty(o) for o in sl_orders)
        tp_covered = sum(_order_qty(o) for o in tp_orders)

        uncovered_by_sl = total_qty - sl_covered  # units with no SL protection

        # Case 1: Position fully locked in TP but no SL — cancel lowest TP to make room for SL
        if uncovered_by_sl > 0 and free_qty < uncovered_by_sl and tp_orders:
            # Estimate SL price before canceling TP
            try:
                stats = client.get_24hr_stats(symbol)
                est_price = float(stats.get('last_price', 0)) if stats else 0
            except (ConnectionError, TimeoutError, ValueError, KeyError, OSError):
                est_price = 0
            est_sl_price = round(est_price * 0.95, p_prec) if est_price > 0 else 0
            est_notional = uncovered_by_sl * est_sl_price if est_sl_price > 0 else 0

            # If SL notional would be below $5, canceling TP would just waste it — skip
            if est_notional < 5.0 and est_notional > 0:
                logger.info(
                    "Skipping TP cancel for %s: SL notional $%.2f < $5 minimum. "
                    "TP preserved.",
                    asset, est_notional,
                )
            else:
                # All or most units locked in TP with no SL — cancel lowest TP
                lowest_tp = min(tp_orders, key=lambda o: float(o.get('price', 0)))
                cancel_qty = _order_qty(lowest_tp)
                logger.warning(
                    "No SL for %s (%.4f total, SL covers %.4f, TP locks %.4f). "
                    "Canceling lowest TP (%.4f @ $%s) to place SL.",
                    asset, total_qty, sl_covered, tp_covered,
                    cancel_qty, lowest_tp.get('price'),
                )
                try:
                    cancel_result = client.cancel_order(symbol, _order_id(lowest_tp))
                    if cancel_result:
                        free_qty += cancel_qty  # freed up by cancel
                        uncovered_by_sl = total_qty - sl_covered  # recalculate
                        tp_covered -= cancel_qty
                except Exception as e:
                    logger.error(f"Failed to cancel TP for {asset}: {e}")
                    results.append({
                        "asset": asset, "action": "no_sl_cancel_tp_failed",
                        "error": str(e),
                    })

        # Case 2: Free units available with no SL — place default -5% SL
        # Use qty_to_protect = min(free_qty, uncovered_by_sl) but also check notional minimum
        qty_to_protect = min(free_qty, uncovered_by_sl)
        p_prec = client.get_price_precision(symbol)
        current_price = 0
        try:
            stats = client.get_24hr_stats(symbol)
            current_price = float(stats.get('last_price', 0))
        except (ConnectionError, TimeoutError, ValueError, KeyError, OSError):
            pass  # current_price stays 0, will skip this position

        if qty_to_protect <= 0 or current_price <= 0:
            continue

        # Check minimum notional ($5 on Binance)
        notional = qty_to_protect * current_price
        if notional < 5.0:
            results.append({
                "asset": asset, "action": "no_sl_below_notional",
                "qty": qty_to_protect, "value": round(notional, 2),
                "msg": f"價值 ${notional:.2f} < $5 最低掛單門檻",
            })
            continue

        sl_price = round(current_price * 0.95, p_prec)  # -5% default SL
        try:
            sl_result = client.place_order(
                symbol, "SELL", "STOP_LOSS_LIMIT",
                qty_to_protect, price=sl_price, stop_price=sl_price
            )
            if sl_result:
                logger.warning(
                    "SL placed for unprotected position: %s %.4f units @ $%.6f",
                    asset, qty_to_protect, sl_price,
                )
                results.append({
                    "asset": asset,
                    "action": "uncovered_sl_created",
                    "qty": qty_to_protect,
                    "sl_price": sl_price,
                    "current_price": current_price,
                })
            else:
                logger.critical("Failed to place SL for %s %.4f units", asset, qty_to_protect)
                results.append({
                    "asset": asset, "action": "uncovered_sl_failed",
                    "qty": qty_to_protect,
                })
        except Exception as e:
            logger.error("Error placing SL for %s: %s", asset, e)
            results.append({
                "asset": asset, "action": "uncovered_sl_error",
                "qty": qty_to_protect, "error": str(e),
            })

    # Detect SL/TP filled by exchange (position gone but was tracked)
    tracked = ts.get_all()
    for sym in list(tracked.keys()):
        sym_info = tracked[sym]
        # Normalize symbol for comparison (trailing stop uses USDT suffix)
        sym_normalized = sym if sym.endswith("USDT") else sym + "USDT"
        # Find if this asset is still in positions with meaningful balance
        sym_pos = next((p for p in positions if p['asset'] == sym or p['asset'] == sym_normalized or p.get('symbol') == sym_normalized), None)
        if sym_pos is None:
            # Position gone — SL/TP was filled on exchange
            entry_price = sym_info.get('entry_price', 0)
            # Determine exit reason from trailing_stop state
            activated = sym_info.get('activated', False)
            sl_price = sym_info.get('sl_price', 0)
            if activated:
                exit_reason = "trailing"
            elif sl_price > 0 and entry_price > 0:
                exit_reason = "sl"
            else:
                exit_reason = "order_fill"
            if entry_price > 0:
                try:
                    # Get the exit price from recent trades
                    exit_price = 0
                    symbol = sym_normalized
                    trades = client.get_my_trades(symbol=symbol, limit=5)
                    if trades:
                        last_trade = trades[-1]
                        exit_price = float(last_trade.get('price', 0))
                    if exit_price == 0:
                        # Fallback to current market price
                        stats = client.get_24hr_stats(symbol)
                        exit_price = float(stats.get('last_price', 0))
                    if exit_price > 0:
                        # P1-10: Get actual filled qty from Binance trade history
                        # instead of relying on pos['total'] which is 0 when position is gone
                        actual_qty = 0.0
                        if trades:
                            # Sum qty from recent trades (exit sells)
                            actual_qty = sum(float(t.get('qty', 0)) for t in trades)
                        if actual_qty <= 0:
                            # Fallback to position data if trades unavailable
                            actual_qty = sym_pos['total'] if sym_pos else sym_info.get('qty', 0)
                        qty = actual_qty
                        if qty <= 0:
                            logger.warning(f"Cannot compute PnL for {sym}: no qty available (position gone, not tracked)")
                        else:
                            pnl = (exit_price - entry_price) * qty
                            risk_mgr.post_trade_update(sym, pnl)
                            logger.info(f"Detected SL/TP fill: {sym} entry={entry_price} exit={exit_price} qty={qty} PnL={pnl:.4f}")
                        results.append({
                            "asset": sym,
                            "action": "sltp_filled_detected",
                            "exit_reason": exit_reason,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "qty": qty,
                            "pnl": round((exit_price - entry_price) * qty, 4) if qty > 0 else 0,
                        })
                except Exception as e:
                    err_str = str(e)
                    # Skip known non-critical errors: invalid symbol, no such order
                    if "-1121" in err_str or "Invalid symbol" in err_str:
                        logger.warning(f"SL/TP fill detection skipped for {sym}: {err_str}")
                    else:
                        logger.error(f"Failed to record SL/TP fill for {sym}: {e}")

    # Clean stale entries (positions no longer held)
    held_assets = {p['asset'] for p in positions}
    for sym in list(ts.get_all().keys()):
        sym_normalized = sym if sym.endswith("USDT") else sym + "USDT"
        if sym_normalized not in held_assets and sym not in held_assets:
            ts.remove(sym)

    print(_json.dumps({"positions": len(positions), "results": results}, default=str, ensure_ascii=False))
