"""
Portfolio Manager - Track and manage positions

Facade module: PortfolioManager inherits from specialized mixins.
Each mixin handles a specific concern (PnL, risk, state persistence).

SOLE SOURCE OF TRUTH: SQLite state.db (via StateDB)
No JSON backup — Binance API is the external source of truth for recovery.
"""

import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from datetime import datetime
from pathlib import Path

from src.portfolio_pnl import PnlMixin
from src.portfolio_risk import RiskMixin
from src.portfolio_state import StateMixin

logger = logging.getLogger(__name__)


class PortfolioManager(PnlMixin, RiskMixin, StateMixin):
    """Manage portfolio positions and risk.

    Composed from:
    - PnlMixin: calculate_pnl, get_total_exposure, get_total_pnl
    - RiskMixin: check_risk_limits, suggest_rebalance, validate_leverage
    - StateMixin: _save_state, _load_state_from_db, sync_from_binance
    """

    # Dust threshold: positions worth less than this are ignored
    DUST_THRESHOLD_USD = 1.0

    def __init__(
        self,
        config_path: Optional[str] = None,
        binance_client: Optional["ExchangeClient"] = None,
    ):
        # Load risk config — auto-detect risk_limits.yaml if no path given
        if config_path is None:
            _default_path = Path(__file__).parent.parent / "config" / "risk_limits.yaml"
            if _default_path.exists():
                config_path = str(_default_path)
                logger.info("Auto-loaded risk config from %s", config_path)
        if config_path and Path(config_path).exists():
            with open(config_path) as f:
                config_data = yaml.safe_load(f)
            if not isinstance(config_data, dict) or "risk" not in config_data:
                logger.warning(
                    f"Invalid config format in {config_path}, using defaults"
                )
                self.config = self._default_config()
            else:
                self.config = self._validate_config(config_data.get("risk", {}))
        else:
            self.config = self._default_config()

        self.positions = {}  # symbol -> position data (in-memory cache)
        self.cash_balance = 0
        self.orders_log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()  # Thread safety for positions and cash_balance

        # Optional BinanceClient for real-time price fetch
        self._client = binance_client

        # Debounce state saves (min interval in seconds)
        self._last_save_time = 0
        self._save_debounce_sec = 2

        # Daily loss tracking (used by RiskMixin._check_daily_reset)
        self._daily_start_value = None
        self._daily_start_date = None

        # Initialize StateDB (primary storage)
        self._db: Optional[Any] = None
        try:
            from src.state_db import get_state_db

            self._db = get_state_db()
        except Exception as e:
            logger.error(f"Failed to initialize StateDB: {e}")
            self._db = None

        # Load state from DB if available (and optionally sync from Binance)
        if self._db is not None:
            try:
                self._load_state_from_db()
            except Exception as e:
                logger.warning(f"Failed to load state from DB: {e}")

            if binance_client is not None:
                try:
                    synced = self.sync_from_binance(binance_client)
                    if synced:
                        logger.info("Portfolio synced from Binance on startup")
                except Exception as e:
                    logger.warning(f"Failed to sync from Binance on startup: {e}")

    def _validate_config(self, config: Dict) -> Dict:
        """Validate and set defaults for risk config"""
        defaults = self._default_config()
        # Merge with defaults (user config takes precedence)
        for key in defaults:
            if key not in config:
                config[key] = defaults[key]
            elif isinstance(defaults[key], dict) and isinstance(config.get(key), dict):
                for sub_key in defaults[key]:
                    if sub_key not in config[key]:
                        config[key][sub_key] = defaults[key][sub_key]
            elif isinstance(defaults[key], dict) and not isinstance(
                config.get(key), dict
            ):
                # Config value is wrong type (e.g. null), reset to default
                logger.warning(
                    f"Config key '{key}' expected dict, got {type(config.get(key)).__name__} — using default"
                )
                config[key] = defaults[key]
        return config

    def _default_config(self) -> Dict:
        """Default risk configuration"""
        return {
            "max_position_pct": 30,
            "max_total_exposure_pct": 80,
            "max_daily_loss_pct": 5,
            "max_leverage": 1,
            "max_open_positions": 3,  # Aligned with risk_limits.yaml (2026-08-17)
            "stop_loss": {"default_pct": 5},
            "take_profit": {"default_pct": 6},
            "trailing_stop": {"activation_pct": 2, "distance_pct": 1},
            "max_hold_hours": 168,  # 7 days
        }

    def update_balance(self, balance: float):
        """Update cash balance (debounced save unless critical)."""
        self.cash_balance = balance
        self._save_state(
            force=False
        )  # debounce — critical ops (add/close/sync) use force=True

    def add_position(
        self,
        symbol: str,
        quantity: float,
        entry_price: float,
        strategy: str = "unknown",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        deduct_cash: bool = True,
        _dry_run: bool = False,
        _skip_validation: bool = False,
        _from_sync: bool = False,
    ):
        """Add or merge a position with validation.

        Args:
            symbol: Trading pair symbol.
            quantity: Amount to add.
            entry_price: Price per unit.
            strategy: Strategy label.
            stop_loss: Optional stop-loss price (default: from config)
            take_profit: Optional take-profit price (default: from config)
            deduct_cash: If True, deduct qty*entry_price from cash_balance.
            _dry_run: If True, only run validation without modifying state.
            _skip_validation: If True, skip size/risk validation (used by sync_from_binance).
        """
        # Dust filter: skip positions worth less than threshold
        position_value = quantity * entry_price
        if position_value < self.DUST_THRESHOLD_USD:
            logger.info(
                f"Dust position ignored: {symbol} {quantity} @ {entry_price} = ${position_value:.4f}"
            )
            return

        # Check cash balance before opening position
        if deduct_cash and self.cash_balance < position_value:
            raise ValueError(
                f"Insufficient cash: ${self.cash_balance:.2f} < ${position_value:.2f} needed for {symbol}. "
                f"Cannot open position."
            )

        if not _skip_validation:
            # Validate position size against max_position_pct
            total_value = self.cash_balance + self.get_total_exposure()
            position_pct = (
                (position_value / total_value * 100) if total_value > 0 else 0
            )

            if position_pct > self.config.get("max_position_pct", 100):
                raise ValueError(
                    f"Position size {position_pct:.1f}% exceeds max {self.config['max_position_pct']}%. "
                    f"Reduce quantity or increase capital."
                )

            # Validate max open positions (exclude dust <$5)
            max_positions = self.config.get("max_open_positions")
            if max_positions is not None and symbol not in self.positions:
                non_dust = sum(
                    1
                    for p in self.positions.values()
                    if (p.get("current_price", 0) or p.get("entry_price", 0))
                    * p.get("quantity", 0)
                    >= 5.0
                )
                if non_dust >= max_positions:
                    raise ValueError(
                        f"Cannot open new position: {non_dust} non-dust positions already open "
                        f"(max {max_positions})."
                    )

        # Also enforce max positions even when _skip_validation=True (safety guard)
        max_positions = self.config.get("max_open_positions")
        if max_positions is not None and symbol not in self.positions:
            non_dust = sum(
                1
                for p in self.positions.values()
                if (p.get("current_price", 0) or p.get("entry_price", 0))
                * p.get("quantity", 0)
                >= 5.0
            )
            if non_dust >= max_positions:
                raise ValueError(
                    f"Cannot open new position: {non_dust} non-dust positions already open "
                    f"(max {max_positions})."
                )

        if _dry_run:
            return  # Validation passed, caller can proceed with order

        now = datetime.now().isoformat()

        with self._lock:
            if symbol in self.positions:
                # Merge: weighted average entry price
                old = self.positions[symbol]
                old_qty = old["quantity"]
                old_entry = old["entry_price"]
                new_qty = old_qty + quantity
                new_entry = (old_qty * old_entry + quantity * entry_price) / new_qty
                self.positions[symbol] = {
                    "symbol": symbol,
                    "quantity": new_qty,
                    "entry_price": new_entry,
                    "current_price": old.get("current_price", entry_price),
                    "strategy": strategy,
                    "stop_loss": (
                        stop_loss
                        if stop_loss is not None
                        else old.get(
                            "stop_loss",
                            new_entry * (1 - self.config["stop_loss"]["default_pct"] / 100),
                        )
                    ),
                    "take_profit": (
                        take_profit
                        if take_profit is not None
                        else old.get(
                            "take_profit",
                            new_entry
                            * (
                                1
                                + self.config.get("take_profit", {}).get("default_pct", 6.0)
                                / 100
                            ),
                        )
                    ),
                    "trailing_stop_pct": old.get("trailing_stop_pct", 1.5),
                    "highest_price": max(
                        old.get("highest_price", entry_price), entry_price
                    ),
                    "created_at": old.get("created_at", now),
                    "updated_at": now,
                }
                logger.info(f"Merged position: {symbol} -> {new_qty} @ {new_entry:.6f}")
            else:
                sl = (
                    stop_loss
                    if stop_loss is not None
                    else entry_price * (1 - self.config["stop_loss"]["default_pct"] / 100)
                )
                tp = (
                    take_profit
                    if take_profit is not None
                    else entry_price
                    * (1 + self.config.get("take_profit", {}).get("default_pct", 6.0) / 100)
                )
                self.positions[symbol] = {
                    "symbol": symbol,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "current_price": entry_price,
                    "strategy": strategy,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "trailing_stop_pct": 1.5,
                    "highest_price": entry_price,
                    "created_at": now,
                    "updated_at": now,
                }
                logger.info(f"Added position: {symbol} {quantity} @ {entry_price}")

            if deduct_cash:
                self.cash_balance -= quantity * entry_price

        # Record BUY trade to StateDB (skip during sync to avoid phantom records)
        if self._db is not None and not _from_sync:
            try:
                self._db.trade_add(
                    symbol=symbol,
                    side="BUY",
                    qty=quantity,
                    price=entry_price,
                )
                logger.info(
                    f"Recorded trade: {symbol} BUY qty={quantity} price={entry_price}"
                )
            except Exception as e:
                logger.error(f"Failed to record trade to StateDB: {e}")

        # Force save on add_position to prevent debounce inconsistency
        self._save_state(force=True)

    def update_position_price(self, symbol: str, current_price: float):
        """Update current price for a position (for PnL tracking)"""
        if symbol in self.positions:
            self.positions[symbol]["current_price"] = current_price
            self.positions[symbol]["updated_at"] = datetime.now().isoformat()
            # Update highest price for trailing stop
            if current_price > self.positions[symbol].get("highest_price", 0):
                self.positions[symbol]["highest_price"] = current_price

    def close_position(
        self,
        symbol: str,
        close_price: Optional[float] = None,
        exit_reason: Optional[str] = None,
    ) -> Dict:
        """Close a position, credit PnL to cash, record trade, and return details.

        Args:
            symbol: Position symbol to close.
            close_price: Override price for closing. If None, uses pos["current_price"].
            exit_reason: P0-A2 (2026-08-26) explicit close reason; overrides the
                position-stored value so switch/exit-to-USDT no longer land as "manual".
        """
        with self._lock:
            if symbol not in self.positions:
                return {"success": False, "error": f"No position for {symbol}"}
            pos = self.positions.pop(symbol)
            # bug#8: mark as closed BY THIS MANAGER so _save_state's delete
            # step removes it even if its DB updated_at is newer than our
            # snapshot (we wrote that update ourselves before closing).
            self._closed_symbols = getattr(self, "_closed_symbols", set()) | {symbol}

        pos["closed_at"] = datetime.now().isoformat()

        price = (
            close_price
            if close_price is not None
            else pos.get("current_price", pos["entry_price"])
        )
        pos["close_price"] = price

        # Credit sale proceeds to cash balance
        pnl = (price - pos["entry_price"]) * pos["quantity"]
        with self._lock:
            self.cash_balance += pos["quantity"] * price
        pos["pnl"] = pnl
        pos["realized"] = True

        # Record trade to StateDB
        if self._db is not None:
            try:
                self._db.trade_add(
                    symbol=symbol,
                    side="SELL",
                    qty=pos["quantity"],
                    price=price,
                    pnl=pnl,
                )
                logger.info(
                    f"Recorded trade: {symbol} SELL qty={pos['quantity']} price={price:.6f} PnL={pnl:.2f}"
                )
            except Exception as e:
                logger.error(f"Failed to record trade to StateDB: {e}")

            # Record outcome using stored entry_rowid for precise matching
            try:
                from src.trade_outcome_recorder import TradeOutcomeRecorder

                recorder = TradeOutcomeRecorder(db=self._db)
                entry_rowid = pos.get("entry_rowid")
                recorder.record_outcome(
                    symbol=symbol,
                    exit_price=price,
                    exit_reason=exit_reason or pos.get("exit_reason") or "manual",
                    entry_id=entry_rowid,
                )
            except Exception as e:
                logger.debug(f"Trade outcome recording failed: {e}")

            # Snapshot features for LightGBM training (closes the training-serving loop)
            try:
                from src.feature_store import get_store
                import time as _t
                fs = get_store()
                label = 1 if pnl >= 0 else 0
                entry_ts = pos.get("opened_at_ts", _t.time())
                fs.snapshot_for_training(symbol, label=label, timestamp=entry_ts)
            except Exception as e:
                logger.warning("portfolio.close_position: " + str(e))
                pass  # non-critical

        self._save_state(force=True)
        logger.info(f"Closed position: {symbol}, PnL: {pnl:.2f}")

        # Publish event to event bus + update contextual bandit (Phase 9)
        try:
            from src.event_bus import get_event_bus

            bus = get_event_bus()
            bus.publish(
                "trade_executed",
                {
                    "symbol": symbol,
                    "action": "SELL",
                    "qty": pos["quantity"],
                    "price": price,
                    "pnl": pnl,
                    "pnl_pct": (
                        ((price - pos["entry_price"]) / pos["entry_price"] * 100)
                        if pos["entry_price"] > 0
                        else 0
                    ),
                },
            )
            bus.publish(
                "position_closed",
                {
                    "symbol": symbol,
                    "entry_price": pos["entry_price"],
                    "close_price": price,
                    "pnl": pnl,
                    "strategy": pos.get("strategy", "unknown"),
                },
            )
        except Exception as e:
            logger.debug(f"Event bus publish failed: {e}")

        # Update contextual bandit with trade outcome
        try:
            from src.contextual_bandit import get_contextual_bandit

            bandit = get_contextual_bandit()
            pnl_pct = (
                ((price - pos["entry_price"]) / pos["entry_price"]) * 100
                if pos["entry_price"] > 0
                else 0
            )
            # Reconstruct context from stored data or use defaults
            ctx = {
                "hmm_regime": "sideways",
                "fear_greed": 50,
                "btc_trend": "NEUTRAL",
                "portfolio_heat": "warm",
            }
            stored_ctx = pos.get("bandit_context")
            if stored_ctx:
                ctx = stored_ctx
            # Use actual invest_pct from the trade (stored as fraction, e.g. 0.15)
            raw_invest = pos.get("invest_pct", 0.8)
            action = raw_invest / 100.0 if raw_invest > 1.0 else raw_invest
            bandit.update_from_outcome(ctx, action_taken=action, pnl_pct=pnl_pct)
        except Exception as e:
            logger.debug(f"Bandit update failed: {e}")

        return pos

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get a single position by symbol"""
        return self.positions.get(symbol)

    def get_trade_history(
        self, symbol: Optional[str] = None, limit: int = 50
    ) -> List[Dict]:
        """Get trade history from StateDB trade_outcomes (has real PnL data)."""
        if self._db is not None:
            try:
                conn = self._db._get_conn()
                if symbol:
                    rows = conn.execute(
                        """SELECT symbol, entry_price, exit_price, net_pnl_pct, strategy, exit_reason, status
                           FROM trade_outcomes WHERE symbol = ?
                           ORDER BY entry_time DESC LIMIT ?""",
                        (symbol, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT symbol, entry_price, exit_price, net_pnl_pct, strategy, exit_reason, status
                           FROM trade_outcomes ORDER BY entry_time DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
                trades = [
                    {
                        "symbol": r[0],
                        "entry_price": r[1],
                        "exit_price": r[2],
                        "pnl": r[3],
                        "strategy": r[4],
                        "exit_reason": r[5],
                        "status": r[6],
                    }
                    for r in rows
                ]
                if trades:
                    return trades
            except Exception as e:
                logger.warning(f"get_trade_history: StateDB query failed: {e}")
        # Fallback to in-memory log (may be empty)
        if symbol:
            return [o for o in self.orders_log if o.get("symbol") == symbol][-limit:]
        return self.orders_log[-limit:]

    def get_all_positions(self) -> List[Dict]:
        """Get all positions with live PnL and TP/SL data.

        Batch-fetches all USDT ticker prices in ONE API call, then maps
        them to positions — avoids N+1 API calls when holding multiple
        positions.
        """
        # --- Batch price fetch (1 API call instead of N) ---
        price_map: Dict[str, float] = {}
        client = self._client
        if client is None and self.positions:
            # Lazy-create client so callers that omit binance_client still get live prices
            try:
                from src.binance_client import BinanceClient

                client = BinanceClient()  # type: ignore[assignment]
            except Exception as e:
                logger.warning(f"Could not lazy-create BinanceClient: {e}")
        if client is not None and self.positions:
            try:
                all_tickers = client.get_24hr_stats()  # returns List[Dict]
                if isinstance(all_tickers, list):
                    price_map = {
                        t["symbol"]: float(t["last_price"])
                        for t in all_tickers
                        if "symbol" in t and "last_price" in t
                    }
            except Exception as e:
                logger.warning(f"Batch price fetch failed, falling back to cached: {e}")

        results = []
        for pos in self.positions.values():
            symbol = pos["symbol"]
            # Use batch-fetched price, or fall back to cache / entry_price
            price_is_stale = False
            current_price = price_map.get(symbol)
            if current_price is None:
                current_price = pos.get("current_price", pos["entry_price"])
                price_is_stale = current_price == pos["entry_price"]
            if current_price != pos.get("current_price"):
                pos["current_price"] = current_price  # update cache

            pnl_data = self.calculate_pnl(symbol, current_price_override=current_price)
            enriched = {**pos, **pnl_data, "price_is_stale": price_is_stale}
            # Ensure take_profit is always present
            if "take_profit" not in enriched or enriched["take_profit"] is None:
                tp_default = pos["entry_price"] * (
                    1 + self.config.get("take_profit", {}).get("default_pct", 6.0) / 100
                )
                enriched["take_profit"] = tp_default
            # Ensure current_price is present
            if "current_price" not in enriched or enriched["current_price"] is None:
                enriched["current_price"] = current_price
            # Ensure entry_price uses DB value (not recalculated from trade history)
            enriched["entry_price"] = pos.get(
                "entry_price", enriched.get("entry_price", current_price)
            )
            results.append(enriched)
        return results

    def get_balance(self, asset: str) -> float:
        """Get total balance for an asset symbol (e.g. 'BTCUSDT') or 'cash'.

        Note: 'asset' must be a trading pair key (e.g. 'BTCUSDT'), not a base coin ('BTC').
        Use get_balance_by_coin() for base coin lookup.
        """
        if asset == "cash":
            return self.cash_balance
        pos = self.positions.get(asset)
        if pos:
            return pos["quantity"]
        return 0.0

    def get_available_balance(self, asset: str) -> float:
        """Get free (available) balance for an asset — use this for order sizing."""
        return self.get_balance(asset)

    def get_summary(self) -> Dict:
        """Get portfolio summary"""
        positions = self.get_all_positions()
        total_pnl = self.get_total_pnl()
        total_exposure = self.get_total_exposure()

        return {
            "timestamp": datetime.now().isoformat(),
            "cash": self.cash_balance,
            "total_exposure": total_exposure,
            "total_value": self.cash_balance + total_exposure,
            "total_pnl": total_pnl,
            "positions_count": len(positions),
            "positions": [{**p, **self.calculate_pnl(p["symbol"])} for p in positions],
            "risk_check": self.check_risk_limits(),
        }
