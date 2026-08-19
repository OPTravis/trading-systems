"""
Portfolio state persistence — mixin for PortfolioManager.
Handles SQLite save/load and Binance API sync.
"""

import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient

logger = logging.getLogger(__name__)


class StateMixin:
    """State persistence methods for PortfolioManager."""

    DUST_THRESHOLD_USD: float
    _db: Any
    config: Dict[str, Any]
    positions: Dict[str, Any]
    cash_balance: float
    _last_save_time: float
    _save_debounce_sec: float

    def add_position(self, *args: Any, **kwargs: Any) -> Any: ...

    def _save_state(self, force=False):
        """Save state to SQLite (sole source of truth).

        Args:
            force: If True, bypass debounce and save immediately.
                   Used for critical operations (sync, close_position)
                   where memory-DB inconsistency is unacceptable.
        """
        now = time.monotonic()
        if not force and now - self._last_save_time < self._save_debounce_sec:
            return
        self._last_save_time = now

        if self._db is None:
            logger.warning("StateDB not available, skipping state save")
            return

        try:
            with self._db.transaction():
                # Save cash_balance FIRST (foreign-key-like dependency for audit)
                self._db.portfolio_set_cash_balance(self.cash_balance)
                # Save all positions
                for sym, pos in self.positions.items():
                    self._db.portfolio_set(
                        sym,
                        {
                            "quantity": pos["quantity"],
                            "entry_price": pos["entry_price"],
                            "strategy": pos.get("strategy", ""),
                            "opened_at": pos.get(
                                "created_at", datetime.now().isoformat()
                            ),
                            "stop_loss": pos.get("stop_loss"),
                            "take_profit": pos.get("take_profit"),
                        },
                    )
                # Remove closed positions from DB
                db_positions = self._db.portfolio_get_all()
                for sym in db_positions:
                    if sym not in self.positions:
                        self._db.portfolio_remove(sym)
        except Exception as e:
            logger.error(f"Failed to save portfolio to StateDB: {e}")
            raise

    def _load_state_from_db(self):
        """Load state from SQLite (sole source of truth)."""
        if self._db is None:
            raise RuntimeError("StateDB not available — cannot load portfolio state")

        try:
            db_positions = self._db.portfolio_get_all()
            if db_positions:
                self.positions = {}
                MIN_SL_PCT = 3.0  # Safety: minimum stop loss percentage
                for sym, data in db_positions.items():
                    entry_price = data["entry_price"]
                    db_sl = data.get("stop_loss")
                    db_tp = data.get("take_profit")
                    default_sl = entry_price * (
                        1 - self.config["stop_loss"]["default_pct"] / 100
                    )
                    default_tp = entry_price * (
                        1
                        + self.config.get("take_profit", {}).get("default_pct", 6.0)
                        / 100
                    )
                    # Safety: Ensure stop_loss is never 0 or invalid
                    min_sl_price = entry_price * (1 - MIN_SL_PCT / 100)
                    effective_sl = db_sl if db_sl and db_sl > 0 else default_sl
                    if effective_sl <= 0 or effective_sl >= entry_price:
                        effective_sl = min_sl_price
                        logger.warning(
                            f"Invalid stop_loss for {sym} (db_sl={db_sl}), "
                            f"enforcing minimum {MIN_SL_PCT}%: ${effective_sl:.4f}"
                        )
                    self.positions[sym] = {
                        "symbol": sym,
                        "quantity": data["quantity"],
                        "entry_price": entry_price,
                        "current_price": entry_price,
                        "strategy": data.get("strategy", ""),
                        "created_at": data.get("opened_at", datetime.now().isoformat()),
                        "updated_at": datetime.now().isoformat(),
                        "stop_loss": effective_sl,
                        "take_profit": db_tp if db_tp is not None else default_tp,
                        "trailing_stop_pct": 1.5,
                        "highest_price": entry_price,
                    }
                logger.info(f"Loaded {len(self.positions)} positions from StateDB")
            # Load cash_balance from SQLite kv store
            db_cash = self._db.portfolio_get_cash_balance()
            self.cash_balance = db_cash
            logger.info(f"Loaded cash_balance={db_cash} from StateDB")
        except Exception as e:
            logger.error(f"Failed to load from StateDB: {e}")
            raise

    def sync_from_binance(self, binance_client: "ExchangeClient"):
        """Sync portfolio state from Binance API — THE SOURCE OF TRUTH.

        This method should be called on system startup to ensure local state
        matches the actual exchange state. It:
        1. Fetches all non-dust balances from Binance
        2. Clears local positions
        3. Rebuilds positions from API data (without deducting cash)
        4. Sets cash_balance to USDT balance

        CRITICAL: This method is atomic — if any step fails, the original
        state is restored to prevent partial sync corruption.
        """
        if binance_client is None:
            logger.error("No Binance client provided for sync")
            return False

        logger.info(
            "\U0001f504 Syncing portfolio from Binance API (source of truth)..."
        )

        try:
            account = binance_client.get_account()
        except Exception as e:
            logger.error(f"Failed to fetch Binance account: {e}")
            return False

        # Safety: if Binance returns empty/zero balances, this is likely a transient error
        # (not a legitimate zero-balance account). Skip sync to prevent destroying local state.
        total_assets = sum(
            float(b.get("free", 0)) + float(b.get("locked", 0))
            for b in account.get("balances", [])
        )
        if total_assets <= 0 and len(self.positions) > 0:
            logger.warning(
                "Binance returned zero total assets but local state has %d positions — "
                "skipping sync (likely transient API error)",
                len(self.positions),
            )
            return False

        # Save original state for rollback on failure
        old_positions = dict(self.positions)
        old_cash = self.cash_balance

        # P1-5: Build-then-swap pattern
        # Build new state into a temporary dict; only swap self.positions
        # after all positions are successfully built. This prevents partial
        # state exposure and corruption on mid-build failures.
        _new_positions: Dict[str, Any] = {}
        # Temporarily redirect self.positions so add_position() works during build
        self.positions = _new_positions

        usdt_balance = 0.0
        new_positions = []

        STABLECOINS = {
            "USDT",
            "USDC",
            "BUSD",
            "DAI",
            "TUSD",
            "FDUSD",
            "USDP",
            "EUR",
            "RLUSD",
            "EURT",
            "AEUR",
            "GBP",
            "NTRN",
        }

        try:
            for balance in account.get("balances", []):
                asset = balance["asset"]
                free = float(balance.get("free", 0))
                locked = float(balance.get("locked", 0))
                total = free + locked

                if asset in STABLECOINS:
                    if asset == "USDT":
                        usdt_balance = total
                    continue

                if total <= 0:
                    continue

                # Get current price to check dust threshold
                try:
                    stats = binance_client.get_24hr_stats(f"{asset}USDT")
                    price = float(stats.get("last_price", 0))
                    # Fallback if get_24hr_stats returned empty (transient error)
                    if price <= 0:
                        price = float(
                            binance_client.get_ticker_price(f"{asset}USDT") or 0
                        )
                    if price <= 0:
                        logger.warning(f"Invalid price for {asset}, skipping")
                        continue
                    value = total * price

                    if value < self.DUST_THRESHOLD_USD:
                        logger.info(
                            f"Skipping dust position: {asset} {total} @ {price} = ${value:.4f}"
                        )
                        continue

                    existing_entry = None
                    try:
                        db_pos = self._db.portfolio_get(f"{asset}USDT")
                        if db_pos and db_pos.get("entry_price"):
                            existing_entry = float(db_pos["entry_price"])
                            entry_source = "db_existing"
                    except Exception:
                        logger.error(
                            "Failed to read existing entry price from DB for %s",
                            asset,
                            exc_info=True,
                        )

                    # ── bug #6 fix: cost-basis resolution, market price is a
                    # last-resort TEMPORARY placeholder, never a locked-in entry.
                    # A previous market_estimate could have polluted the stored
                    # entry (ETH: 2099.84 vs real 1935.41) and then locked itself
                    # in via the db_existing path. Estimate-flagged entries are
                    # retried on every sync until a real cost basis is found.
                    _sym_key = f"{asset}USDT"
                    _est_key = f"entry_est:{_sym_key}"
                    is_estimate = False
                    try:
                        is_estimate = bool(self._db.kv_get(_est_key))
                    except Exception:
                        pass

                    if existing_entry and existing_entry > 0 and not is_estimate:
                        entry_price = existing_entry
                    else:
                        entry_price = None
                        # 1) exchange trade history (weighted avg of open lots)
                        try:
                            from src.entry_price import get_avg_entry_price

                            avg_entry = get_avg_entry_price(
                                binance_client, _sym_key, total
                            )
                            if avg_entry and avg_entry > 0:
                                entry_price = avg_entry
                                entry_source = "trade_history"
                        except Exception as e:
                            logger.warning(
                                f"entry_price via exchange history failed for {asset}: {e}"
                            )
                        # 2) local DB ledger fallback (bug #6: exchange history
                        #    can be incomplete — e.g. ETH qty 0.0269 vs 0.0027 —
                        #    while our own trades table holds the real fills)
                        if entry_price is None:
                            try:
                                from src.entry_price import (
                                    get_avg_entry_price_from_db,
                                )

                                db_entry = get_avg_entry_price_from_db(
                                    self._db, _sym_key, total
                                )
                                if db_entry and db_entry > 0:
                                    entry_price = db_entry
                                    entry_source = "db_ledger"
                            except Exception as e:
                                logger.warning(
                                    f"entry_price via DB ledger failed for {asset}: {e}"
                                )
                        # 3) last resort: market price, flagged for retry
                        if entry_price is None:
                            entry_price = price
                            entry_source = "market_estimate"
                            logger.warning(
                                f"Could not determine cost basis for {asset}; using market price "
                                f"${price:.4f} as TEMPORARY entry (flagged for retry on next sync)"
                            )
                            try:
                                self._db.kv_set(_est_key, time.time())
                            except Exception:
                                pass

                        # A real cost basis was found: clear the estimate flag
                        if entry_source != "market_estimate":
                            try:
                                self._db.kv_remove(_est_key)
                            except Exception:
                                pass
                            if is_estimate:
                                logger.info(
                                    f"{_sym_key}: replaced estimated entry with real "
                                    f"cost basis ${entry_price:.4f} ({entry_source})"
                                )

                    # Check if position already exists in DB (avoid phantom BUY records)
                    sym_key = f"{asset}USDT"
                    already_in_db = False
                    try:
                        existing_db = (
                            self._db.portfolio_get(sym_key) if self._db else None
                        )
                        if existing_db:
                            already_in_db = True
                    except Exception:
                        logger.error(
                            "Failed to check existing DB position for %s",
                            asset,
                            exc_info=True,
                        )

                    if already_in_db:
                        # Existing position: update qty and price, preserve original SL/TP
                        db_sl = existing_db.get("stop_loss") if existing_db else None
                        db_tp = existing_db.get("take_profit") if existing_db else None
                        default_sl = entry_price * (
                            1 - self.config["stop_loss"]["default_pct"] / 100
                        )
                        default_tp = entry_price * (
                            1
                            + self.config.get("take_profit", {}).get("default_pct", 6.0)
                            / 100
                        )
                        # Safety: Ensure stop_loss is never 0 or None (minimum 3%)
                        MIN_SL_PCT = 3.0
                        min_sl_price = entry_price * (1 - MIN_SL_PCT / 100)
                        effective_sl = db_sl if db_sl and db_sl > 0 else default_sl
                        if effective_sl <= 0 or effective_sl >= entry_price:
                            effective_sl = min_sl_price
                            logger.warning(
                                f"Invalid stop_loss for {sym_key} (db_sl={db_sl}), "
                                f"enforcing minimum {MIN_SL_PCT}%: ${effective_sl:.4f}"
                            )
                        self.positions[sym_key] = {
                            "symbol": sym_key,
                            "quantity": total,
                            "entry_price": entry_price,
                            "current_price": price,
                            "strategy": "synced",
                            "created_at": datetime.now().isoformat(),
                            "updated_at": datetime.now().isoformat(),
                            "stop_loss": effective_sl,
                            "take_profit": db_tp if db_tp else default_tp,
                            "trailing_stop_pct": 1.5,
                            "highest_price": entry_price,
                        }
                    else:
                        # New position: use add_position with _from_sync to skip phantom BUY trade
                        self.add_position(
                            symbol=sym_key,
                            quantity=total,
                            entry_price=entry_price,
                            strategy="synced",
                            deduct_cash=False,
                            _skip_validation=True,
                            _from_sync=True,
                        )
                    if f"{asset}USDT" in self.positions:
                        self.positions[f"{asset}USDT"][
                            "entry_price_source"
                        ] = entry_source
                    new_positions.append(
                        f"{asset}: {total} @ ${entry_price:.4f} ({entry_source}) = ${value:.2f}"
                    )

                except Exception as e:
                    logger.warning(f"Could not price {asset}: {e}")
                    continue

            # Set cash balance
            self.cash_balance = usdt_balance
            self._last_save_time = 0
            self._save_state(force=True)

        except Exception as e:
            logger.error(
                f"Sync failed mid-process: {e}. Rolling back to previous state."
            )
            self.positions = old_positions
            self.cash_balance = old_cash
            self._last_save_time = 0
            self._save_state(force=True)
            return False

        # Audit log only on success
        if self._db is not None:
            try:
                self._db.audit_log(
                    action="PORTFOLIO_SYNC",
                    details=f"Synced {len(self.positions)} positions from Binance",
                    old_value=json.dumps(
                        {"positions": list(old_positions.keys()), "cash": old_cash}
                    ),
                    new_value=json.dumps(
                        {"positions": list(self.positions.keys()), "cash": usdt_balance}
                    ),
                    source="binance_api",
                )
            except Exception as e:
                logger.warning(f"Failed to write audit log: {e}")

        logger.info("\u2705 Portfolio synced from Binance:")
        logger.info(f"   USDT: ${usdt_balance:.2f}")
        for pos in new_positions:
            logger.info(f"   {pos}")
        logger.info(
            f"   Total: ${usdt_balance + sum(p['quantity'] * p['entry_price'] for p in self.positions.values()):.2f}"
        )

        if old_positions:
            logger.info(f"   Replaced {len(old_positions)} old positions")

        return True
