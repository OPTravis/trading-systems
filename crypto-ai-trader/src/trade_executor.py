"""
Trade execution — position sizing, order placement, and portfolio tracking.

This module is the SOLE entry point for executing trades. It orchestrates:
  - Kelly-based position sizing (via KellyPositionSizer)
  - Risk checks (circuit breakers, daily loss, drawdown)
  - Order placement (market buy, SL/TP in tiered/OCO/separate strategies)
  - Portfolio tracking and event publishing

Calculation delegation:
  - Exchange filters (LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL) → SmartOrder.get_symbol_filters()
  - Quantity precision → SmartOrder.apply_qty_precision()
  - SL/TP price calculation → inline percentage-based (strategies define pct directly)

Extracted from main.py for maintainability.
"""

import logging
import signal
import time
from math import floor

import numpy as np

from src.notifier import FeishuNotifier
from src.paper_trader import get_trading_client
from src.portfolio import PortfolioManager

logger = logging.getLogger(__name__)

# ── Trade executor risk parameters (loaded from unified risk config) ──
# These were previously hardcoded inside execute_auto_trade().
_DEFAULT_MIN_STOP_LOSS_PCT = 3.0
_DEFAULT_MAX_SINGLE_LOSS_PCT = 5.0
_DEFAULT_MAX_ACTIVE_POSITIONS = 5
_DEFAULT_SL_LIMIT_BUFFER_PCT = 0.015  # 1.5% below stop trigger price

try:
    from src.risk_config import get_risk_param
    _RISK_MIN_STOP_LOSS_PCT = get_risk_param(
        "trade_executor", "min_stop_loss_pct", _DEFAULT_MIN_STOP_LOSS_PCT
    )
    _RISK_MAX_SINGLE_LOSS_PCT = get_risk_param(
        "trade_executor", "max_single_loss_pct", _DEFAULT_MAX_SINGLE_LOSS_PCT
    )
    _RISK_MAX_ACTIVE_POSITIONS = get_risk_param(
        "trade_executor", "max_active_positions", _DEFAULT_MAX_ACTIVE_POSITIONS
    )
    _RISK_SL_LIMIT_BUFFER_PCT = get_risk_param(
        "trade_executor", "sl_limit_buffer_pct", _DEFAULT_SL_LIMIT_BUFFER_PCT
    )
except Exception as e:
    logger.warning("trade_executor.module: " + str(e))
    _RISK_MIN_STOP_LOSS_PCT = _DEFAULT_MIN_STOP_LOSS_PCT
    _RISK_MAX_SINGLE_LOSS_PCT = _DEFAULT_MAX_SINGLE_LOSS_PCT
    _RISK_MAX_ACTIVE_POSITIONS = _DEFAULT_MAX_ACTIVE_POSITIONS
    _RISK_SL_LIMIT_BUFFER_PCT = _DEFAULT_SL_LIMIT_BUFFER_PCT

# P2-6: Graceful shutdown flag — SIGTERM handler sets this to prevent new trades
_shutting_down = False


def _sigterm_handler(signum, frame):
    """SIGTERM handler — sets shutdown flag so execute_auto_trade refuses new trades."""
    global _shutting_down
    logger.warning("SIGTERM received — shutting down, no new trades will be executed")
    _shutting_down = True


# Register SIGTERM handler (best-effort; no-op if not in main thread)
try:
    signal.signal(signal.SIGTERM, _sigterm_handler)
except (ValueError, OSError):
    logger.info("SIGTERM handler not registered (not in main thread) — skipping")


def _check_price_deviation(
    client, symbol: str, price: float, sigma: float = 3.0
) -> bool:
    """Check if current price deviates abnormally from 14-kline average.

    Returns True if price is within normal range (safe to trade).
    Returns False if price is anomalous (>sigma std from mean).
    """
    try:
        klines = client.get_klines(symbol, "1h", limit=14)
        if len(klines) < 14:
            return True  # not enough data — pass through
        # klines from Binance API are list-of-lists: [open_time, open, high, low, close, ...]
        # Index 4 = close price (previously used k['close'] which failed on lists)
        closes = [float(k["close"]) for k in klines]
        mean = np.mean(closes)
        std = np.std(closes)
        if std == 0:
            return True  # flat price — no deviation to check
        z_score = abs(price - mean) / std
        if z_score > sigma:
            logger.warning(
                f"PRICE_ANOMALY: {symbol} price=${price:.6f} is {z_score:.1f}σ "
                f"from 14h mean ${mean:.6f} (std=${std:.6f}) — BLOCKED"
            )
            return False
        return True
    except Exception as e:
        logger.error(
            f"Price deviation check failed for {symbol}: {e} — BLOCKING trade (fail-closed)"
        )
        return False  # fail-closed: block trade on transient check failure


def _check_duplicate_order(client, symbol: str) -> bool:
    """Check if there's already a pending BUY order for this symbol.

    Returns True if no duplicate exists (safe to place order).
    Returns False if duplicate found (should block).
    """
    try:
        open_orders = client.get_open_orders(symbol)
        for o in open_orders:
            if o.get("side") == "BUY":
                logger.warning(
                    f"DUPLICATE_ORDER: {symbol} already has pending BUY "
                    f"order #{o.get('orderId')} — BLOCKED"
                )
                return False
        return True
    except Exception as e:
        logger.error(
            f"Duplicate order check failed for {symbol}: {e} — BLOCKING trade (fail-closed)"
        )
        return False  # fail-closed: block trade on transient check failure


def get_position_tier(score):
    """Determine position size tier based on opportunity score.

    Returns (base_pct, tier_label). Used by execute_auto_trade for position sizing.
    TODO: Migrate to KellyPositionSizer when fully integrated.
    """
    if score >= 90:
        return 0.50, "HIGH"
    elif score >= 75:
        return 0.30, "MEDIUM-HIGH"
    elif score >= 65:
        return 0.20, "MEDIUM"
    elif score >= 60:
        return 0.15, "CAUTIOUS"
    else:
        return 0.0, "SKIP"


def count_active_positions(client):
    """Count number of active positions (non-USDT balances with value > $1).

    Filters out NTRN (delisted) and dust coins worth less than $1.
    Uses batch ticker fetch (1 API call) instead of per-asset calls.
    """
    try:
        acct = client.get_account()
        # Batch fetch all prices in one call
        price_map = {}
        try:
            all_tickers = client.get_24hr_stats()
            if isinstance(all_tickers, list):
                price_map = {
                    t["symbol"]: float(t.get("last_price", 0))
                    for t in all_tickers
                    if "symbol" in t
                }
        except Exception as e:
            logger.debug(f"count_active_positions: batch ticker fetch failed: {e}")
        count = 0
        for b in acct["balances"]:
            free = float(b["free"]) + float(b["locked"])
            if free > 0 and b["asset"] not in ("USDT", "NTRN"):
                sym = b["asset"] + "USDT"
                price = price_map.get(sym, 0)
                if price > 0 and free * price >= 5.0:
                    count += 1
                elif price == 0:
                    # Can't get price — conservatively skip (don't inflate count)
                    logger.warning(
                        "count_active_positions: no price for %s, skipping", sym
                    )
        return count
    except Exception:
        logger.warning("count_active_positions: account fetch failed")
        return -1  # P1-8: fail-closed — return -1 so callers can detect error


def _send_execution_notification(
    notifier, symbol, strategy, tier_label, score,
    invest_pct, usdt_bal, invest_amount, kelly_result,
    executed_qty, price, reason, active_positions, max_positions, results,
):
    """Send Feishu notification after trade execution.

    Extracted from execute_auto_trade for maintainability (P0-5).
    """
    lines = [
        f"🚀 自動執行 - {symbol}",
        f"策略: {strategy.upper()} | 倉位級別: {tier_label} (Score: {score})",
        f"Kelly: {invest_pct*100:.1f}% of ${usdt_bal:.2f} = ${invest_amount:.2f}",
        f"勝率: {kelly_result.get('win_rate',0):.1%} | 信心: {kelly_result.get('confidence','N/A')}",
        f"買入: {executed_qty:.0f} @ ${price:.6f}",
        f"信號: {reason}",
        f"活躍持倉: {active_positions + 1}/{max_positions}",
        "",
        "📊 訂單:",
    ]
    for r in results:
        lines.append(f"  {r}")
    notifier.send_text("\n".join(lines))


def _pretrade_risk_checks(client, usdt_bal: float) -> dict:
    """Run all pre-trade risk checks and compute portfolio metrics.

    Returns dict with:
        blocked: bool — True if trade should be blocked
        reason: str — block reason (if blocked)
        total_invested: float — total value in non-USDT positions
        total_portfolio: float — USDT + all non-USDT positions
        dl_multiplier: float — daily loss tier size multiplier (1.0/0.5/0.0)
        sd_multiplier: float — stepwise drawdown size multiplier
    """
    result = {
        "blocked": False,
        "reason": "",
        "total_invested": 0.0,
        "total_portfolio": usdt_bal,
        "dl_multiplier": 1.0,
        "sd_multiplier": 1.0,
    }

    # ── Fetch account data ONCE and compute portfolio metrics ──
    try:
        _account_data = client.get_account()
        _price_map = {}
        for b in _account_data.get("balances", []):
            _asset = b["asset"]
            _qty = float(b.get("free", 0)) + float(b.get("locked", 0))
            if _qty > 0 and _asset not in ("USDT", "NTRN"):
                try:
                    if f"{_asset}USDT" not in _price_map:
                        _p = float(client.get_ticker_price(f"{_asset}USDT"))
                        _price_map[f"{_asset}USDT"] = _p
                    else:
                        _p = _price_map[f"{_asset}USDT"]
                    result["total_invested"] += _qty * _p
                    result["total_portfolio"] += _qty * _p
                except (ConnectionError, TimeoutError, ValueError, KeyError, OSError) as e:
                    logger.error("Failed to get asset price for %s: %s", _asset, e)
    except (ConnectionError, TimeoutError, ValueError, KeyError, OSError) as e:
        logger.error(f"Failed to fetch account data for pre-computation: {e}")

    total_value = result["total_portfolio"] if result["total_portfolio"] > usdt_bal else usdt_bal

    # ── Circuit breaker ──
    try:
        from src.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker()
        if cb.is_tripped():
            logger.warning("Circuit breaker tripped — blocking trade")
            result["blocked"] = True
            result["reason"] = "Circuit breaker tripped"
            return result
    except Exception as e:
        logger.warning(f"Circuit breaker check failed: {e}")
        result["blocked"] = True
        result["reason"] = "Circuit breaker check failed"
        return result

    # ── Daily loss circuit breaker ──
    try:
        from src.daily_loss_breaker import get_daily_loss_breaker
        dlb = get_daily_loss_breaker()
        dl_result = dlb.check_daily_loss(portfolio_value=total_value)
        if dlb.should_close_all():
            logger.warning(f"Daily loss breaker tier {dl_result['tier']} — close all")
            result["blocked"] = True
            result["reason"] = f"Daily loss breaker tier {dl_result['tier']}: close all"
            return result
        if dlb.should_block_new_trades():
            logger.warning(f"Daily loss breaker tier {dl_result['tier']} — blocked")
            result["blocked"] = True
            result["reason"] = f"Daily loss breaker tier {dl_result['tier']}: new trades blocked"
            return result
        result["dl_multiplier"] = dlb.get_position_size_multiplier()
    except Exception as e:
        logger.warning(f"Daily loss breaker check failed: {e}")
        result["blocked"] = True
        result["reason"] = f"Daily loss breaker check failed: {e}"
        return result

    # ── Stepwise drawdown ──
    try:
        from src.drawdown_breaker import DrawdownBreaker
        from src.stepwise_drawdown import get_drawdown_action
        _ddb = DrawdownBreaker(binance_client=client)
        _dd_check = _ddb.check_drawdown(total_value)
        _dd_pct = _dd_check.get("drawdown_pct", 0)
        _dd_action = get_drawdown_action(_dd_pct)
        result["sd_multiplier"] = _dd_action["size_multiplier"]
        if result["sd_multiplier"] < 1.0:
            logger.info(
                f"Stepwise drawdown: {_dd_pct:.1f}% → {_dd_action['level']} "
                f"(multiplier={result['sd_multiplier']}, {_dd_action['reason']})"
            )
        if _dd_action.get("block_new_trades"):
            result["blocked"] = True
            result["reason"] = f"Stepwise drawdown {_dd_pct:.1f}%: {_dd_action['reason']}"
            return result
    except Exception as e:
        logger.warning(f"Stepwise drawdown check failed (proceeding without): {e}")

    return result


def _record_trade_portfolio(
    client, symbol, executed_qty, avg_price, strategy,
    usdt_bal, invest_amount, fee_rate,
    invest_pct, bandit_context, bandit_multiplier,
):
    """Track executed trade in portfolio state and publish events.

    Extracted from execute_auto_trade for maintainability (P0-5).
    """
    # Track position in portfolio_state.json
    try:
        portfolio = PortfolioManager()
        try:
            actual_usdt = client.get_free_balance("USDT")
            portfolio.update_balance(actual_usdt)
        except Exception:
            logger.error(
                "Failed to fetch actual USDT balance for portfolio tracking",
                exc_info=True,
            )
            portfolio.update_balance(usdt_bal - invest_amount * (1 + fee_rate))
        portfolio.add_position(
            symbol=symbol,
            quantity=executed_qty,
            entry_price=avg_price,
            strategy=strategy,
            deduct_cash=True,
        )
        if symbol in portfolio.positions:
            portfolio.positions[symbol]["invest_pct"] = invest_pct
            if bandit_context:
                portfolio.positions[symbol]["bandit_context"] = bandit_context
                portfolio.positions[symbol]["bandit_multiplier"] = bandit_multiplier
            try:
                if portfolio._db is not None:
                    portfolio._db.portfolio_set(symbol, portfolio.positions[symbol])
            except Exception:
                logger.debug("Failed to persist invest_pct/bandit for %s", symbol)
    except Exception as e:
        logger.warning(f"Portfolio tracking failed: {e}")

    # Publish event to event bus (Phase 9)
    try:
        from src.event_bus import get_event_bus

        bus = get_event_bus()
        bus.publish(
            "trade_executed",
            {
                "symbol": symbol,
                "action": "BUY",
                "qty": executed_qty,
                "price": avg_price,
                "strategy": strategy,
            },
        )
        bus.publish(
            "position_opened",
            {
                "symbol": symbol,
                "entry_price": avg_price,
                "quantity": executed_qty,
                "strategy": strategy,
            },
        )
    except Exception as e:
        logger.debug(f"Event bus publish failed: {e}")


def _place_sl_tp_orders(
    client,
    notifier,
    symbol,
    executed_qty: float,
    price: float,
    p_prec: int,
    stop_loss_pct: float,
    tp_levels: list,
    _step_size: float,
    _qty_decimals: int,
    _min_notional: float,
    strategy_size_multiplier: float,
) -> dict:
    """Place SL and TP orders after a successful entry.

    Strategy selection:
      A) Small position (notional < 4× minNotional) → SL-only (full qty)
      B) Medium+ position → Tiered TP exits (40/40/20) with full SL
      C) If tiered fails → OCO fallback, then separate SL + TP

    Returns dict with keys: results (list[str]), sl_placed_qty (float),
    tp_placed_qty (float), oco_placed (bool).
    """
    results: list[str] = []
    sl_placed_qty = 0.0
    tp_placed_qty = 0.0
    oco_placed = False

    # Calculate SL price and buffered limit price
    sl_price = round(price * (1 - stop_loss_pct / 100), p_prec)
    SL_LIMIT_BUFFER_PCT = _RISK_SL_LIMIT_BUFFER_PCT  # 1.5% below stop trigger price
    sl_limit_price = round(sl_price * (1 - SL_LIMIT_BUFFER_PCT), p_prec)

    # Determine primary TP price (highest probability first TP level)
    primary_tp_pct = tp_levels[0]["pct"] if tp_levels else 10.0
    primary_tp_price = round(price * (1 + primary_tp_pct / 100), p_prec)

    # Helper to floor qty to step size
    def _round_qty(raw_qty):
        """Floor to step size — never round UP to avoid exceeding balance."""
        return round(floor(raw_qty / _step_size) * _step_size, _qty_decimals)

    # --- Strategy A: Small position → SL only ---
    full_qty_notional = executed_qty * price
    if full_qty_notional < _min_notional * 4:
        logger.info(
            "Small position $%.2f < $%.2f → SL-only mode",
            full_qty_notional,
            _min_notional * 4,
        )
        sl_qty = executed_qty
        sl = None
        for attempt in range(2):
            try:
                sl = client.place_order(
                    symbol,
                    "SELL",
                    "STOP_LOSS_LIMIT",
                    sl_qty,
                    price=sl_limit_price,
                    stop_price=sl_price,
                )
                if sl:
                    break
            except Exception:
                logger.error("SL order placement failed for %s", symbol, exc_info=True)
                if attempt == 0:
                    time.sleep(1)
        if sl:
            results.append(f"SL-only: {sl_qty} @ ${sl_price} (-{stop_loss_pct}%)")
            sl_placed_qty = sl_qty
        else:
            results.append("SL: FAILED")
            try:
                notifier.send_text(
                    f"🚨 URGENT: SL failed for {symbol}! Manual SL needed!"
                )
            except Exception:
                logger.error(
                    "Failed to send SL failure alert notification", exc_info=True
                )

    # --- Strategy B: Medium+ position → Tiered TP exits (40/40/20) ---
    else:
        tiered_ok = False
        _tiered_results = []
        _tiered_sl_placed = 0.0
        _tiered_tp_placed = 0.0
        try:
            tp1_qty = _round_qty(executed_qty * 0.40)
            tp3_qty = _round_qty(executed_qty * 0.20)
            tp2_qty = _round_qty(
                executed_qty - tp1_qty - tp3_qty
            )  # remainder avoids rounding gaps
            tiers = [
                (
                    1,
                    tp1_qty,
                    tp_levels[0]["pct"] if len(tp_levels) > 0 else primary_tp_pct,
                ),
                (
                    2,
                    tp2_qty,
                    (
                        tp_levels[1]["pct"]
                        if len(tp_levels) > 1
                        else tp_levels[0]["pct"] * 1.5
                    ),
                ),
                (
                    3,
                    tp3_qty,
                    (
                        tp_levels[2]["pct"]
                        if len(tp_levels) > 2
                        else tp_levels[0]["pct"] * 2.0
                    ),
                ),
            ]
            _all_tiers_valid = True
            for tier_num, tq, tp_pct_val in tiers:
                if tq < _step_size:
                    continue
                tp_p = round(price * (1 + tp_pct_val / 100), p_prec)
                if tq * tp_p < _min_notional:
                    _all_tiers_valid = False
                    break

            if _all_tiers_valid:
                # Step 1: Place SL covering FULL position
                sl = None
                for attempt in range(2):
                    try:
                        sl = client.place_order(
                            symbol,
                            "SELL",
                            "STOP_LOSS_LIMIT",
                            executed_qty,
                            price=sl_limit_price,
                            stop_price=sl_price,
                        )
                        if sl:
                            break
                    except Exception:
                        logger.error(
                            "Tiered SL placement failed for %s", symbol, exc_info=True
                        )
                        if attempt == 0:
                            time.sleep(1)
                if sl:
                    _tiered_results.append(
                        f"SL(full): {executed_qty} @ ${sl_price} (-{stop_loss_pct}%)"
                    )
                    _tiered_sl_placed = executed_qty
                else:
                    raise RuntimeError(
                        "Tiered SL failed — falling back to OCO/Strategy C"
                    )

                # Step 2: Place TP limit sells
                for tier_num, tq, tp_pct_val in tiers:
                    if tq < _step_size:
                        _tiered_results.append(f"TP{tier_num}: SKIPPED (qty too small)")
                        continue
                    tp_p = round(price * (1 + tp_pct_val / 100), p_prec)
                    tp_notional = tq * tp_p
                    if tp_notional < _min_notional:
                        _tiered_results.append(
                            f"TP{tier_num}(+{tp_pct_val}%): SKIPPED (notional ${tp_notional:.2f})"
                        )
                        continue
                    tpo = None
                    for attempt in range(2):
                        try:
                            tpo = client.place_order(
                                symbol, "SELL", "LIMIT", tq, price=tp_p
                            )
                            if tpo:
                                break
                        except Exception:
                            logger.error(
                                "Tiered TP%d placement failed for %s",
                                tier_num,
                                symbol,
                                exc_info=True,
                            )
                            if attempt == 0:
                                time.sleep(1)
                    if tpo:
                        _tiered_results.append(
                            f"TP{tier_num}(+{tp_pct_val}%): {tq} @ ${tp_p}"
                        )
                        _tiered_tp_placed += tq
                    else:
                        raise RuntimeError(
                            f"Tiered TP{tier_num} failed — falling back to OCO/Strategy C"
                        )

                tiered_ok = True
                results.extend(_tiered_results)
                sl_placed_qty = _tiered_sl_placed
                tp_placed_qty = _tiered_tp_placed
                logger.info(
                    "Tiered exits placed for %s: SL=full, TP placed=%s",
                    symbol,
                    _tiered_tp_placed,
                )
        except Exception as e:
            logger.warning(
                "Tiered exits failed for %s: %s — trying OCO fallback", symbol, e
            )
            try:
                _open = client.get_open_orders(symbol)
                for _o in _open:
                    logger.info(
                        "Cancelling tiered residue: %s order %s", symbol, _o.get("id")
                    )
                    client.cancel_order(symbol, _o.get("id"))
            except Exception as _ce:
                logger.error("Failed to cancel tiered residue for %s: %s", symbol, _ce)

        # --- OCO fallback (if tiered failed) ---
        if not tiered_ok:
            oco = None
            try:
                oco = client.place_oco(
                    symbol=symbol,
                    quantity=executed_qty,
                    tp_price=primary_tp_price,
                    sl_price=sl_price,
                )
            except Exception as e:
                logger.warning("OCO failed, falling back to separate orders: %s", e)

            if oco:
                oco_placed = True
                sl_placed_qty = executed_qty  # OCO covers full qty (both SL and TP)
                tp_placed_qty = 0  # Avoid double-counting in covered calc
                results.append(
                    f"OCO: TP {primary_tp_pct}% @ ${primary_tp_price} | SL -{stop_loss_pct}% @ ${sl_price}"
                )
                logger.info("OCO placed for %s: full qty covered", symbol)
            else:
                # --- Strategy C: Fallback → separate SL + TP orders ---
                logger.info("OCO unavailable, using separate SL + TP")

                tp_qty_list = []
                for i, tp in enumerate(tp_levels):
                    raw_tp = executed_qty * tp["size_pct"] / 100
                    tq = _round_qty(raw_tp)
                    if tq < _step_size:
                        tq = 0.0
                    tp_qty_list.append(tq)

                total_tp = sum(tp_qty_list)
                if total_tp > executed_qty * 0.70:
                    scale = (executed_qty * 0.70) / total_tp
                    tp_qty_list = [_round_qty(q * scale) for q in tp_qty_list]
                    total_tp = sum(tp_qty_list)

                sl_qty = round(executed_qty - total_tp, _qty_decimals)
                if sl_qty < _step_size and executed_qty >= _step_size * 3:
                    sl_qty = max(
                        _step_size, round(executed_qty * 0.30 / _step_size) * _step_size
                    )
                    sl_qty = round(sl_qty, _qty_decimals)
                    total_tp = round(executed_qty - sl_qty, _qty_decimals)
                    remaining = total_tp
                    tp_qty_list = []
                    for i, tp in enumerate(tp_levels):
                        tq = (
                            _round_qty(total_tp * tp["size_pct"] / 100)
                            if total_tp > 0
                            else 0
                        )
                        tq = min(tq, remaining)
                        if tq < _step_size:
                            tq = 0.0
                        tp_qty_list.append(tq)
                        remaining = round(remaining - tq, _qty_decimals)

                logger.info(
                    "Strategy C: SL=%s, TPs=%s (total=%s, executed=%s)",
                    sl_qty,
                    tp_qty_list,
                    round(sl_qty + sum(tp_qty_list), _qty_decimals),
                    executed_qty,
                )

                if sl_qty >= _step_size:
                    sl = None
                    for attempt in range(2):
                        try:
                            sl = client.place_order(
                                symbol,
                                "SELL",
                                "STOP_LOSS_LIMIT",
                                sl_qty,
                                price=sl_limit_price,
                                stop_price=sl_price,
                            )
                            if sl:
                                break
                        except Exception:
                            logger.error(
                                "Fallback SL order placement failed for %s",
                                symbol,
                                exc_info=True,
                            )
                            if attempt == 0:
                                time.sleep(1)
                    if sl:
                        results.append(
                            f"SL: {sl_qty} @ ${sl_price} (-{stop_loss_pct}%)"
                        )
                        sl_placed_qty = sl_qty
                    else:
                        results.append("SL: FAILED")
                        try:
                            notifier.send_text(
                                f"🚨 URGENT: SL failed for {symbol}! Manual SL needed!"
                            )
                        except Exception:
                            logger.error(
                                "Failed to send fallback SL failure alert",
                                exc_info=True,
                            )

                for i, (tp, tp_qty) in enumerate(zip(tp_levels, tp_qty_list)):
                    if tp_qty < _step_size:
                        continue
                    tp_price = round(price * (1 + tp["pct"] / 100), p_prec)
                    tp_notional = tp_qty * tp_price
                    if tp_notional < _min_notional:
                        results.append(
                            f"TP{i+1}(+{tp['pct']}%): SKIPPED (notional ${tp_notional:.2f})"
                        )
                        continue
                    tpo = None
                    for attempt in range(2):
                        try:
                            tpo = client.place_order(
                                symbol, "SELL", "LIMIT", tp_qty, price=tp_price
                            )
                            if tpo:
                                break
                        except Exception:
                            logger.error(
                                "TP limit order placement failed for %s",
                                symbol,
                                exc_info=True,
                            )
                            if attempt == 0:
                                time.sleep(1)
                    if tpo:
                        results.append(
                            f"TP{i+1}(+{tp['pct']}%): {tp_qty} @ ${tp_price}"
                        )
                        tp_placed_qty += tp_qty
                    else:
                        results.append(f"TP{i+1}: FAILED")

    # Check for uncovered units
    covered = sl_placed_qty + tp_placed_qty
    uncovered = executed_qty - covered
    if uncovered >= _step_size and sl_placed_qty > 0 and not oco_placed:
        extra_sl_qty = round(floor(uncovered / _step_size) * _step_size, _qty_decimals)
        logger.info("Placing extra SL for %s uncovered units", extra_sl_qty)
        extra_sl = None
        for attempt in range(2):
            try:
                extra_sl = client.place_order(
                    symbol,
                    "SELL",
                    "STOP_LOSS_LIMIT",
                    extra_sl_qty,
                    price=sl_limit_price,
                    stop_price=sl_price,
                )
                if extra_sl:
                    break
            except Exception:
                logger.error(
                    "Extra SL order for uncovered units failed for %s",
                    symbol,
                    exc_info=True,
                )
                if attempt == 0:
                    time.sleep(1)
        if extra_sl:
            results.append(f"Extra SL: {extra_sl_qty} @ ${sl_price}")
            sl_placed_qty += extra_sl_qty
            covered = sl_placed_qty + tp_placed_qty
    elif uncovered >= _step_size and sl_placed_qty == 0 and not oco_placed:
        results.append(f"🔴 未保護: {uncovered:.4f} units (no SL placed)")
        try:
            notifier.send_text(
                f"🔴🔴 {symbol} 完全無SL！{uncovered:.4f} 單位裸露！手動處理！"
            )
        except Exception:
            logger.error(
                "Failed to send 'No SL' critical alert notification", exc_info=True
            )
        try:
            emergency_sell = client.place_order(
                symbol,
                "SELL",
                "MARKET",
                uncovered,
            )
            if emergency_sell:
                results.append(
                    f"🟡 Emergency market sell: {uncovered:.4f} units (no SL possible)"
                )
                logger.warning(
                    "Emergency market sell executed for %s (%.4f units — no SL possible)",
                    symbol,
                    uncovered,
                )
        except Exception:
            logger.error(
                "Emergency market sell FAILED for %s — position is naked!",
                symbol,
                exc_info=True,
            )

    if sl_placed_qty + tp_placed_qty < executed_qty:
        remainder = executed_qty - sl_placed_qty - tp_placed_qty
        if remainder >= _step_size:
            results.append(f"注意: {remainder:.0f} 單位已由額外SL覆蓋")

    return {
        "results": results,
        "sl_placed_qty": sl_placed_qty,
        "tp_placed_qty": tp_placed_qty,
        "oco_placed": oco_placed,
    }


def execute_auto_trade(
    symbol,
    price,
    strategy,
    stop_loss_pct,
    tp_levels,
    stop_price,
    max_hold,
    signals,
    reason,
    score=70,
    cash_reserve_pct=30,
    max_position_pct=15,
    max_total_exposure_pct=70,
    strategy_size_multiplier=1.0,
):
    """Execute trade automatically with Kelly-optimal position sizing.

    Position size uses Half-Kelly criterion based on historical win rate
    and trade outcome data. Falls back to tier-based sizing when
    insufficient history exists.

    Returns dict with success status and order details.
    """
    import copy
    import json
    import os

    from src.utils import get_project_root

    # P2-1: Structured logging context — generate trade_id for correlation
    import uuid as _uuid
    _trade_id = f"{symbol}_{int(time.time())}_{_uuid.uuid4().hex[:6]}"

    # P2-6: Graceful shutdown — refuse new trades if SIGTERM was received
    if _shutting_down:
        logger.warning(
            f"[trade_id={_trade_id}] execute_auto_trade BLOCKED — shutdown in progress, "
            f"symbol={symbol}"
        )
        return {"success": False, "reason": "shutdown_in_progress"}

    logger.info(
        f"[trade_id={_trade_id}] execute_auto_trade START symbol={symbol} "
        f"strategy={strategy} score={score} price=${price:.6f}"
    )

    # Safety: Ensure stop_loss_pct is never 0 or negative (hard minimum 3%)
    MIN_STOP_LOSS_PCT = _RISK_MIN_STOP_LOSS_PCT
    MAX_SINGLE_LOSS_PCT = _RISK_MAX_SINGLE_LOSS_PCT  # Maximum single trade loss as % of position
    if stop_loss_pct <= 0:
        logger.error(f"stop_loss_pct={stop_loss_pct}% is invalid (≤0), blocking trade")
        return {"success": False, "reason": f"Invalid stop_loss_pct={stop_loss_pct}%"}
    if stop_loss_pct < MIN_STOP_LOSS_PCT:
        logger.warning(
            f"stop_loss_pct={stop_loss_pct}% is below minimum {MIN_STOP_LOSS_PCT}%, "
            f"adjusting to {MIN_STOP_LOSS_PCT}%"
        )
        stop_loss_pct = MIN_STOP_LOSS_PCT
        stop_price = price * (1 - stop_loss_pct / 100)

    # DCA exclusion: block auto-trade on coins managed by DCA monitor
    # Skip when DCA_CHECK_DISABLED=1 (for testing) or when state file doesn't exist
    _dca_coins: set = set()
    if not os.environ.get("DCA_CHECK_DISABLED"):
        _dca_state_file = str(get_project_root() / "data" / "dca_state.json")
        try:
            if os.path.exists(_dca_state_file):
                with open(_dca_state_file) as _f:
                    _dca = json.load(_f)
                if not _dca.get("stop_loss", {}).get("triggered"):
                    for _tier in ("tier1", "tier2", "tier3", "tier4"):
                        if _dca.get(_tier, {}).get("executed") or _tier in (
                            "tier3",
                            "tier4",
                        ):
                            for _buy in _dca.get(_tier, {}).get("buys", []):
                                _dca_coins.add(_buy.split("/")[0])
                            if not _dca.get(_tier, {}).get("buys"):
                                _dca_coins.update(["BTC", "ETH", "SOL"])
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"DCA state file corrupted: {e}, skipping DCA exclusion")
        except Exception as e:
            logger.warning(f"Failed to load DCA state: {e}, skipping DCA exclusion")

    _base = symbol.replace("/USDT", "").replace("USDT", "")
    if _base in _dca_coins:
        return {
            "success": False,
            "error": f"DCA-managed coin {_base}, skipped by auto-trade",
        }

    tp_levels = copy.deepcopy(tp_levels)  # prevent mutation of cached strategy data
    client = get_trading_client()
    notifier = FeishuNotifier()

    # Get available USDT balance
    usdt_bal = client.get_free_balance("USDT")
    if usdt_bal < 10:
        return {"success": False, "error": f"Insufficient USDT: ${usdt_bal:.2f}"}

    # ── Pre-trade risk checks (circuit breakers, daily loss, drawdown) ──
    risk_check = _pretrade_risk_checks(client, usdt_bal)
    if risk_check["blocked"]:
        return {"success": False, "error": risk_check["reason"]}
    _total_invested = risk_check["total_invested"]
    _total_portfolio = risk_check["total_portfolio"]
    _dl_multiplier = risk_check["dl_multiplier"]
    _sd_multiplier = risk_check["sd_multiplier"]

    # Count existing positions
    active_positions = count_active_positions(client)
    max_positions = _RISK_MAX_ACTIVE_POSITIONS

    # P1-8: fail-closed — if count failed, block trade
    if active_positions < 0:
        return {
            "success": False,
            "error": "count_active_positions failed (account fetch error) — blocking trade for safety",
        }

    if active_positions >= max_positions:
        return {
            "success": False,
            "error": f"Max positions reached: {active_positions}/{max_positions}",
        }

    # Score below minimum threshold — no trade regardless of Kelly
    if score < 60:
        logger.info(f"Score {score} below minimum threshold (60), skipping trade")
        return {"success": False, "error": f"Score too low: {score} (min 60)"}

    # ── Position sizing: Kelly-first, tier-fallback ──
    # KellyPositionSizer uses historical win-rate data for optimal sizing.
    # When insufficient history (< 10 trades), falls back to tier-based sizing.
    from src.fee_optimizer import FeeOptimizer
    from src.kelly_sizer import KellyPositionSizer
    from src.state_db import get_state_db

    db = get_state_db()
    kelly = KellyPositionSizer(state_db=db)
    fee_opt = FeeOptimizer(client)

    # Primary TP level as take_profit_pct for Kelly R/R calculation
    tp_pct = tp_levels[0]["pct"] if tp_levels else 10.0

    kelly_result = kelly.get_position_size(
        symbol=symbol,
        balance=usdt_bal,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=tp_pct,
        signal_score=score,
        use_historical=True,
    )

    # Actual fee rate (not flat 1%)
    fees = fee_opt.get_effective_fees()
    fee_rate = fees["taker_fee"]  # 0.001 or 0.00075 with BNB
    fee_reserve = 1.0 - fee_rate * 2  # buy + sell

    kelly_confidence = kelly_result.get("confidence", "")
    kelly_active = "estimated" not in kelly_confidence.lower()

    if kelly_active:
        # ── Kelly-driven sizing (sufficient history) ──
        kelly_result = kelly.adjust_for_portfolio(
            kelly_result,
            current_positions=active_positions,
            max_positions=max_positions,
        )
        invest_pct = kelly_result["position_pct"]
        invest_amount = usdt_bal * invest_pct
        invest_amount *= fee_reserve
        # NOTE: invest_pct NOT adjusted by fee_reserve here — downstream
        # portfolio tracking (line ~696) already applies fees via fee_rate.
        # Applying twice would double-count.

        if invest_pct <= 0 or invest_amount < 10:
            logger.info(
                f"Kelly position too small: {invest_pct*100:.2f}% (${invest_amount:.2f}). "
                f"win_rate={kelly_result.get('win_rate',0):.1%} confidence={kelly_confidence}"
            )
            return {
                "success": False,
                "error": f"Position too small: {invest_pct*100:.1f}% (${invest_amount:.2f})",
            }
    else:
        # ── Tier-based fallback (insufficient history) ──
        base_pct, tier_label_tmp = get_position_tier(score)
        if base_pct == 0:
            logger.info(f"Score {score} below threshold, skipping trade")
            return {"success": False, "error": f"Score too low: {score} (min 60)"}

        # Fallback fee reserve — conservative 1% until Kelly path takes over
        _old_fee_reserve = 0.99

        position_scale = 1.0
        if active_positions == 4:
            position_scale = 0.35
        elif active_positions == 3:
            position_scale = 0.50
        elif active_positions == 2:
            position_scale = 0.65
        elif active_positions == 1:
            position_scale = 0.80

        invest_pct = base_pct * position_scale * _old_fee_reserve
        invest_amount = usdt_bal * invest_pct
        fee_rate = 0.001  # conservative default for fallback
        kelly_result = {
            "win_rate": 0,
            "confidence": "FALLBACK (tier-based, insufficient history)",
            "reward_risk": 0,
            "reason": "Tier-based fallback: not enough trade history for Kelly",
            "position_pct": invest_pct,
        }
        logger.info(f"Kelly fallback to tier-based: confidence={kelly_confidence}")

    # ── Regime-aware cash reserve cap ──
    # Ensure we never invest more than (100 - cash_reserve_pct)% of USDT balance.
    # cash_reserve_pct comes from strategy_adaptor (regime + BTC trend + volatility).
    max_invest = usdt_bal * (1.0 - cash_reserve_pct / 100.0)
    if invest_amount > max_invest:
        logger.info(
            f"Cash reserve cap: ${invest_amount:.2f} → ${max_invest:.2f} "
            f"(reserve={cash_reserve_pct}%)"
        )
        invest_amount = max_invest
        invest_pct = invest_amount / usdt_bal if usdt_bal > 0 else 0

    # ── Single position size cap ──
    # No single trade can exceed max_position_pct of available USDT.
    max_single = usdt_bal * max_position_pct / 100.0
    if invest_amount > max_single:
        logger.info(
            f"Position cap: ${invest_amount:.2f} → ${max_single:.2f} "
            f"(max_position_pct={max_position_pct}%)"
        )
        invest_amount = max_single
        invest_pct = invest_amount / usdt_bal if usdt_bal > 0 else 0

    # ── Total exposure cap ──
    # Total invested across all positions cannot exceed max_total_exposure_pct of portfolio.
    # P1-1: Reuse pre-computed _total_invested and _total_portfolio
    try:
        _max_exposure = _total_portfolio * max_total_exposure_pct / 100.0
        if _total_invested + invest_amount > _max_exposure:
            _allowed = max(0, _max_exposure - _total_invested)
            if _allowed < invest_amount:
                logger.info(
                    f"Exposure cap: ${invest_amount:.2f} → ${_allowed:.2f} "
                    f"(invested=${_total_invested:.2f}, max={max_total_exposure_pct}%)"
                )
                invest_amount = _allowed
                invest_pct = invest_amount / usdt_bal if usdt_bal > 0 else 0
    except Exception as e:
        logger.warning(f"Exposure cap check failed (proceeding without): {e}")

    # ── ContextualBandit position size multiplier ──
    # Thompson Sampling bandit that learns optimal sizing based on market context.
    _bandit_multiplier = 1.0
    _bandit_context = {}
    try:
        from src.contextual_bandit import DEFAULT_SIZE, get_contextual_bandit

        bandit = get_contextual_bandit()

        # Build context from available market data
        _bandit_hmm = "sideways"
        _bandit_fng = 50.0
        _bandit_btc_trend = "NEUTRAL"
        try:
            conn = db._get_conn()
            hmm_row = conn.execute("SELECT value FROM kv WHERE key = 'hmm_regime'").fetchone()
            if hmm_row:
                import json as _json_hmm
                hmm_data = _json_hmm.loads(hmm_row["value"])
                _bandit_hmm = hmm_data.get("regime", "sideways").lower()
        except Exception:
            logger.debug("ContextualBandit: HMM regime fetch failed", exc_info=True)

        try:
            from src.data_feed import FearGreedIndex
            fng = FearGreedIndex()
            fng_data = fng.get_current()
            if fng_data and "value" in fng_data:
                _bandit_fng = float(fng_data["value"])
        except Exception:
            logger.debug("ContextualBandit: FearGreed fetch failed", exc_info=True)

        try:
            conn = db._get_conn()
            btc_row = conn.execute("SELECT value FROM kv WHERE key = 'btc_trend'").fetchone()
            if btc_row:
                _bandit_btc_trend = str(btc_row["value"]).upper()
        except Exception:
            logger.debug("ContextualBandit: BTC trend fetch failed", exc_info=True)

        _bandit_heat = "cold"
        if active_positions >= 4:
            _bandit_heat = "hot"
        elif active_positions >= 2:
            _bandit_heat = "warm"

        _bandit_context = {
            "hmm_regime": _bandit_hmm,
            "fear_greed": _bandit_fng,
            "btc_trend": _bandit_btc_trend,
            "portfolio_heat": _bandit_heat,
        }
        _bandit_multiplier = bandit.recommend_size(_bandit_context)
        if _bandit_multiplier != DEFAULT_SIZE:
            logger.info(
                f"ContextualBandit: multiplier={_bandit_multiplier:.2f} "
                f"context={_bandit_context}"
            )
    except Exception as e:
        logger.warning(f"ContextualBandit unavailable (using 1.0x): {e}")
        _bandit_multiplier = 1.0

    # ── Position size multipliers (strategy + daily loss tier + stepwise drawdown + bandit) ──
    # P0-3: strategy_size_multiplier (from strategy_adaptor) was previously ignored,
    # causing FEAR regime positions to be 30-40% oversized.
    _effective_multiplier = max(
        0.15, strategy_size_multiplier * _dl_multiplier * _sd_multiplier * _bandit_multiplier
    )
    if _effective_multiplier < 1.0:
        _orig = invest_amount
        invest_amount *= _effective_multiplier
        logger.info(
            f"Size multiplier {_effective_multiplier:.3f}x "
            f"(strategy={strategy_size_multiplier:.2f} × daily_loss={_dl_multiplier:.2f} × drawdown={_sd_multiplier:.2f}): "
            f"${_orig:.2f} → ${invest_amount:.2f}"
        )
        invest_pct = invest_amount / usdt_bal if usdt_bal > 0 else 0

    # Safety: Single trade max loss limit (3% of total portfolio value)
    # Calculate maximum loss for this trade: position_size * stop_loss_pct
    # P1-1: Reuse pre-computed _total_portfolio
    max_loss_pct_of_portfolio = 3.0  # Maximum 3% of portfolio per trade
    try:
        max_loss_amount = _total_portfolio * max_loss_pct_of_portfolio / 100.0
        potential_loss = invest_amount * stop_loss_pct / 100.0
        if potential_loss > max_loss_amount:
            # Reduce position size to limit max loss
            adjusted_invest = max_loss_amount / (stop_loss_pct / 100.0)
            logger.warning(
                f"Single trade loss limit: potential loss ${potential_loss:.2f} > "
                f"max ${max_loss_amount:.2f} ({max_loss_pct_of_portfolio}% of portfolio). "
                f"Reducing position: ${invest_amount:.2f} → ${adjusted_invest:.2f}"
            )
            invest_amount = adjusted_invest
            invest_pct = invest_amount / usdt_bal if usdt_bal > 0 else 0
    except Exception as e:
        logger.warning(f"Single trade loss limit check failed (proceeding without): {e}")

    # Final minimum check — caps may have reduced below Binance minimum
    if invest_amount < 10:
        return {
            "success": False,
            "error": f"Caps reduced position below $10 minimum: ${invest_amount:.2f}",
        }

    # Tier label for logging (informational only for Kelly mode)
    _, tier_label = get_position_tier(score)

    # Fetch exchange filters once (stepSize, minQty, minNotional)
    # Delegates to SmartOrder (pure calculation module) for filter data.
    # SmartOrder does NOT execute trades — it only provides exchange metadata.
    _step_size = 1.0
    _qty_decimals = 0
    _min_qty = 1.0
    _min_notional = 5.0
    try:
        from src.smart_order import SmartOrder

        _so = SmartOrder(client)
        _filters = _so.get_symbol_filters(symbol)
        if _filters:
            _step_size = _filters.get("stepSize", 1.0)
            _qty_decimals = _filters.get("qty_decimals", 0)
            _min_qty = _filters.get("minQty", 1.0)
            _min_notional = _filters.get("minNotional", 5.0)
    except Exception:
        logger.debug(
            "execute_auto_trade: SmartOrder filter fetch failed, using defaults"
        )

    raw_qty = invest_amount / price
    qty = round(raw_qty / _step_size) * _step_size
    qty = round(qty, _qty_decimals)
    if qty < _min_qty:
        return {
            "success": False,
            "error": f"Qty too small: {qty} (min {_min_qty}). Invest amount: ${invest_amount:.2f}",
        }

    logger.info(
        f"Kelly: {tier_label} | Score: {score} | WinRate: {kelly_result.get('win_rate',0):.1%} | "
        f"Confidence: {kelly_result.get('confidence','N/A')} | "
        f"Pct: {invest_pct*100:.1f}% | Qty: {qty} | Active pos: {active_positions}"
    )

    results = []
    executed_qty = 0.0

    # ── P0 #1: 異常價格過濾 (flash crash / pump protection) ──
    if not _check_price_deviation(client, symbol, price):
        return {
            "success": False,
            "error": f"Price anomaly: {symbol} ${price:.6f} deviates >3σ from 14h avg",
        }

    # ── P0 #2: 雙重下單防護 ──
    if not _check_duplicate_order(client, symbol):
        return {
            "success": False,
            "error": f"Duplicate order: {symbol} already has pending BUY",
        }

    # Market buy - use TWAP for large orders, MARKET for small ones
    from src.twap_vwap import should_use_twap, execute_twap

    if should_use_twap(invest_amount, threshold=100.0):
        logger.info(
            "TWAP: $%.2f >= $100 → splitting into %d slices over 2min",
            invest_amount, 5,
        )
        twap_results = execute_twap(
            client, symbol, "BUY", qty,
            duration_minutes=2, num_slices=5, dry_run=False,
        )
        if twap_results:
            filled_slices = [s for s in twap_results if s.get("success")]
            if filled_slices:
                total_filled = sum(s.get("filled_qty", 0) for s in filled_slices)
                avg_price = (
                    sum(s.get("filled_qty", 0) * s.get("price", 0) for s in filled_slices)
                    / total_filled if total_filled > 0 else price
                )
                executed_qty = total_filled
                buy_result = {"status": "TWAP_FILLED"}
                results.append(f"TWAP BUY: {executed_qty:.6f} @ avg ${avg_price:.6f} ({len(filled_slices)}/{len(twap_results)} slices)")
            else:
                logger.warning("TWAP: all slices failed, falling back to MARKET")
                buy_result = client.place_market_buy(symbol, qty)
        else:
            logger.warning("TWAP: returned empty, falling back to MARKET")
            buy_result = client.place_market_buy(symbol, qty)
    else:
        buy_result = client.place_market_buy(symbol, qty)

    if buy_result is None:
        return {
            "success": False,
            "error": f"BUY MARKET failed - {symbol} may be unavailable or balance insufficient",
        }

    # Get actual executed quantity from fills
    # Skip fills parsing if TWAP already populated executed_qty and avg_price
    fills = buy_result.get("fills", [])
    if buy_result.get("status") == "TWAP_FILLED":
        pass  # executed_qty and avg_price already set by TWAP path
    elif fills:
        executed_qty = sum(float(f.get("qty", 0)) for f in fills)
        avg_price = (
            sum(float(f.get("price", 0)) * float(f.get("qty", 0)) for f in fills)
            / executed_qty
            if executed_qty > 0
            else price
        )
        results.append(f"BUY: {executed_qty:.0f} @ ${avg_price:.6f}")

        # ── P1 #1: 滑點追蹤 (fill vs expected price) ──
        slippage_pct = (avg_price - price) / price * 100 if price > 0 else 0
        if abs(slippage_pct) > 0.1:  # >0.1% slippage is notable
            logger.warning(
                f"SLIPPAGE: {symbol} fill=${avg_price:.6f} vs expected=${price:.6f} "
                f"({slippage_pct:+.2f}%)"
            )
            results.append(
                f"Slippage: {slippage_pct:+.2f}% (fill ${avg_price:.6f} vs expected ${price:.6f})"
            )
    else:
        executed_qty = qty
        avg_price = price
        results.append(f"BUY: {qty} @ market (fills unavailable)")

    # Get price precision for this symbol
    p_prec = client.get_price_precision(symbol)

    # Query actual available balance after buy (accounts for trading fees)
    # executed_qty from fills is GROSS; actual free balance is NET of fees
    try:
        # M3 fix: handle BUSDUSDT edge case and multiple quote suffixes
        asset = symbol
        for suffix in ("USDT", "BUSD", "USDC"):
            if asset.endswith(suffix):
                asset = asset[: -len(suffix)]
                break
        acct = client.get_account()
        for b in acct.get("balances", []):
            if b["asset"] == asset:
                actual_free = float(b["free"])
                if actual_free > 0 and actual_free < executed_qty:
                    logger.info(
                        "Fee-adjusted qty: %.8f -> %.8f (fee=%.8f)",
                        executed_qty,
                        actual_free,
                        executed_qty - actual_free,
                    )
                    executed_qty = actual_free
                break
    except Exception as e:
        logger.warning("Could not query free balance, using fills qty: %s", e)

    # Calculate qty allocation: TP orders first, SL covers the rest
    # Reserve qty for SL first, then distribute TP from remaining
    # For small positions, check if splitting would violate minNotional
    total_tp_pct = sum(tp["size_pct"] for tp in tp_levels)
    if total_tp_pct > 70:
        # Cap total TP to 70%, SL gets 30% minimum
        scale = 70 / total_tp_pct
        for tp in tp_levels:
            tp["size_pct"] = round(tp["size_pct"] * scale)
        total_tp_pct = sum(tp["size_pct"] for tp in tp_levels)

    # ── Place SL/TP orders (delegates to extracted helper) ──
    sltp_result = _place_sl_tp_orders(
        client, notifier, symbol, executed_qty, price, p_prec,
        stop_loss_pct, tp_levels, _step_size, _qty_decimals, _min_notional,
        strategy_size_multiplier,
    )
    results.extend(sltp_result["results"])
    sl_placed_qty = sltp_result["sl_placed_qty"]
    tp_placed_qty = sltp_result["tp_placed_qty"]
    oco_placed = sltp_result["oco_placed"]

    # Send execution notification (extracted helper)

    _send_execution_notification(
        notifier, symbol, strategy, tier_label, score,
        invest_pct, usdt_bal, invest_amount, kelly_result,
        executed_qty, price, reason, active_positions, max_positions, results,
    )

    # Track position and publish events (extracted helper)
    _record_trade_portfolio(
        client, symbol, executed_qty, avg_price, strategy,
        usdt_bal, invest_amount, fee_rate,
        invest_pct, _bandit_context, _bandit_multiplier,
    )

    logger.info(
        f"[trade_id={_trade_id}] execute_auto_trade SUCCESS symbol={symbol} "
        f"qty={executed_qty:.6f} invest_pct={invest_pct*100:.1f}% active_positions={active_positions + 1}"
    )

    return {
        "success": True,
        "symbol": symbol,
        "trade_id": _trade_id,
        "qty": executed_qty,
        "price": price,
        "strategy": strategy,
        "tier": tier_label,
        "score": score,
        "invest_pct": round(invest_pct * 100, 1),
        "active_positions": active_positions + 1,
        "kelly": {
            "position_pct": round(kelly_result["position_pct"] * 100, 1),
            "win_rate": round(kelly_result.get("win_rate", 0) * 100, 1),
            "confidence": kelly_result.get("confidence", "N/A"),
            "reward_risk": kelly_result.get("reward_risk", 0),
            "reason": kelly_result.get("reason", ""),
        },
        "orders": results,
    }
