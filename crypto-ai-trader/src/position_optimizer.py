"""
Position Optimizer - Smart position switching based on opportunity cost analysis.

Rules:
- Trigger: existing position 24h change < -5% OR new coin score - existing score > 20
- Frequency: max 1 switch per coin per 4 hours
- Ratio: 100% full switch
- Blacklist: skip coins with 24h change > +30%
- Fee: Binance VIP0 spot 0.1% per side, total switch cost = 0.2%
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PositionOptimizer:
    """Analyzes existing positions vs market opportunities and triggers switches."""

    # Thresholds
    EXISTING_LOSS_THRESHOLD = (
        -3.0
    )  # 24h change < -3% triggers switch (was -5, too conservative)
    SCORE_GAP_THRESHOLD = (
        10.0  # new score - existing score > 10 triggers switch (was 20, unreachable)
    )
    BLACKLIST_24H_CHANGE = 30.0  # skip coins with 24h change > +30%
    SWITCH_FEE_PCT = 0.5  # total cost for sell+buy: 0.1% fee × 2 + ~0.3% slippage (market orders)
    MIN_SWITCH_INTERVAL_HOURS = (
        2  # min hours between switches for same coin (was 4, too slow)
    )
    LOW_SCORE_EXIT_THRESHOLD = (
        50.0  # score below this → exit to USDT (was 40, too lenient)
    )
    DUST_EXIT_USDT = 20.0  # exit positions below this value (dust)
    MIN_EXPECTED_GAIN_PCT = 0.5  # minimum expected gain after fees to justify switch

    # Smart activation thresholds
    VOLATILITY激活_THRESHOLD = 2.0  # BTC 24h > 2% → activate optimizer
    POSITION_LOSS激活_THRESHOLD = -2.0  # any position 24h < -2% → activate

    def __init__(self, binance_client, portfolio, market_scanner):
        self.bc = binance_client
        self.portfolio = portfolio
        self.scanner = market_scanner
        self._last_switch_time: Dict[str, float] = {}  # symbol -> timestamp
        self._load_switch_times()

    def should_activate(
        self,
        btc_change_24h: float = 0.0,
        position_24h_changes: Optional[Dict[str, float]] = None,
    ) -> bool:
        """Smart activation: only run optimizer when market conditions warrant it.

        Activates when ANY of:
        - BTC 24h change > ±2% (volatile market)
        - Any position 24h change < -2% (underperforming)
        - Market regime is GREED/EXTREME_GREED (opportunity-rich)

        Returns False in flat markets with stable positions (no-op saves resources).
        """
        # Condition 1: BTC volatility
        if abs(btc_change_24h) >= self.VOLATILITY激活_THRESHOLD:
            logger.info(
                f"Optimizer activated: BTC 24h={btc_change_24h:+.1f}% (volatility)"
            )
            return True

        # Condition 2: Any position losing
        if position_24h_changes:
            for sym, change in position_24h_changes.items():
                if change <= self.POSITION_LOSS激活_THRESHOLD:
                    logger.info(
                        f"Optimizer activated: {sym} 24h={change:+.1f}% (underperforming)"
                    )
                    return True

        # Condition 3: Flat market — skip optimization
        logger.info(
            f"Optimizer skipped: BTC 24h={btc_change_24h:+.1f}%, no position losses >2%"
        )
        return False

    def _load_switch_times(self):
        """Restore switch cooldowns from StateDB kv store."""
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            # Scan all kv keys starting with 'switch:last:'
            # Since kv doesn't have prefix scan, we use a different approach
            # Store all switch times in a single key
            stored = db.kv_get("position_optimizer:switch_times", {})
            if stored:
                self._last_switch_time = {k: float(v) for k, v in stored.items()}
                logger.info(
                    f"Loaded {len(self._last_switch_time)} switch cooldowns from StateDB"
                )
        except Exception as e:
            logger.warning(f"Failed to load switch times from StateDB: {e}")

    def _save_switch_times(self):
        """Persist switch cooldowns to StateDB kv store."""
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            db.kv_set("position_optimizer:switch_times", self._last_switch_time)
        except Exception as e:
            logger.warning(f"Failed to save switch times to StateDB: {e}")

    def analyze_and_switch(
        self,
        dry_run: bool = True,
        opportunities: Optional[List[Dict]] = None,
        btc_change_24h: float = 0.0,
    ) -> List[Dict]:
        """
        Main entry: analyze all positions and execute switches if conditions met.

        Args:
            dry_run: if True, only log decisions without executing trades
            opportunities: pre-computed scan results (avoids redundant scan_all)
            btc_change_24h: BTC 24h change for smart activation

        Returns:
            List of switch decisions made
        """
        decisions: List[Dict] = []

        # 1. Get existing positions
        positions = self.portfolio.get_all_positions()
        if not positions:
            logger.info("No existing positions to optimize")
            return decisions

        # 2. Smart activation check
        position_24h = {}
        for pos in positions:
            try:
                ticker = self.bc.get_24hr_stats(symbol=pos["symbol"])
                position_24h[pos["symbol"]] = float(ticker.get("price_change_pct", 0))
            except Exception:
                logger.error(
                    "Failed to get 24h change for %s during activation check",
                    pos["symbol"],
                    exc_info=True,
                )
        if not self.should_activate(
            btc_change_24h=btc_change_24h, position_24h_changes=position_24h
        ):
            return decisions

        # 3. Get market opportunities (use pre-computed or fetch)
        if opportunities is None:
            opportunities = self.scanner.scan_all()
        if not opportunities:
            logger.warning("Market scan returned no opportunities")
            return decisions

        # Sort by score descending
        opportunities.sort(key=lambda x: x.get("score", 0), reverse=True)
        top_opportunities = opportunities[:20]

        # 3. Analyze each position
        for pos in positions:
            pos["symbol"]
            decision = self._analyze_position(pos, top_opportunities)
            if decision:
                decisions.append(decision)
                if not dry_run:
                    self._execute_switch(decision)
                else:
                    logger.info(f"[DRY RUN] Would execute: {decision}")

        return decisions

    def _get_position_score(self, symbol: str, opportunities: List[Dict]) -> float:
        """Get score for a held symbol — from opportunities list or by direct scoring.

        This ensures existing positions are always scored, even if they
        fell out of the scanner's top 20.
        """
        # 1. Check if already in opportunities
        for opp in opportunities:
            if opp["symbol"] == symbol:
                return opp.get("score", 0)

        # 2. Not in top 20 — score it directly
        try:
            symbol.replace("USDT", "")
            # Use scanner's analyze_coin for a full 11-factor score
            coin_data = {
                "symbol": symbol,
                "price": 0,
                "volume_24h": 0,
                "price_change_24h": 0,
                "rank": 0,
                "volume_surge": False,
            }
            # Fetch minimal data for scoring
            try:
                stats = self.bc.get_24hr_stats(symbol)
                if stats:
                    coin_data["price"] = float(stats.get("last_price", 0))
                    coin_data["volume_24h"] = float(stats.get("quote_volume", 0))
                    coin_data["price_change_24h"] = float(
                        stats.get("price_change_pct", 0)
                    )
            except Exception:
                logger.error(
                    "Failed to fetch stats for direct scoring of %s",
                    symbol,
                    exc_info=True,
                )

            result = self.scanner._analyze_coin(coin_data)
            if result and "score" in result:
                score = result["score"]
                logger.info(f"Direct score for {symbol}: {score:.1f} (not in top 20)")
                return score
        except Exception as e:
            logger.debug(f"Direct scoring failed for {symbol}: {e}")

        return 0.0

    def _analyze_position(self, pos: Dict, opportunities: List[Dict]) -> Optional[Dict]:
        """Analyze single position vs market opportunities.

        Three exit conditions:
        1. 24h loss > 5% AND a better alternative exists → switch
        2. Score gap > 20 AND existing_score > 0 → switch
        3. Score < LOW_SCORE_EXIT_THRESHOLD → exit to USDT (no alt needed)
        """
        symbol = pos["symbol"]
        position_value = pos.get("position_value", 0)
        pos.get("entry_price", 0)
        pos.get("quantity", 0)

        # Get 24h change for existing position
        try:
            ticker = self.bc.get_24hr_stats(symbol=symbol)
            existing_24h_change = float(ticker.get("price_change_pct", 0))
        except Exception as e:
            logger.warning(f"Failed to get 24h change for {symbol}: {e}")
            existing_24h_change = 0.0

        # Get existing position score — actively, not just from top 20
        existing_score = self._get_position_score(symbol, opportunities)

        # Check cooldown
        now = time.time()
        last_switch = self._last_switch_time.get(symbol, 0)
        hours_since_last = (now - last_switch) / 3600
        if hours_since_last < self.MIN_SWITCH_INTERVAL_HOURS:
            logger.debug(
                f"{symbol}: cooldown active ({hours_since_last:.1f}h < {self.MIN_SWITCH_INTERVAL_HOURS}h)"
            )
            return None

        # Find best alternative opportunity
        best_alt = None
        best_score_gap = 0.0

        for opp in opportunities:
            opp_symbol = opp["symbol"]
            if opp_symbol == symbol:
                continue  # skip same coin

            # Check cooldown on target symbol too
            last_switch_opp = self._last_switch_time.get(opp_symbol, 0)
            hours_since_opp = (now - last_switch_opp) / 3600
            if hours_since_opp < self.MIN_SWITCH_INTERVAL_HOURS:
                logger.debug(
                    f"{opp_symbol}: target cooldown active ({hours_since_opp:.1f}h < {self.MIN_SWITCH_INTERVAL_HOURS}h)"
                )
                continue

            # Check blacklist
            opp_24h = opp.get("price_change_24h", 0)
            if opp_24h > self.BLACKLIST_24H_CHANGE:
                logger.debug(
                    f"{opp_symbol}: blacklisted (24h={opp_24h:.1f}% > {self.BLACKLIST_24H_CHANGE}%)"
                )
                continue

            # Check score gap
            opp_score = opp.get("score", 0)
            score_gap = opp_score - existing_score

            if score_gap > best_score_gap:
                best_score_gap = score_gap
                best_alt = opp

        # Decision logic
        should_switch = False
        reason = ""
        to_symbol = None

        # Condition 1: existing position is losing > 5% AND a valid alternative exists
        if existing_24h_change < self.EXISTING_LOSS_THRESHOLD and best_alt:
            should_switch = True
            to_symbol = best_alt["symbol"]
            reason = f"loss {existing_24h_change:.1f}% < {self.EXISTING_LOSS_THRESHOLD}% → {to_symbol} (score gap={best_score_gap:.0f})"

        # Condition 2: new opportunity is significantly better
        if (
            existing_score > 0
            and best_score_gap > self.SCORE_GAP_THRESHOLD
            and best_alt
        ):
            should_switch = True
            to_symbol = best_alt["symbol"]
            reason = f"score gap={best_score_gap:.0f} > {self.SCORE_GAP_THRESHOLD} ({existing_score:.0f}→{best_alt.get('score',0):.0f})"

        # Condition 3: LOW SCORE EXIT — position score below threshold, exit to USDT
        if existing_score > 0 and existing_score < self.LOW_SCORE_EXIT_THRESHOLD:
            should_switch = True
            to_symbol = best_alt["symbol"] if best_alt else None
            if to_symbol and best_alt is not None:
                reason = f"low score {existing_score:.0f} < {self.LOW_SCORE_EXIT_THRESHOLD} → {to_symbol} ({best_alt.get('score',0):.0f})"
            else:
                reason = f"low score {existing_score:.0f} < {self.LOW_SCORE_EXIT_THRESHOLD} → USDT (no alt)"

        if not should_switch:
            return None

        # Calculate expected benefit — FIX-7: score-based instead of 24h momentum
        # Old formula was momentum-chasing: alt_24h - existing_24h_change (past predicts future = wrong)
        # New formula: score gap is the primary signal (higher score = better expected performance)
        alt_score = best_alt.get("score", 0) if best_alt else 0
        score_gap = alt_score - existing_score
        alt_24h = best_alt.get("price_change_24h", 0) if best_alt else 0
        # Score gap drives decision, 24h is minor tiebreaker (capped at ±1%)
        momentum_bonus = max(-1.0, min(1.0, (alt_24h - existing_24h_change) * 0.1))
        expected_gain = (score_gap * 0.15) + momentum_bonus - self.SWITCH_FEE_PCT

        # Minimum gain gate: don't switch for < 0.5% expected gain
        if expected_gain < self.MIN_EXPECTED_GAIN_PCT:
            logger.debug(
                f"Switch rejected: expected gain {expected_gain:.2f}% (score_gap={score_gap:.0f}, momentum={momentum_bonus:.2f}) < {self.MIN_EXPECTED_GAIN_PCT}%"
            )
            return None

        decision = {
            "timestamp": datetime.now().isoformat(),
            "action": "switch",
            "from_symbol": symbol,
            "from_value": position_value,
            "from_24h": existing_24h_change,
            "from_score": existing_score,
            "to_symbol": to_symbol,
            "to_score": best_alt.get("score", 0) if best_alt else 0,
            "to_24h": alt_24h,
            "to_price": best_alt.get("price", 0) if best_alt else 0,
            "score_gap": best_score_gap,
            "fee_pct": self.SWITCH_FEE_PCT,
            "expected_gain_pct": expected_gain,
            "reason": reason,
            "executed": False,
        }

        logger.info(f"Switch decision: {symbol} -> {to_symbol or 'USDT'} ({reason})")
        return decision

    def _execute_switch(self, decision: Dict) -> bool:
        """Execute sell + buy switch, or sell-only if no alternative (exit to USDT)."""
        from_symbol = decision["from_symbol"]
        to_symbol = decision.get("to_symbol")
        from_value = decision["from_value"]

        try:
            # 1. Get quantity to sell from portfolio
            positions = self.portfolio.get_all_positions()
            from_qty = None
            for pos in positions:
                if pos["symbol"] == from_symbol:
                    from_qty = pos.get("quantity", 0)
                    break
            if not from_qty or from_qty <= 0:
                logger.error(f"No quantity found for {from_symbol} in portfolio")
                return False

            # 1b. Validate sell quantity against exchange filters (minQty, minNotional, stepSize)
            sell_filters = self.bc.get_symbol_filters(from_symbol)
            if sell_filters:
                import math

                min_qty = sell_filters.get("minQty", 0)
                min_notional = sell_filters.get("minNotional", 0)
                step = sell_filters.get("stepSize", 1)
                # Floor to stepSize and round to avoid floating-point noise
                decimals = max(0, -int(math.floor(math.log10(step)))) if step > 0 else 8
                from_qty = round(math.floor(from_qty / step) * step, decimals)
                if from_qty < min_qty:
                    logger.error(
                        f"Sell blocked for {from_symbol}: qty={from_qty:.8f} < minQty={min_qty} (dust position)"
                    )
                    return False
                # Check minNotional using current price
                try:
                    current_price = self.bc.get_ticker_price(symbol=from_symbol)
                    if from_qty * current_price < min_notional:
                        logger.error(
                            f"Sell blocked for {from_symbol}: notional=${from_qty * current_price:.2f} < minNotional=${min_notional} (dust position)"
                        )
                        return False
                except Exception as e:
                    logger.warning(f"Cannot verify minNotional for {from_symbol}: {e}")

            # 2. Cancel open orders first (TP/SL lock the quantity)
            try:
                cancel_result = self.bc.cancel_all_orders(from_symbol)
                if cancel_result:
                    logger.info(f"Cancelled open orders for {from_symbol}")
                    time.sleep(0.5)  # Brief wait for orders to settle
            except Exception as e:
                logger.warning(f"Cancel orders failed for {from_symbol}: {e}")

            # 2b. Re-fetch actual free balance after canceling orders
            try:
                bal_info = self.bc.get_account()
                for b in bal_info.get("balances", []):
                    if b["asset"] == from_symbol.replace("USDT", ""):
                        free_qty = float(b["free"])
                        if free_qty > 0:
                            from_qty = min(from_qty, free_qty)
                        break
            except Exception as e:
                logger.warning(f"Cannot re-fetch balance for {from_symbol}: {e}")

            # 3. Sell existing position (market sell by quantity)
            logger.info(f"Selling {from_symbol}: qty={from_qty}")
            sell_order = self.bc.place_market_sell(
                symbol=from_symbol, quantity=from_qty
            )
            if not sell_order:
                logger.error(f"Failed to sell {from_symbol}")
                return False
            logger.info(f"Sell order placed: {sell_order.get('orderId', 'N/A')}")

            # Record switch time immediately after successful sell
            # (prevents cooldown bypass on partial failure)
            self._last_switch_time[from_symbol] = time.time()
            if to_symbol:
                self._last_switch_time[to_symbol] = time.time()
            self._save_switch_times()

            # 3. If no buy target (exit to USDT), skip buy logic
            if not to_symbol:
                logger.info(
                    f"Exit-to-USDT: sold {from_symbol}, no buy target. Funds in USDT."
                )
                try:
                    self.portfolio.close_position(
                        from_symbol, close_price=current_price
                    )
                except Exception as e:
                    logger.warning(f"Portfolio close failed (non-critical): {e}")
                # Cooldown already recorded after sell
                decision["executed"] = True
                decision["sell_order_id"] = sell_order.get("orderId")
                try:
                    from src.state_db import get_state_db

                    db = get_state_db()
                    db.audit_log(
                        "EXIT_TO_USDT",
                        {
                            "from_symbol": from_symbol,
                            "reason": decision.get("reason", ""),
                            "sell_order_id": sell_order.get("orderId"),
                            "value": from_value,
                        },
                    )
                except Exception:
                    logger.error(
                        "Failed to log EXIT_TO_USDT audit for %s",
                        from_symbol,
                        exc_info=True,
                    )
                return True

            # 4. Calculate buy quantity using actual USDT balance (not stale from_value)
            try:
                usdt_balance = float(self.bc.get_free_balance("USDT"))
                # Sanity-check: if balance is less than 1% of from_value,
                # treat as mock/unreliable and fall back to from_value
                if usdt_balance < from_value * 0.01:
                    logger.warning(
                        f"USDT balance {usdt_balance:.2f} suspiciously low vs from_value={from_value:.2f}; using from_value"
                    )
                    buy_value = from_value * (1 - self.SWITCH_FEE_PCT / 100)
                else:
                    # Use the lesser of sell proceeds and available balance
                    buy_value = min(from_value, usdt_balance) * (
                        1 - self.SWITCH_FEE_PCT / 100
                    )
            except Exception:
                logger.error(
                    "Failed to fetch USDT balance — using estimated from_value=%.2f",
                    from_value,
                    exc_info=True,
                )
                buy_value = from_value * (1 - self.SWITCH_FEE_PCT / 100)
            if buy_value <= 0:
                logger.error(f"No USDT available after selling {from_symbol}")
                return False
            to_price = decision.get("to_price", 0)
            if to_price <= 0:
                # Fallback: fetch current price
                try:
                    to_price = self.bc.get_ticker_price(symbol=to_symbol)
                except Exception as e:
                    logger.error(f"Cannot get price for {to_symbol}: {e}")
                    return False
            buy_qty = buy_value / to_price

            # 4. Validate against symbol filters (minQty, minNotional)
            filters = self.bc.get_symbol_filters(to_symbol)
            if filters:
                min_qty = filters.get("minQty", 0)
                min_notional = filters.get("minNotional", 0)
                if buy_qty < min_qty:
                    logger.error(
                        f"Buy quantity too small for {to_symbol}: "
                        f"qty={buy_qty:.8f} < minQty={min_qty}"
                    )
                    return False
                if buy_value < min_notional:
                    logger.error(
                        f"Buy value too small for {to_symbol}: "
                        f"value=${buy_value:.2f} < minNotional=${min_notional:.2f}"
                    )
                    return False

            # 5. Floor buy quantity to stepSize before validation
            filters = self.bc.get_symbol_filters(to_symbol)
            if filters and "stepSize" in filters:
                import math

                step = filters["stepSize"]
                # Round to step precision to avoid floating-point noise (e.g. 1.6580000000000001)
                decimals = max(0, -int(math.floor(math.log10(step))))
                buy_qty = round(math.floor(buy_qty / step) * step, decimals)
                logger.info(
                    f"Floored buy qty for {to_symbol}: {buy_qty:.8f} (stepSize={step})"
                )

            # 6. Re-validate after flooring
            if filters:
                min_qty = filters.get("minQty", 0)
                min_notional = filters.get("minNotional", 0)
                if buy_qty < min_qty:
                    logger.error(
                        f"Buy quantity too small for {to_symbol} after flooring: "
                        f"qty={buy_qty:.8f} < minQty={min_qty}"
                    )
                    return False
                if buy_value < min_notional:
                    logger.error(
                        f"Buy value too small for {to_symbol}: "
                        f"value=${buy_value:.2f} < minNotional=${min_notional:.2f}"
                    )
                    return False

            # 7. Buy new position (market buy by quantity)
            logger.info(
                f"Buying {to_symbol}: qty={buy_qty:.6f} @ ${to_price:.4f} (value=${buy_value:.2f})"
            )
            buy_order = self.bc.place_market_buy(symbol=to_symbol, quantity=buy_qty)
            if not buy_order:
                # CRITICAL: sell succeeded but buy failed — funds now in USDT, idle
                logger.critical(
                    f"SWITCH HALF-FAILED: Sold {from_symbol} but failed to buy {to_symbol}. "
                    f"Funds (~${buy_value:.2f} USDT) are now IDLE. Manual intervention required."
                )
                # Persist alert to state_db
                try:
                    from src.state_db import get_state_db

                    db = get_state_db()
                    db.audit_log(
                        "SWITCH_HALF_FAILED",
                        {
                            "from_symbol": from_symbol,
                            "to_symbol": to_symbol,
                            "sell_order_id": sell_order.get("orderId"),
                            "buy_value": buy_value,
                            "status": "FUNDS_IDLE",
                            "alert": "Manual buy required",
                        },
                    )
                except Exception:
                    logger.error(
                        "Failed to log switch audit for %s to %s",
                        from_symbol,
                        to_symbol,
                        exc_info=True,
                    )
                return False
            logger.info(f"Buy order placed: {buy_order.get('orderId', 'N/A')}")

            # 8. Update portfolio state: remove old, add new
            try:
                # Get from_price for close_position
                from_price = decision.get("from_price", 0)
                if from_price <= 0:
                    # Fallback: fetch current price
                    try:
                        stats = self.bc.get_24hr_stats(from_symbol)
                        from_price = float(stats.get("last_price", 0))
                    except Exception:
                        logger.warning(
                            "get_24hr_stats failed for %s, close_position will use cached price",
                            from_symbol,
                            exc_info=True,
                        )
                        from_price = None  # close_position will use cached price
                # Close old position (credits proceeds to cash)
                self.portfolio.close_position(from_symbol, close_price=from_price)
                # Add new position (deducts cost from cash)
                self.portfolio.add_position(
                    symbol=to_symbol,
                    quantity=buy_qty,
                    entry_price=to_price,
                    strategy="switch",
                    deduct_cash=True,
                )
                logger.info(
                    f"Portfolio updated: removed {from_symbol}, added {to_symbol}"
                )
            except Exception as portfolio_err:
                logger.error(f"Portfolio state update failed: {portfolio_err}")
                # Non-critical: next sync_from_binance will correct it

            # 9. Cooldown already recorded after sell; no need to duplicate
            # (cooldowns were persisted at sell-success time above)

            # 10. Persist to state_db audit log
            try:
                from src.state_db import get_state_db

                db = get_state_db()
                db.audit_log(
                    "SWITCH_EXECUTED",
                    {
                        "from_symbol": from_symbol,
                        "to_symbol": to_symbol,
                        "from_value": from_value,
                        "buy_value": buy_value,
                        "sell_order_id": sell_order.get("orderId"),
                        "buy_order_id": buy_order.get("orderId"),
                    },
                )
            except Exception as db_err:
                logger.warning(f"State DB persistence failed (non-critical): {db_err}")

            decision["executed"] = True
            decision["sell_order_id"] = sell_order.get("orderId")
            decision["buy_order_id"] = buy_order.get("orderId")
            logger.info(f"Switch executed: {from_symbol} -> {to_symbol}")

            # FIX 2026-08-18: create a trade_outcomes entry for switch buys.
            # execute_phases records entries for scan-driven buys, but switch
            # buys had no entry → later exits logged "No open entry" and the
            # self-learning loop missed switch trades entirely (e.g. ALLO 08-18).
            try:
                from src.trade_outcome_recorder import TradeOutcomeRecorder

                TradeOutcomeRecorder().record_entry(
                    symbol=to_symbol,
                    entry_price=float(to_price),
                    qty=float(buy_qty),
                    score=float(decision.get("to_score", 0) or 0),
                    strategy="switch",
                )
                logger.info(f"Outcome entry recorded for switch buy: {to_symbol}")
            except Exception as entry_err:
                logger.warning(
                    "Failed to record outcome entry for switch buy %s: %s",
                    to_symbol,
                    entry_err,
                )
            return True

        except Exception as e:
            logger.error(f"Switch execution failed: {e}")
            return False

    def get_status(self) -> Dict:
        """Return optimizer status."""
        return {
            "last_switch_times": {
                k: datetime.fromtimestamp(v).isoformat()
                for k, v in self._last_switch_time.items()
            },
            "thresholds": {
                "existing_loss_threshold": self.EXISTING_LOSS_THRESHOLD,
                "score_gap_threshold": self.SCORE_GAP_THRESHOLD,
                "low_score_exit_threshold": self.LOW_SCORE_EXIT_THRESHOLD,
                "blacklist_24h_change": self.BLACKLIST_24H_CHANGE,
                "switch_fee_pct": self.SWITCH_FEE_PCT,
                "min_switch_interval_hours": self.MIN_SWITCH_INTERVAL_HOURS,
            },
        }
