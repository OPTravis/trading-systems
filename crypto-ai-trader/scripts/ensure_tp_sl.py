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
from src.state_db import get_state_db, db_write_with_verify
from src.trade_outcome_recorder import TradeOutcomeRecorder

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)

NTRN = "NTRN"
DUST_THRESHOLD = 1.0  # USD

def insert_sell_dedup(conn, sym, qty, price, pnl, ts):
    """bug#24 fix (2026-08-24): ensure_tp_sl booked the same exchange exit TWICE
    when reconcile_fills (trailing-check, every 5 min) had already booked the
    actual fill and ensure_tp_sl's own paths (sync-check stale / tp_breach /
    max_hold) then wrote a full-qty SELL row on top — e.g. ENA 06:17/06:30,
    TRUMP 22:05/22:30, GRAM 19:25/19:30 on 8/23-24. Atomic INSERT..SELECT with
    the same matching window as reconcile_fills' bug#13 guard: same symbol,
    qty within 2%, price within 1%, 1h window. Returns True if inserted."""
    cur = conn.execute(
        """
        INSERT INTO trades (symbol, side, qty, price, pnl, timestamp)
        SELECT ?, 'SELL', ?, ?, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM trades
            WHERE symbol = ? AND side = 'SELL'
              AND ABS(qty - ?) <= ? * 0.02
              AND ABS(price - ?) <= ? * 0.01
              AND timestamp >= ? - 3600
              AND timestamp <= ? + 3600
        )
        """,
        (sym, qty, price, pnl, ts,
         sym, qty, qty, price, price, ts, ts),
    )
    return cur.rowcount > 0



def get_positions_with_targets():
    """Read portfolio from StateDB with entry_price, stop_loss, take_profit."""
    import sqlite3
    db = get_state_db()
    conn = db._get_conn()
    saved_factory = conn.row_factory
    conn.row_factory = None
    try:
        rows = conn.execute(
            "SELECT symbol, quantity, entry_price, stop_loss, take_profit FROM portfolio"
        ).fetchall()
    finally:
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


def _discipline_exit(client, sym, pos, current_price, exit_reason, fixes, errors):
    """bug#29 (2026-08-29): market-exit a stranded position + verified bookkeeping.

    Used when a position's stop has been breached but no SL order could be
    placed: Binance validates STOP_LOSS_LIMIT notional at the LIMIT price, so
    a position whose value slid under ~$5 can never be protected, and a
    market sell also needs >= $5 notional. While price keeps the position
    above the exchange floor the exit window is open — take it (8/29 ENSO:
    entered $5.01, slid to $4.73, SL rejected twice, manually exited +$0.08).
    Bookkeeping mirrors the tp_breach path: dedup SELL row + outcome close +
    portfolio cleanup, all write-with-verify, failures LOUD.
    Returns True if a critical bookkeeping failure occurred.
    """
    qty = pos["quantity"]
    entry = pos.get("entry_price", 0)
    try:
        filters = get_symbol_filters(client, sym)
        step_size = filters.get("stepSize", 0.001)
        sell_qty = floor_qty(qty, step_size)
        result = client.place_market_sell(sym, sell_qty)
    except Exception as e:
        errors.append(f"{sym}: 紀律平倉下單失敗 ({e})")
        return True
    if not result or result.get("status") not in ("closed", "FILLED"):
        status = result.get("status") if result else "None"
        errors.append(f"{sym}: 紀律平倉失敗，訂單狀態: {status}")
        return True

    pnl_pct = (current_price - entry) / entry * 100 if entry else 0
    fixes.append(
        f"{sym}: 止損已穿且SL無法掛（bug#29），紀律市價平倉 {pnl_pct:+.1f}%"
    )
    trade_pnl = (current_price - entry) * qty if entry > 0 else 0.0
    _sell_ts = time.time()

    def _insert_sell_row():
        db = get_state_db()
        conn = db._get_conn()
        insert_sell_dedup(conn, sym, sell_qty, current_price, trade_pnl, _sell_ts)
        conn.commit()

    def _verify_sell_row():
        db = get_state_db()
        row = db._get_conn().execute(
            "SELECT 1 FROM trades WHERE symbol = ? AND side = 'SELL' "
            "AND ABS(timestamp - ?) < 3600 LIMIT 1",
            (sym, _sell_ts),
        ).fetchone()
        return row is not None

    def _record_outcome():
        db = get_state_db()
        recorder = TradeOutcomeRecorder(db=db)
        recorder.record_outcome(
            symbol=sym, exit_price=current_price, exit_reason=exit_reason
        )

    def _verify_outcome_closed():
        db = get_state_db()
        row = db._get_conn().execute(
            "SELECT status FROM trade_outcomes WHERE symbol = ? "
            "AND status = 'closed' ORDER BY updated_at DESC LIMIT 1",
            (sym,),
        ).fetchone()
        return row is not None

    def _delete_rows():
        db = get_state_db()
        conn = db._get_conn()
        conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
        conn.execute("DELETE FROM trailing_stop WHERE symbol = ?", (sym,))
        conn.commit()

    def _verify_rows_gone():
        db = get_state_db()
        row = db._get_conn().execute(
            "SELECT 1 FROM portfolio WHERE symbol = ? LIMIT 1", (sym,)
        ).fetchone()
        return row is None

    _ok_sell = db_write_with_verify(
        db=get_state_db(), write_fn=_insert_sell_row, verify_fn=_verify_sell_row,
        label=f"ensure_tp_sl[{sym}] sl_breach SELL INSERT",
        attempts=3, backoff_sec=1.0,
    )
    _ok_outcome = db_write_with_verify(
        db=get_state_db(), write_fn=_record_outcome, verify_fn=_verify_outcome_closed,
        label=f"ensure_tp_sl[{sym}] sl_breach outcome close",
        attempts=3, backoff_sec=1.0,
    )
    _ok_cleanup = db_write_with_verify(
        db=get_state_db(), write_fn=_delete_rows, verify_fn=_verify_rows_gone,
        label=f"ensure_tp_sl[{sym}] sl_breach portfolio cleanup",
        attempts=3, backoff_sec=1.0,
    )
    if not (_ok_sell and _ok_outcome and _ok_cleanup):
        try:
            from src.notifier import FeishuNotifier
            FeishuNotifier().send_text(
                f"🔴 ensure_tp_sl: {sym} 紀律平倉已成交但DB寫入失敗 "
                f"(sell={_ok_sell}, outcome={_ok_outcome}, "
                f"cleanup={_ok_cleanup})，請核對 trades/outcomes/portfolio"
            )
        except Exception as ne:
            logger.error(f"DB-failure alert send failed: {ne}")
        return True
    return False


def main():
    client = BinanceClient(testnet=False)
    positions = get_positions_with_targets()
    fixes = []
    errors = []
    # critical_failures > 0 => script exits non-zero so run_ensure_tp_sl.sh
    # appends to logs/cron_failures.jsonl (monitoring) — bug#8: previously
    # the script always exited 0 even when bookkeeping writes were lost.
    critical_failures = 0

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
            recorder = TradeOutcomeRecorder(db=db)
            for sym in stale:
                pos_data = positions[sym]
                entry_price = pos_data.get('entry_price', 0)
                qty = pos_data.get('quantity', 0)
                sl_target = pos_data.get('stop_loss', 0)
                tp_target = pos_data.get('take_profit', 0)

                # Fetch recent Binance trades to find actual exit price
                exit_price = 0
                exit_reason = "unknown"
                try:
                    my_trades = client.get_my_trades(symbol=sym, limit=10)
                    # Find the most recent SELL trade
                    for t in reversed(my_trades):
                        is_buyer = t.get('isBuyer', True)
                        if not is_buyer:  # SELL
                            exit_price = float(t.get('price', 0))
                            sell_qty = float(t.get('qty', 0))
                            # Determine exit reason based on price vs SL/TP
                            if sl_target and exit_price <= sl_target * 1.002:
                                exit_reason = "sl"
                            elif entry_price and exit_price > entry_price:
                                exit_reason = "tp_breach"
                            else:
                                exit_reason = "trailing"
                            break
                except Exception as e:
                    logger.warning(f"Cannot fetch trade history for {sym}: {e}")

                # Fallback: use current market price
                if exit_price <= 0:
                    try:
                        ticker = client.get_ticker_price(symbol=sym)
                        exit_price = float(ticker)
                        exit_reason = "sync_cleanup"
                    except Exception:
                        exit_price = entry_price  # last resort
                        exit_reason = "sync_cleanup"

                # Write SELL to trades table (bug#24: dedup — reconcile_fills
                # may have already booked the actual exchange fill minutes ago)
                if exit_price > 0 and qty > 0:
                    pnl = (exit_price - entry_price) * qty if entry_price > 0 else 0
                    try:
                        inserted = insert_sell_dedup(
                            conn, sym, qty, exit_price, pnl, time.time()
                        )
                    except Exception as e:
                        logger.warning(f"Failed to write SELL trade for {sym}: {e}")
                        inserted = False

                    # Record outcome for self-learning
                    try:
                        recorder.record_outcome(
                            symbol=sym,
                            exit_price=exit_price,
                            exit_reason=exit_reason,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record outcome for {sym}: {e}")

                    pnl_pct = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
                    if inserted:
                        fixes.append(
                            f"{sym}: 平倉已記錄 SELL {qty} @ ${exit_price:.6f} "
                            f"({exit_reason}, PnL {pnl_pct:+.2f}%)"
                        )
                    else:
                        fixes.append(
                            f"{sym}: 平倉已被reconcile先行記錄，跳過重複記賬 "
                            f"(qty {qty} @ ${exit_price:.6f})"
                        )
                else:
                    fixes.append(f"{sym}: DB已清理（Binance無持倉，無法取得成交價）")

                conn.execute('DELETE FROM portfolio WHERE symbol = ?', (sym,))
                conn.execute('DELETE FROM trailing_stop WHERE symbol = ?', (sym,))
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
            # bug#17 fix: prefer the exchange's actual protective orders over
            # fresh defaults — executor may already have tiered SL/TP legs
            # (8/22 SOL case: recomputed defaults overrode real order prices)
            _re_tick = 0.00001
            _re_step = 0.001
            try:
                _re_filters = get_symbol_filters(client, sym_key)
                _re_tick = _re_filters.get("tickSize", _re_tick) or _re_tick
                _re_step = _re_filters.get("stepSize", _re_step) or _re_step
            except Exception:
                pass
            _re_sl_px, _re_tp_px = 0.0, 0.0
            try:
                for _o in client.get_open_orders(sym_key):
                    _o_stop = float(_o.get("stopPrice", 0) or _o.get("info", {}).get("stopPrice", 0) or 0)
                    _o_px = float(_o.get("price", 0) or _o.get("info", {}).get("price", 0) or 0)
                    if _o_stop > 0 and _o_stop < price:
                        _re_sl_px = max(_re_sl_px, _o_stop)
                    elif _o_px > price:
                        _re_tp_px = max(_re_tp_px, _o_px)
            except Exception:
                pass
            sl_target = round_price(_re_sl_px, _re_tick) if _re_sl_px > 0 else round_price(price * (1 - sl_pct), _re_tick)
            tp_target = round_price(_re_tp_px, _re_tick) if _re_tp_px > 0 else round_price(price * (1 + tp_pct), _re_tick)

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
        # bug#17 fix: tiered TP (>=2 legs) is the executor's designed exit
        # structure — restructuring it into a single-leg OCO destroys the
        # laddered exits (8/22 POL case: TP1/TP2 replaced by one TP leg)
        if has_tp and has_sl and notional >= 10 and len(tp_orders) < 2:
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
            # bug#17 fix: compare against step-floored holding (dust tail can
            # never be covered — 8/22 SOL: 0.497 locked vs 0.5195 held kept
            # every 30min cycle retrying and failing)
            from math import floor as _m_floor
            _coverable = _m_floor(qty / step_size) * step_size if step_size > 0 else qty
            sl_covers_full = sl_covered >= _coverable * 0.98  # 2% tolerance
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
            # bug#29: Binance validates STOP_LOSS_LIMIT notional at the LIMIT
            # price, not current — precheck at sl_limit to match the exchange.
            if sl_qty * sl_limit >= min_notional:
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
                # bug#29: precheck at limit price (exchange validation basis)
                if sl_qty * sl_limit >= min_notional:
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
                    # FIX 2026-08-18: write SELL row to trades table so the ledger
                    # stays complete (previously outcome-only + row deletion left no
                    # PnL trace in trades, e.g. ALLO +7.1% on 08-18 went unrecorded).
                    # FIX 2026-08-19: network-FS disk I/O flakes made BOTH the SELL
                    # insert and outcome close die silently (20:30 EDEN close: trade
                    # filled on exchange, DB untouched — patched by hand). All three
                    # bookkeeping ops now retry once, and a double failure is LOUD:
                    # escalated to errors[] (visible in cron output JSON) plus a
                    # Feishu alert, instead of an invisible logger.warning.
                    if entry > 0:
                        trade_pnl = (current_price - entry) * qty
                    else:
                        trade_pnl = 0.0

                    # FIX 2026-08-20 (bug#8): exception-only retry could not
                    # see writes that commit() acknowledges but the storage
                    # layer then loses (20:30 ACE close: errors=[] yet no row
                    # landed). Every critical write is now followed by a
                    # read-back verification; a verified loss retries and, on
                    # final failure, escalates LOUDLY: errors[] (cron JSON) +
                    # logger.error + logs/cron_failures.jsonl + non-zero exit.
                    _sell_ts = time.time()

                    def _insert_sell_row():
                        db = get_state_db()
                        conn = db._get_conn()
                        # bug#24: dedup against a concurrent reconcile_fills booking
                        # (qty within 2%, price within 1%, 1h window)
                        insert_sell_dedup(
                            conn, sym, sell_qty, current_price, trade_pnl, _sell_ts
                        )
                        conn.commit()

                    def _verify_sell_row():
                        db = get_state_db()
                        row = db._get_conn().execute(
                            "SELECT 1 FROM trades WHERE symbol = ? AND side = 'SELL' "
                            "AND ABS(timestamp - ?) < 3600 LIMIT 1",
                            (sym, _sell_ts),
                        ).fetchone()
                        return row is not None

                    def _record_outcome():
                        db = get_state_db()
                        recorder = TradeOutcomeRecorder(db=db)
                        recorder.record_outcome(
                            symbol=sym,
                            exit_price=current_price,
                            exit_reason="tp_breach",
                        )

                    def _verify_outcome_closed():
                        db = get_state_db()
                        row = db._get_conn().execute(
                            "SELECT status FROM trade_outcomes WHERE symbol = ? "
                            "AND status = 'closed' ORDER BY updated_at DESC LIMIT 1",
                            (sym,),
                        ).fetchone()
                        return row is not None

                    def _delete_rows():
                        db = get_state_db()
                        conn = db._get_conn()
                        conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
                        conn.execute("DELETE FROM trailing_stop WHERE symbol = ?", (sym,))
                        conn.commit()

                    def _verify_rows_gone():
                        db = get_state_db()
                        row = db._get_conn().execute(
                            "SELECT 1 FROM portfolio WHERE symbol = ? LIMIT 1", (sym,)
                        ).fetchone()
                        return row is None

                    _ok_sell = db_write_with_verify(
                        db=get_state_db(), write_fn=_insert_sell_row,
                        verify_fn=_verify_sell_row,
                        label=f"ensure_tp_sl[{sym}] SELL trade INSERT",
                        attempts=3, backoff_sec=1.0,
                    )
                    _ok_outcome = db_write_with_verify(
                        db=get_state_db(), write_fn=_record_outcome,
                        verify_fn=_verify_outcome_closed,
                        label=f"ensure_tp_sl[{sym}] outcome close",
                        attempts=3, backoff_sec=1.0,
                    )
                    _ok_cleanup = db_write_with_verify(
                        db=get_state_db(), write_fn=_delete_rows,
                        verify_fn=_verify_rows_gone,
                        label=f"ensure_tp_sl[{sym}] portfolio cleanup",
                        attempts=3, backoff_sec=1.0,
                    )
                    if not (_ok_sell and _ok_outcome and _ok_cleanup):
                        critical_failures += 1

                    if not (_ok_sell and _ok_outcome and _ok_cleanup):
                        try:
                            from src.notifier import FeishuNotifier
                            FeishuNotifier().send_text(
                                f"🔴 ensure_tp_sl: {sym} 平倉已成交但DB寫入失敗 "
                                f"(sell={_ok_sell}, outcome={_ok_outcome}, "
                                f"cleanup={_ok_cleanup})，請核對 trades/outcomes/portfolio"
                            )
                        except Exception as ne:
                            logger.error(f"DB-failure alert send failed: {ne}")
                else:
                    # MARKET orders usually fill immediately; if not FILLED, log error
                    status = result.get("status") if result else "None"
                    errors.append(f"{sym}: 市價平倉失敗，訂單狀態: {status}")
                    critical_failures += 1
            except Exception as e:
                errors.append(f"{sym}: TP過價自動平倉失敗 ({e})")

    # ── Case 5: stop breached but SL unplaceable → disciplined exit (bug#29) ──
    # Binance validates STOP_LOSS_LIMIT notional at the LIMIT price
    # (stop*0.995), so a position whose value slid under ~$5 can never get SL
    # protection, and a market sell needs >= $5 too. If price has already
    # breached the stop and the position is unprotected, the only disciplined
    # action is a market exit while the notional window is open; if even that
    # is blocked the position is STRANDED → LOUD alert (Feishu + errors[]).
    for sym, pos in positions.items():
        qty = pos["quantity"]
        sl_target = pos.get("stop_loss")
        if not sl_target or qty <= 0:
            continue
        # Skip positions already closed by the tp_breach pass above
        try:
            still_open = get_state_db()._get_conn().execute(
                "SELECT 1 FROM portfolio WHERE symbol = ? LIMIT 1", (sym,)
            ).fetchone()
        except Exception:
            still_open = True
        if not still_open:
            continue
        try:
            current_price = float(client.get_ticker_price(symbol=sym))
        except Exception:
            continue
        if current_price >= sl_target:
            continue  # stop not breached
        sl_orders, _tp = get_order_coverage(client, sym)
        if sl_orders:
            continue  # protected: exchange will trigger the SL
        notional = qty * current_price
        if notional < DUST_THRESHOLD:
            continue
        filters = get_symbol_filters(client, sym)
        min_notional = filters.get("minNotional", 5.0)
        if notional >= min_notional:
            # Exit window open — take it
            if _discipline_exit(
                client, sym, pos, current_price, "sl_breach_exit", fixes, errors
            ):
                critical_failures += 1
        else:
            # Stranded: cannot sell, cannot protect → LOUD alert
            errors.append(
                f"{sym}: 🚨裸奔倉位——止損已穿(${current_price:.6f}<${sl_target:.6f})"
                f"但市值${notional:.2f}<${min_notional}，SL與市價平倉均被拒"
            )
            critical_failures += 1
            try:
                from src.notifier import FeishuNotifier
                FeishuNotifier().send_text(
                    f"🚨 ensure_tp_sl: {sym} 裸奔倉位（bug#29類）——止損已穿但市值 "
                    f"${notional:.2f} < minNotional ${min_notional}，"
                    f"無法掛SL也無法市價平倉，需人工介入"
                )
            except Exception as ne:
                logger.error(f"stranded-position alert send failed: {ne}")

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
        try:
            rows = conn.execute(
                "SELECT symbol, quantity, entry_price, strategy, opened_at FROM portfolio"
            ).fetchall()
        finally:
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
                        # FIX 2026-08-18: write SELL row to trades table (same gap as tp_breach)
                        if entry > 0:
                            trade_pnl = (current_price - entry) * qty
                        else:
                            trade_pnl = 0.0
                        try:
                            # bug#24: dedup against concurrent reconcile_fills booking
                            insert_sell_dedup(
                                conn, sym, sell_qty, current_price, trade_pnl, time.time()
                            )
                            conn.commit()
                        except Exception as e:
                            logger.warning(f"Failed to write SELL trade for {sym}: {e}")
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
    return 1 if critical_failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
