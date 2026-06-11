#!/home/travis/crypto-ai-trader/.venv/bin/python3
"""
Auto-fix missing TP/SL orders for all portfolio positions.

Checks each position's order coverage and:
1. If TP is missing but portfolio has a TP target → places LIMIT SELL
2. If SL is missing but portfolio has an SL target → places STOP_LOSS_LIMIT
3. If full qty is locked in SL with no TP → cancels SL, places OCO (TP+SL)

SPOT ONLY. Uses BinanceClient directly.

Exit codes:
  0 = all good or fixes applied
  1 = error
"""

import sys
import os
import json
import time
import logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.expanduser("~/trading-systems/crypto-ai-trader"))

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

from src.binance_client import BinanceClient
from src.state_db import get_state_db
from src.trade_outcome_recorder import TradeOutcomeRecorder

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)

NTRN = "NTRN"
DUST_THRESHOLD = 1.0  # USD


def get_positions_with_targets():
    """Read portfolio from StateDB with entry_price, stop_loss, take_profit."""
    import sqlite3
    db = get_state_db()
    conn = db._get_conn()
    saved_factory = conn.row_factory
    conn.row_factory = None
    rows = conn.execute(
        "SELECT symbol, quantity, entry_price, stop_loss, take_profit FROM portfolio"
    ).fetchall()
    conn.row_factory = saved_factory  # restore for other callers
    positions = {}
    for row in rows:
        sym, qty, entry, sl, tp = row
        if NTRN in sym:
            continue
        if qty <= 0:
            continue
        positions[sym] = {
            "quantity": qty,
            "entry_price": entry,
            "stop_loss": sl,
            "take_profit": tp,
        }
    return positions


def get_order_coverage(client, symbol):
    """Classify open orders into SL, TP, and other."""
    orders = client.get_open_orders(symbol)
    sl_orders = []
    tp_orders = []
    for o in orders:
        otype = o["type"].upper()
        if otype == "STOP_LOSS_LIMIT" and o["side"].upper() == "SELL":
            sl_orders.append(o)
        elif otype in ("LIMIT", "LIMIT_MAKER") and o["side"].upper() == "SELL":
            tp_orders.append(o)
    return sl_orders, tp_orders


def floor_qty(qty, step_size):
    """Floor quantity to step size."""
    from decimal import Decimal, ROUND_DOWN
    d_qty = Decimal(str(qty))
    d_step = Decimal(str(step_size))
    return float(d_qty.quantize(d_step, rounding=ROUND_DOWN))


def get_symbol_filters(client, symbol):
    """Get LOT_SIZE and PRICE_FILTER for a symbol."""
    try:
        info = client._get_exchange_info()
        sym_info = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
        if not sym_info:
            return {}
        filters = {}
        for f in sym_info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                filters["stepSize"] = float(f["stepSize"])
                filters["minQty"] = float(f["minQty"])
                step_str = f["stepSize"].rstrip("0").rstrip(".")
                filters["qty_decimals"] = len(step_str.split(".")[-1]) if "." in step_str else 0
            elif f["filterType"] == "PRICE_FILTER":
                filters["tickSize"] = float(f["tickSize"])
                tick_str = f["tickSize"].rstrip("0").rstrip(".")
                filters["price_decimals"] = len(tick_str.split(".")[-1]) if "." in tick_str else 0
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                filters["minNotional"] = float(f["minNotional"])
        return filters
    except Exception as e:
        logger.warning(f"Failed to get filters for {symbol}: {e}")
        return {}


def round_price(price, tick_size):
    """Round price to tick size."""
    from decimal import Decimal, ROUND_DOWN
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick_size))
    return float(d_price.quantize(d_tick, rounding=ROUND_DOWN))


def main():
    client = BinanceClient(testnet=False)
    positions = get_positions_with_targets()
    fixes = []
    errors = []

    # ── Sync check: remove DB positions that no longer exist on Binance ──
    try:
        acct = client.get_account()
        binance_assets = {}
        for b in acct['balances']:
            total = float(b['free']) + float(b['locked'])
            if total > 0:
                binance_assets[b['asset']] = total

        stale = []
        for sym, pos in positions.items():
            asset = sym.replace('/', '').replace('USDT', '')
            binance_qty = binance_assets.get(asset, 0)
            db_qty = pos['quantity']
            # If Binance has < 1% of DB qty, position was likely sold
            if binance_qty < db_qty * 0.01:
                stale.append(sym)

        if stale:
            db = get_state_db()
            conn = db._get_conn()
            for sym in stale:
                conn.execute('DELETE FROM portfolio WHERE symbol = ?', (sym,))
                conn.execute('DELETE FROM trailing_stop WHERE symbol = ?', (sym,))
                fixes.append(f"{sym}: DB已清理（Binance無持倉）")
            conn.commit()
            # Refresh positions
            positions = {k: v for k, v in positions.items() if k not in stale}

        # ── Cleanup orphaned trailing stops (not in portfolio) ──
        db = get_state_db()
        conn = db._get_conn()
        ts_rows = conn.execute('SELECT symbol FROM trailing_stop').fetchall()
        portfolio_syms = set(positions.keys())
        for row in ts_rows:
            ts_sym = row[0]
            if ts_sym not in portfolio_syms:
                conn.execute('DELETE FROM trailing_stop WHERE symbol = ?', (ts_sym,))
                fixes.append(f"{ts_sym}: 清理孤立trailing_stop")
        conn.commit()

    except Exception as e:
        errors.append(f"同步檢查失敗: {e}")

    # ── Detect Binance positions NOT in local DB ──
    try:
        acct = client.get_account()
        binance_assets = {}
        for b in acct['balances']:
            total = float(b['free']) + float(b['locked'])
            if total > 0:
                binance_assets[b['asset']] = total

        STABLECOINS = {'USDT', 'USDC', 'BUSD', 'DAI', 'TUSD', 'FDUSD', 'USDP', 'EUR', 'RLUSD', 'EURT', 'AEUR', 'GBP', 'NTRN', 'BNB'}
        db_syms_lower = {s.lower().replace('/', '').replace('usdt', '') for s in positions}
        db = get_state_db()
        conn = db._get_conn()

        for asset, total_qty in binance_assets.items():
            if asset in STABLECOINS:
                continue
            sym_key = f"{asset}USDT"
            asset_lower = asset.lower()
            if asset_lower in db_syms_lower:
                continue
            # Skip BTC (often has residual dust from trades)
            if asset == 'BTC' and total_qty * 77000 < DUST_THRESHOLD:
                continue

            try:
                price = float(client.get_ticker_price(sym_key))
                if price <= 0:
                    continue
                notional = total_qty * price
                if notional < DUST_THRESHOLD:
                    continue
            except Exception:
                continue

            # Compute default SL/TP (7% SL, 4% TP1)
            sl_pct = 0.07
            tp_pct = 0.04
            entry_price = price  # no history available, use market
            sl_target = round(price * (1 - sl_pct), 8)
            tp_target = round(price * (1 + tp_pct), 8)

            # Insert into portfolio table
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO portfolio
                       (symbol, quantity, entry_price, strategy, opened_at, updated_at, stop_loss, take_profit)
                       VALUES (?, ?, ?, 'synced', ?, ?, ?, ?)""",
                    (sym_key, total_qty, entry_price, datetime.now(timezone.utc).isoformat(),
                     time.time(), sl_target, tp_target)
                )
                conn.commit()
            except Exception as e:
                errors.append(f"{sym_key}: DB寫入失敗 ({e})")
                continue

            positions[sym_key] = {
                "quantity": total_qty,
                "entry_price": entry_price,
                "stop_loss": sl_target,
                "take_profit": tp_target,
            }
            fixes.append(f"{sym_key}: 從Binance補錄到DB（qty={total_qty}, SL=${sl_target:.4f}, TP=${tp_target:.4f}）")

    except Exception as e:
        errors.append(f"未追蹤持倉檢測失敗: {e}")

    if not positions:
        print(json.dumps({"fixes": fixes, "errors": errors}, ensure_ascii=False))
        return

    for sym, pos in positions.items():
        qty = pos["quantity"]
        tp_target = pos.get("take_profit")
        sl_target = pos.get("stop_loss")
        entry = pos.get("entry_price", 0)

        # Skip dust positions
        try:
            current_price = client.get_ticker_price(sym)
            if not current_price:
                errors.append(f"{sym}: 無法獲取價格")
                continue
        except Exception as e:
            errors.append(f"{sym}: 無法獲取價格 ({e})")
            continue

        notional = qty * current_price
        if notional < DUST_THRESHOLD:
            continue

        sl_orders, tp_orders = get_order_coverage(client, sym)
        sl_covered = sum(float(o.get("amount", o.get("origQty", 0))) for o in sl_orders)
        tp_covered = sum(float(o.get("amount", o.get("origQty", 0))) for o in tp_orders)
        total_covered = sl_covered + tp_covered

        # Check min notional
        filters = get_symbol_filters(client, sym)
        min_notional = filters.get("minNotional", 5.0)
        step_size = filters.get("stepSize", 0.001)
        tick_size = filters.get("tickSize", 0.01)
        qty_decimals = filters.get("qty_decimals", 4)
        price_decimals = filters.get("price_decimals", 2)

        has_tp = len(tp_orders) > 0
        has_sl = len(sl_orders) > 0

        # ── Case 0: Both SL and TP exist as separate orders → restructure to OCO ──
        if has_tp and has_sl and notional >= 10:
            # Check if they're separate orders (not already OCO)
            if total_covered <= qty * 1.01:  # not double-covered
                # Cancel all and place OCO
                for o in sl_orders + tp_orders:
                    try:
                        client.cancel_order(sym, o.get("id", o.get("orderId")))
                        time.sleep(0.5)
                    except Exception:
                        pass

                tp_price = round_price(tp_target, tick_size) if tp_target and tp_target >= current_price else round_price(current_price * 1.05, tick_size)
                sl_price_r = round_price(sl_target, tick_size) if sl_target else round_price(float(sl_orders[0].get("stopPrice", 0) or sl_orders[0].get("info", {}).get("stopPrice", 0)), tick_size)
                sl_limit = round_price(sl_price_r * 0.995, tick_size)

                try:
                    oco = client.place_oco(sym, qty, tp_price, sl_price_r, sl_limit)
                    if oco:
                        fixes.append(f"{sym}: 重構為OCO（TP ${tp_price} + SL ${sl_price_r}）")
                        continue
                except Exception as e:
                    # OCO failed, re-place separate orders
                    logger.warning(f"OCO restructure failed for {sym}: {e}")
                    try:
                        client.place_limit_sell(sym, floor_qty(qty * 0.50, step_size), tp_price)
                        time.sleep(0.5)
                        client.place_stop_loss_limit(sym, floor_qty(qty * 0.50, step_size), sl_limit, sl_price_r)
                        fixes.append(f"{sym}: 重掛分離TP+SL（OCO失敗）")
                        continue
                    except Exception as e2:
                        errors.append(f"{sym}: 重構失敗 ({e2})")
                        continue

        # ── Case 1: Missing TP, has SL ──
        if not has_tp and has_sl and tp_target:
            # Check if SL covers full qty (no room for TP)
            sl_covers_full = sl_covered >= qty * 0.99  # 1% tolerance for rounding
            sl_qty = float(sl_orders[0].get("amount", sl_orders[0].get("origQty", 0)))
            sl_price = float(sl_orders[0].get("stopPrice", 0) or sl_orders[0].get("info", {}).get("stopPrice", 0))

            if sl_covers_full:
                # Try multiple split ratios to find one that meets minNotional
                # Priority: 30/70 → 50/50 → SL-only
                best_tp_pct = None
                for tp_pct in [0.30, 0.40, 0.50]:
                    tp_qty_raw = qty * tp_pct
                    tp_qty = floor_qty(tp_qty_raw, step_size)
                    sl_qty_new = floor_qty(qty - tp_qty, step_size)
                    if (tp_qty * current_price >= min_notional and
                            sl_qty_new * current_price >= min_notional):
                        best_tp_pct = tp_pct
                        break

                if best_tp_pct is None:
                    # Too small to split at any ratio — keep SL-only
                    fixes.append(f"{sym}: 太小(${notional:.1f})無法拆分TP+SL，保留SL-only")
                    continue

                tp_qty = floor_qty(qty * best_tp_pct, step_size)
                sl_qty_new = floor_qty(qty - tp_qty, step_size)

                # TP target: use stored target, but if below current price,
                # use current * 1.05 (next take-profit level)
                tp_price_raw = tp_target
                if tp_price_raw < current_price:
                    tp_price_raw = round_price(current_price * 1.05, tick_size)
                tp_price = round_price(tp_price_raw, tick_size)

                # Cancel existing SL
                try:
                    client.cancel_order(sym, sl_orders[0].get("id", sl_orders[0].get("orderId")))
                    time.sleep(0.5)
                except Exception as e:
                    errors.append(f"{sym}: SL取消失敗 ({e})")
                    continue

                sl_price_r = round_price(sl_target, tick_size) if sl_target else round_price(sl_price, tick_size)
                sl_limit = round_price(sl_price_r * 0.995, tick_size)

                oco_result = None
                if sl_qty_new > 0:
                    try:
                        oco_result = client.place_oco(
                            symbol=sym,
                            quantity=qty,
                            tp_price=tp_price,
                            sl_price=sl_price_r,
                            sl_limit_price=sl_limit,
                        )
                    except Exception as e:
                        logger.warning(f"OCO failed for {sym}: {e}")

                if oco_result:
                    fixes.append(f"{sym}: OCO已掛（TP ${tp_price} + SL ${sl_price_r}）")
                else:
                    # Fallback: place TP + SL separately
                    if tp_qty > 0:
                        try:
                            tp_order = client.place_limit_sell(sym, tp_qty, tp_price)
                            if tp_order:
                                fixes.append(f"{sym}: TP已掛 {tp_qty} @ ${tp_price}")
                                time.sleep(0.5)
                            else:
                                errors.append(f"{sym}: TP掛單失敗")
                        except Exception as e:
                            errors.append(f"{sym}: TP掛單失敗 ({e})")

                    if sl_qty_new > 0:
                        try:
                            sl_order = client.place_stop_loss_limit(
                                sym, sl_qty_new, sl_limit, sl_price_r
                            )
                            if sl_order:
                                fixes.append(f"{sym}: SL已掛 {sl_qty_new} @ ${sl_price_r}")
                            else:
                                errors.append(f"{sym}: SL掛單失敗")
                        except Exception as e:
                            errors.append(f"{sym}: SL掛單失敗 ({e})")
            else:
                # SL doesn't cover full qty — add TP with uncovered portion
                uncovered = qty - sl_covered
                if uncovered > 0:
                    tp_qty = floor_qty(uncovered, step_size)
                    tp_price = round_price(tp_target, tick_size)
                    if tp_qty * current_price >= min_notional:
                        try:
                            tp_order = client.place_limit_sell(sym, tp_qty, tp_price)
                            if tp_order:
                                fixes.append(f"{sym}: TP已掛 {tp_qty} @ ${tp_price}（補充未覆蓋部分）")
                            else:
                                errors.append(f"{sym}: TP掛單失敗")
                        except Exception as e:
                            errors.append(f"{sym}: TP掛單失敗 ({e})")

        # ── Case 2: Missing SL, has TP ──
        elif not has_sl and has_tp and sl_target:
            sl_price = round_price(sl_target, tick_size)
            sl_limit = round_price(sl_price * 0.995, tick_size)
            # Use uncovered qty, but cap at free balance
            uncovered = qty - tp_covered
            sl_qty_raw = max(uncovered, qty * 0.30)
            # Get actual free balance to avoid insufficient funds
            try:
                asset = sym.replace('/', '').replace('USDT', '')
                bal = client.get_free_balance(asset)
                sl_qty = floor_qty(min(sl_qty_raw, bal), step_size)
            except Exception:
                sl_qty = floor_qty(min(sl_qty_raw, uncovered), step_size)
            if sl_qty * current_price >= min_notional:
                try:
                    sl_order = client.place_stop_loss_limit(sym, sl_qty, sl_limit, sl_price)
                    if sl_order:
                        fixes.append(f"{sym}: SL已掛 {sl_qty} @ ${sl_price}")
                    else:
                        errors.append(f"{sym}: SL掛單失敗")
                except Exception as e:
                    errors.append(f"{sym}: SL掛單失敗 ({e})")
            elif notional >= min_notional * 2:
                # Uncovered too small for standalone SL, but total position is large enough
                # → Cancel TPs and place OCO covering full qty
                tp_price = round_price(tp_target, tick_size) if tp_target and tp_target >= current_price else round_price(current_price * 1.05, tick_size)
                try:
                    for o in tp_orders:
                        client.cancel_order(sym, o.get("id", o.get("orderId")))
                        time.sleep(0.5)
                    oco = client.place_oco(sym, floor_qty(qty, step_size), tp_price, sl_price, sl_limit)
                    if oco:
                        fixes.append(f"{sym}: OCO重構（TP ${tp_price} + SL ${sl_price}，原SL太小）")
                    else:
                        errors.append(f"{sym}: OCO重構失敗")
                except Exception as e:
                    errors.append(f"{sym}: OCO重構失敗 ({e})")
            # else: position too small for any SL coverage

        # ── Case 3: Missing BOTH TP and SL ──
        elif not has_tp and not has_sl:
            if sl_target and tp_target and notional >= min_notional * 2:
                # Enough value → try OCO (TP + SL in one atomic pair)
                tp_price_raw = tp_target if tp_target >= current_price else current_price * 1.05
                tp_price = round_price(tp_price_raw, tick_size)
                sl_price = round_price(sl_target, tick_size)
                sl_limit = round_price(sl_price * 0.995, tick_size)
                try:
                    oco = client.place_oco(sym, floor_qty(qty, step_size), tp_price, sl_price, sl_limit)
                    if oco:
                        fixes.append(f"{sym}: OCO已掛（TP ${tp_price} + SL ${sl_price}）")
                        continue
                except Exception as e:
                    logger.warning(f"OCO failed for {sym}: {e}")

            # Fallback: SL-only (safety first)
            if sl_target:
                sl_price = round_price(sl_target, tick_size)
                sl_limit = round_price(sl_price * 0.995, tick_size)
                sl_qty = floor_qty(qty, step_size)
                if sl_qty * current_price >= min_notional:
                    try:
                        sl_order = client.place_stop_loss_limit(sym, sl_qty, sl_limit, sl_price)
                        if sl_order:
                            fixes.append(f"{sym}: SL已掛 {sl_qty} @ ${sl_price}（OCO不可用→SL優先）")
                        else:
                            errors.append(f"{sym}: SL掛單失敗")
                    except Exception as e:
                        errors.append(f"{sym}: SL掛單失敗 ({e})")

    # ── Case 4: TP breached (current price >= TP target) ──
    # This happens when price spiked above TP but the LIMIT sell order didn't fill
    # (e.g., price went through too fast, or TP was set too tight).
    # Action: Cancel the stale TP order, place a market sell to lock profit immediately,
    # then update DB to remove the position.
    for sym, pos in positions.items():
        qty = pos["quantity"]
        tp_target = pos.get("take_profit")
        entry = pos.get("entry_price", 0)

        if not tp_target or qty <= 0:
            continue

        try:
            ticker = client.get_ticker_price(symbol=sym)
            current_price = float(ticker)
        except Exception:
            continue

        notional = qty * current_price
        if notional < DUST_THRESHOLD:
            continue

        # Check if price has breached TP
        if current_price >= tp_target:
            sl_orders, tp_orders = get_order_coverage(client, sym)
            pnl_pct = (current_price - entry) / entry * 100 if entry else 0

            # Cancel any existing sell orders before market sell
            for o in tp_orders + sl_orders:
                try:
                    client.cancel_order(sym, o.get("id", o.get("orderId")))
                    time.sleep(0.5)
                except Exception:
                    pass

            # Place market sell to lock profit
            try:
                filters = get_symbol_filters(client, sym)
                step_size = filters.get("stepSize", 0.001)
                sell_qty = floor_qty(qty, step_size)

                result = client.place_market_sell(sym, sell_qty)
                if result and result.get("status") in ("closed", "FILLED"):
                    fixes.append(
                        f"{sym}: TP已過(${current_price:.4f} >= ${tp_target:.4f})，已市價平倉鎖利 +{pnl_pct:.1f}%"
                    )
                    # Record trade outcome for self-learning
                    try:
                        db = get_state_db()
                        recorder = TradeOutcomeRecorder(db=db)
                        recorder.record_outcome(
                            symbol=sym,
                            exit_price=current_price,
                            exit_reason="tp_breach",
                        )
                    except Exception as e:
                        logger.warning(f"Outcome recording failed for {sym}: {e}")
                    # Remove from DB
                    db = get_state_db()
                    conn = db._get_conn()
                    conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
                    conn.execute("DELETE FROM trailing_stop WHERE symbol = ?", (sym,))
                    conn.commit()
                else:
                    # MARKET orders usually fill immediately; if not FILLED, log error
                    status = result.get("status") if result else "None"
                    errors.append(f"{sym}: 市價平倉失敗，訂單狀態: {status}")
            except Exception as e:
                errors.append(f"{sym}: TP過價自動平倉失敗 ({e})")

    # ── Trailing Take-Profit (P2 #6) ──
    # Adjust TP orders upward when price has far exceeded original targets.
    # Runs after TP breach check so stale orders are already cleaned up.
    try:
        from scripts.trailing_tp import trailing_tp_check
        tp_trail_result = trailing_tp_check(client=client, dry_run=False)
        if tp_trail_result["trailed"] > 0:
            fixes.append(f"追蹤止盈: {tp_trail_result['trailed']} 個TP已上調（{tp_trail_result['skipped']} 跳過）")
        if tp_trail_result["errors"]:
            errors.extend(tp_trail_result["errors"])
    except Exception as e:
        logger.warning(f"Trailing TP check failed (non-critical): {e}")

    # ── P0 #3: 強制 max_hold_hours 自動平倉 ──
    # Positions held beyond their configured max_hold_hours are force-closed.
    # Default: 72h. Override per strategy via StrategyAdaptor config.
    try:
        now_ts = time.time()
        db = get_state_db()
        conn = db._get_conn()
        saved_factory2 = conn.row_factory
        conn.row_factory = None
        rows = conn.execute(
            "SELECT symbol, quantity, entry_price, strategy, opened_at FROM portfolio"
        ).fetchall()
        conn.row_factory = saved_factory2
        # Default max hold: 48 hours (strategy may override)
        MAX_HOLD_DEFAULT = 48 * 3600
        for row in rows:
            sym, qty, entry, strat, opened = row
            if NTRN in sym or qty <= 0:
                continue
            if opened is None:
                continue
            try:
                opened_dt = datetime.fromisoformat(str(opened))
                opened_ts = opened_dt.timestamp()
            except (ValueError, TypeError):
                continue
            hold_sec = now_ts - opened_ts
            # Determine max_hold_hours from strategy config
            if strat in ("grid",):
                max_hold = 48 * 3600
            elif strat in ("dca",):
                max_hold = 72 * 3600
            elif strat in ("vwap",):
                max_hold = 24 * 3600
            else:
                max_hold = MAX_HOLD_DEFAULT
            if hold_sec > max_hold:
                hold_hours = hold_sec / 3600
                try:
                    ticker = client.get_ticker_price(symbol=sym)
                    current_price = float(ticker)
                except Exception as e:
                    errors.append(f"{sym}: max_hold expire check — price fetch failed: {e}")
                    continue
                notional = qty * current_price
                if notional < DUST_THRESHOLD:
                    continue
                # Cancel all open orders first
                try:
                    open_orders = client.get_open_orders(sym)
                    for o in open_orders:
                        client.cancel_order(sym, _order_id(o))
                        time.sleep(0.3)
                except Exception:
                    pass
                # Market sell to force-close
                try:
                    filters = get_symbol_filters(client, sym)
                    step = filters.get("stepSize", 0.001)
                    sell_qty = floor_qty(qty, step)
                    result = client.place_market_sell(sym, sell_qty)
                    if result and result.get("status") in ("closed", "FILLED"):
                        pnl_pct = (current_price - entry) / entry * 100 if entry else 0
                        fixes.append(
                            f"{sym}: 超過max_hold({hold_hours:.0f}h>{max_hold/3600:.0f}h)，"
                            f"已強制平倉 PnL {pnl_pct:+.1f}%"
                        )
                        # Record trade outcome for self-learning
                        try:
                            db = get_state_db()
                            recorder = TradeOutcomeRecorder(db=db)
                            recorder.record_outcome(
                                symbol=sym,
                                exit_price=current_price,
                                exit_reason="max_hold",
                            )
                        except Exception as e:
                            logger.warning(f"Outcome recording failed for {sym}: {e}")
                        # Remove from DB
                        conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
                        conn.execute("DELETE FROM trailing_stop WHERE symbol = ?", (sym,))
                        conn.commit()
                    else:
                        errors.append(f"{sym}: max_hold force-close failed")
                except Exception as e:
                    errors.append(f"{sym}: max_hold force-close error: {e}")
    except Exception as e:
        logger.warning(f"Max hold enforcement failed (non-critical): {e}")

    # ── Output ──
    result = {"fixes": fixes, "errors": errors}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
