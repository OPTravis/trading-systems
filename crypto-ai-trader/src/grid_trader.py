"""
Grid Trading Bot — Binance SPOT

Stateful grid trader that places limit buy/sell orders at evenly spaced
price levels. When a buy fills, a sell is placed at the next level up;
when a sell fills, a buy is placed at the next level down.
"""

import json
import logging

# Python 3.11.15 (uv build) removed random.randbits
import random as _r
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

if not hasattr(_r, "randbits"):
    _r.randbits = _r.getrandbits  # type: ignore[attr-defined]

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient

logger = logging.getLogger(__name__)

from src.strategy_guard import strategy_guard

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = DATA_DIR / "grid_state.json"
FEE_RATE = 0.001  # 0.1% per side
MIN_NOTIONAL = 5.0  # Binance minimum order value


class GridBot:
    """Spot grid trading bot for a single symbol.

    PRIMARY STORAGE: SQLite state.db (via StateDB) - kv store
    BACKUP: data/grid_state.json (human-readable)
    """

    DB_KEY = "grid_state"

    def __init__(self, client: "ExchangeClient"):
        self.client = client
        self.state: Dict[str, Any] = {}
        self._load_state()

    # ────────────────────── State Persistence ──────────────────────

    def _load_state(self):
        # --- Try dedicated grid_state table first ---
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            db_state = db.grid_get_all()
            # grid_get_all returns {symbol: {...}}, we need to find the active one
            # or use the legacy single-grid approach
            if db_state:
                # Take the first one (legacy: only one grid at a time)
                symbol = list(db_state.keys())[0]
                self.state = db_state[symbol]
                logger.info(
                    f"Grid state loaded from grid_state table: {self.state.get('symbol')} status={self.state.get('status')}"
                )
                return
        except Exception as e:
            logger.warning(f"GridBot: failed to load from grid_state table: {e}")
        # --- Fallback to legacy kv store ---
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            db_state = db.kv_get(self.DB_KEY)
            if db_state:
                self.state = db_state
                logger.info(
                    f"Grid state loaded from kv store: {self.state.get('symbol')} status={self.state.get('status')}"
                )
                # Migrate to new table
                try:
                    db.grid_set(self.state.get("symbol", "UNKNOWN"), self.state)
                    logger.info("Grid state migrated from kv to grid_state table")
                except Exception as e:
                    logger.warning(
                        f"GridBot: migration to grid_state table failed: {e}"
                    )
                return
        except Exception as e:
            logger.warning(f"GridBot: failed to load from kv store: {e}")
        # --- Fallback to JSON ---
        if STATE_FILE.exists():
            with open(STATE_FILE) as f:
                self.state = json.load(f)
            logger.info(
                f"Grid state loaded from JSON: {self.state.get('symbol')} status={self.state.get('status')}"
            )
            # Migrate to SQLite
            try:
                from src.state_db import get_state_db

                db = get_state_db()
                db.grid_set(self.state.get("symbol", "UNKNOWN"), self.state)
                logger.info("Grid state migrated from JSON to SQLite grid_state table")
            except Exception as e:
                logger.warning(f"GridBot: migration to SQLite failed: {e}")

    def _save_state(self):
        self.state["stats"]["last_check"] = datetime.now(timezone.utc).isoformat()
        # --- SQLite sole source of truth (grid_state table) ---
        try:
            from src.state_db import get_state_db

            db = get_state_db()
            symbol = self.state.get("symbol", "UNKNOWN")
            db.grid_set(symbol, self.state)
        except Exception as e:
            logger.error(f"GridBot: SQLite grid_state save failed: {e}")

    # ────────────────────── Grid Setup ──────────────────────

    def init_grid(
        self,
        symbol: str,
        total_capital: float,
        grid_count: int = 8,
        range_pct: float = 5.0,
        rebalance_interval_hours: int = 24,
        max_range_pct: float = 15.0,
        adaptive: bool = True,
    ) -> Dict:
        """Configure grid levels around current price. Does NOT place orders.

        When adaptive=True, uses ATR to dynamically set range_pct and grid_count.
        High ATR → wider range, fewer grids (less whipsaw).
        Low ATR → tighter range, more grids (more fills).
        """
        # Validate symbol
        if not self.client.validate_symbol(symbol):
            return {"error": f"{symbol} not in ALLOWED_SYMBOLS"}

        # Get current price
        stats = self.client.get_24hr_stats(symbol)
        if not stats:
            return {"error": f"Cannot get price for {symbol}"}
        current_price = stats["last_price"]

        # ATR-based adaptive parameters
        atr = 0.0
        atr_pct = 0.0
        if adaptive:
            try:
                klines = self.client.get_klines(symbol, "1h", 24)
                if klines and len(klines) >= 14:
                    closes = [k["close"] for k in klines]
                    highs = [k["high"] for k in klines]
                    lows = [k["low"] for k in klines]
                    trs = []
                    for i in range(1, len(closes)):
                        tr = max(
                            highs[i] - lows[i],
                            abs(highs[i] - closes[i - 1]),
                            abs(lows[i] - closes[i - 1]),
                        )
                        trs.append(tr)
                    atr = sum(trs[-14:]) / min(len(trs), 14) if trs else 0
                    atr_pct = (atr / current_price) * 100 if current_price > 0 else 0

                    # Adaptive range: 3x ATR gives ~99.7% coverage
                    if atr_pct > 0:
                        range_pct = max(2.0, min(15.0, atr_pct * 3.0))
                    # Adaptive grid count: tighter spacing in low vol
                    if atr_pct > 3.0:
                        grid_count = max(4, min(6, grid_count))  # high vol: fewer grids
                    elif atr_pct > 1.5:
                        grid_count = max(6, min(10, grid_count))  # medium
                    else:
                        grid_count = max(8, min(15, grid_count))  # low vol: more grids

                    logger.info(
                        "Adaptive grid: ATR=%.4f (%.2f%%) → range=%.1f%% grids=%d",
                        atr,
                        atr_pct,
                        range_pct,
                        grid_count,
                    )
            except Exception as e:
                logger.warning("ATR calculation failed, using defaults: %s", e)

        # Calculate grid range
        grid_lower = current_price * (1 - range_pct / 100)
        grid_upper = current_price * (1 + range_pct / 100)
        spacing = (grid_upper - grid_lower) / grid_count
        capital_per_grid = total_capital / grid_count

        # Validate
        if capital_per_grid < MIN_NOTIONAL:
            return {
                "error": f"Capital per grid ${capital_per_grid:.2f} < min notional ${MIN_NOTIONAL}"
            }

        min_profitable_spacing = 2 * FEE_RATE * current_price
        if spacing < min_profitable_spacing:
            return {
                "error": f"Grid spacing ${spacing:.6f} too small for fees (need >${min_profitable_spacing:.6f})"
            }

        # Generate grid levels
        grid_levels = []
        for i in range(grid_count + 1):
            price = grid_lower + i * spacing
            grid_levels.append(
                {
                    "index": i,
                    "price": round(price, 8),
                    "buy_order_id": None,
                    "sell_order_id": None,
                    "coin_qty": 0.0,
                    "status": "empty",  # empty, bought, sold
                }
            )

        self.state = {
            "symbol": symbol,
            "status": "initialized",
            "config": {
                "grid_count": grid_count,
                "range_pct": round(range_pct, 2),
                "capital_per_grid": round(capital_per_grid, 2),
                "total_capital": total_capital,
                "fee_rate": FEE_RATE,
                "rebalance_interval_hours": rebalance_interval_hours,
                "max_range_pct": max_range_pct,
                "grid_lower": round(grid_lower, 8),
                "grid_upper": round(grid_upper, 8),
                "spacing": round(spacing, 8),
                "adaptive": adaptive,
                "atr": round(atr, 6),
                "atr_pct": round(atr_pct, 2),
            },
            "grid_levels": grid_levels,
            "stats": {
                "total_trades": 0,
                "realized_pnl": 0.0,
                "total_fees": 0.0,
                "started_at": None,
                "last_rebalance": None,
                "last_check": None,
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state()
        return {
            "status": "initialized",
            "symbol": symbol,
            "current_price": current_price,
            "grid_range": f"{grid_lower:.4f} - {grid_upper:.4f}",
            "spacing": f"{spacing:.4f} ({spacing / current_price * 100:.2f}%)",
            "capital_per_grid": capital_per_grid,
            "levels": [round(gl["price"], 4) for gl in grid_levels],
        }

    # ────────────────────── Start / Stop / Pause ──────────────────────

    def start(self, dry_run: bool = False) -> Dict:
        """Place initial grid orders."""
        if not self.state or self.state.get("status") == "running":
            return {"error": "Grid not initialized or already running"}

        symbol = self.state["symbol"]
        config = self.state["config"]
        current_price = self._get_current_price(symbol)
        if not current_price:
            return {"error": "Cannot get current price"}

        # Get exchange info for precision
        precision = self._get_symbol_precision(symbol)

        placed_buys = 0
        placed_sells = 0
        coin_held = self._get_coin_balance(symbol)

        for level in self.state["grid_levels"]:
            idx = level["index"]
            price = level["price"]

            if price < current_price:
                # Place buy order
                qty = config["capital_per_grid"] / price
                qty = self._round_qty(qty, precision["qty_decimals"])
                if qty * price < MIN_NOTIONAL:
                    continue
                if dry_run:
                    level["buy_order_id"] = f"DRY-BUY-{idx}"
                    level["status"] = "pending_buy"
                    placed_buys += 1
                else:
                    result = self.client.place_limit_buy(symbol, qty, price)
                    if result:
                        level["buy_order_id"] = result["orderId"]
                        level["status"] = "pending_buy"
                        placed_buys += 1

            elif price > current_price and coin_held > 0:
                # Place sell order (if we have coins from previous run)
                qty = config["capital_per_grid"] / price
                qty = self._round_qty(qty, precision["qty_decimals"])
                if qty > coin_held:
                    qty = self._round_qty(coin_held, precision["qty_decimals"])
                if qty * price < MIN_NOTIONAL:
                    continue
                if dry_run:
                    level["sell_order_id"] = f"DRY-SELL-{idx}"
                    level["status"] = "pending_sell"
                    placed_sells += 1
                else:
                    result = self.client.place_limit_sell(symbol, qty, price)
                    if result:
                        level["sell_order_id"] = result["orderId"]
                        level["status"] = "pending_sell"
                        placed_sells += 1

        self.state["status"] = "running"
        self.state["stats"]["started_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

        return {
            "status": "running",
            "dry_run": dry_run,
            "placed_buys": placed_buys,
            "placed_sells": placed_sells,
            "current_price": current_price,
        }

    def stop(self) -> Dict:
        """Cancel all orders and stop."""
        symbol = self.state.get("symbol")
        if not symbol:
            return {"error": "No active grid"}

        # Cancel all orders
        if self.state.get("status") == "running":
            try:
                self.client.cancel_all_orders(symbol)
            except Exception as e:
                logger.warning(f"cancel_all_orders failed (may have no orders): {e}")

        # Calculate final equity
        equity = self._calculate_equity()

        self.state["status"] = "stopped"
        self._save_state()

        return {
            "status": "stopped",
            "symbol": symbol,
            "final_equity": equity["total"],
            "realized_pnl": self.state["stats"]["realized_pnl"],
            "total_trades": self.state["stats"]["total_trades"],
        }

    def pause(self) -> Dict:
        """Cancel all orders but keep state."""
        symbol = self.state.get("symbol")
        if not symbol:
            return {"error": "No active grid"}

        if self.state.get("status") == "running":
            try:
                self.client.cancel_all_orders(symbol)
            except Exception as e:
                logger.warning(f"cancel_all_orders failed (may have no orders): {e}")

        self.state["status"] = "paused"
        self._save_state()

        return {"status": "paused", "symbol": symbol}

    # ────────────────────── Core Loop: Tick ──────────────────────

    @strategy_guard(
        max_failures=3,
        cooldown_sec=60,
        default_return={"status": "error", "action": "skip"},
    )
    def tick(self) -> Dict:
        """Main loop — detect fills, place counter orders, check rebalance."""
        if self.state.get("status") != "running":
            return {"status": self.state.get("status", "none"), "action": "skip"}

        symbol = self.state["symbol"]
        config = self.state["config"]
        current_price = self._get_current_price(symbol)
        if not current_price:
            return {"error": "Cannot get price"}

        # 1. Detect filled orders
        fills = self._detect_fills(symbol)
        fills_processed = 0

        for fill in fills:
            fills_processed += 1
            level_idx = fill["level_index"]
            side = fill["side"]  # "buy" or "sell"
            level = self.state["grid_levels"][level_idx]

            if side == "buy":
                # Buy filled → place sell at next level up
                qty = fill["qty"]
                level["coin_qty"] = qty
                level["buy_order_id"] = None
                level["status"] = "bought"

                # Calculate fee
                fee = qty * level["price"] * config["fee_rate"]
                self.state["stats"]["total_fees"] += fee

                # Place sell at level+1
                if level_idx + 1 < len(self.state["grid_levels"]):
                    sell_level = self.state["grid_levels"][level_idx + 1]
                    sell_price = sell_level["price"]
                    precision = self._get_symbol_precision(symbol)
                    sell_qty = self._round_qty(qty, precision["qty_decimals"])
                    if sell_qty * sell_price >= MIN_NOTIONAL:
                        result = self.client.place_limit_sell(
                            symbol, sell_qty, sell_price
                        )
                        if result:
                            sell_level["sell_order_id"] = result["orderId"]
                            sell_level["status"] = "pending_sell"

                self.state["stats"]["total_trades"] += 1

            elif side == "sell":
                # Sell filled → place buy at next level down
                qty = fill["qty"]
                level["coin_qty"] = 0
                level["sell_order_id"] = None
                level["status"] = "sold"

                # Calculate PnL for this round-trip
                buy_level_idx = level_idx - 1
                if buy_level_idx >= 0:
                    buy_price = self.state["grid_levels"][buy_level_idx]["price"]
                    profit = (
                        qty * (level["price"] - buy_price)
                        - qty * level["price"] * config["fee_rate"] * 2
                    )
                    self.state["stats"]["realized_pnl"] += profit

                fee = qty * level["price"] * config["fee_rate"]
                self.state["stats"]["total_fees"] += fee

                # Place buy at level-1
                if level_idx - 1 >= 0:
                    buy_level = self.state["grid_levels"][level_idx - 1]
                    buy_price = buy_level["price"]
                    precision = self._get_symbol_precision(symbol)
                    buy_qty = self._round_qty(qty, precision["qty_decimals"])
                    if buy_qty * buy_price >= MIN_NOTIONAL:
                        result = self.client.place_limit_buy(symbol, buy_qty, buy_price)
                        if result:
                            buy_level["buy_order_id"] = result["orderId"]
                            buy_level["status"] = "pending_buy"

                self.state["stats"]["total_trades"] += 1

        # 2. Check rebalance
        rebalanced = False
        if self._should_rebalance(current_price):
            rebalanced = self._rebalance(current_price)

        # 3. Breakout detection — pause grid if strong trend detected
        breakout_action = None
        if config.get("adaptive") and self.state.get("status") == "running":
            breakout_action = self._check_breakout(symbol, current_price)

        self._save_state()

        equity = self._calculate_equity()
        result = {
            "status": self.state.get("status", "running"),
            "symbol": symbol,
            "current_price": current_price,
            "fills_processed": fills_processed,
            "rebalanced": rebalanced,
            "breakout": breakout_action,
            "total_trades": self.state["stats"]["total_trades"],
            "realized_pnl": round(self.state["stats"]["realized_pnl"], 4),
            "equity": equity["total"],
        }
        return result

    # ────────────────────── Fill Detection ──────────────────────

    def _detect_fills(self, symbol: str) -> List[Dict]:
        """Compare open orders against state to find fills."""
        fills = []
        open_orders = self.client.get_open_orders(symbol)
        open_order_ids = {o["orderId"] for o in open_orders}

        for level in self.state["grid_levels"]:
            # Check buy orders
            buy_id = level.get("buy_order_id")
            if buy_id and buy_id not in open_order_ids and isinstance(buy_id, int):
                # Verify fill via order history
                fill_info = self._verify_fill(symbol, buy_id)
                if fill_info:
                    fills.append(
                        {
                            "level_index": level["index"],
                            "side": "buy",
                            "qty": fill_info["qty"],
                            "price": fill_info["price"],
                        }
                    )

            # Check sell orders
            sell_id = level.get("sell_order_id")
            if sell_id and sell_id not in open_order_ids and isinstance(sell_id, int):
                fill_info = self._verify_fill(symbol, sell_id)
                if fill_info:
                    fills.append(
                        {
                            "level_index": level["index"],
                            "side": "sell",
                            "qty": fill_info["qty"],
                            "price": fill_info["price"],
                        }
                    )

        return fills

    def _verify_fill(self, symbol: str, order_id: int) -> Optional[Dict]:
        """Check order status to confirm fill."""
        try:
            # Use get_open_orders to check — if not found, it's filled or cancelled
            # More reliable: query order directly
            order = self.client.get_order(symbol=symbol, order_id=order_id)
            if order and order.get("status") == "FILLED":
                return {
                    "qty": float(order["executedQty"]),
                    "price": float(order.get("avgPrice") or order.get("price", 0)),
                }
        except Exception as e:
            logger.warning(f"Could not verify order {order_id}: {e}")
        return None

    # ────────────────────── Breakout Detection ──────────────────────

    def _check_breakout(self, symbol: str, current_price: float) -> Optional[str]:
        """Detect if price is trending strongly and grid should pause.

        Uses 4h klines + SMA to detect trend:
        - Price above SMA20 + rising → upside breakout → pause grid (ride the trend)
        - Price below SMA20 - falling → downside breakout → pause grid (avoid catching knives)
        - Range-bound → continue grid

        Returns: "paused_up", "paused_down", "resumed", or None
        """
        try:
            klines = self.client.get_klines(symbol, "4h", 30)
            if not klines or len(klines) < 20:
                return None

            closes = [k["close"] for k in klines]
            sma20 = sum(closes[-20:]) / 20
            sma5 = sum(closes[-5:]) / 5

            above_sma = current_price > sma20
            rising = sma5 > sma20
            below_sma = current_price < sma20
            falling = sma5 < sma20

            prev_status = self.state.get("status")

            if (
                above_sma
                and rising
                and current_price > self.state["config"]["grid_upper"]
            ):
                # Strong upside breakout — pause to ride trend
                if prev_status == "running":
                    self.pause()
                    logger.info(
                        "Breakout UP detected: price=%.4f > grid_upper=%.4f, SMA20=%.4f → paused",
                        current_price,
                        self.state["config"]["grid_upper"],
                        sma20,
                    )
                    return "paused_up"

            elif (
                below_sma
                and falling
                and current_price < self.state["config"]["grid_lower"]
            ):
                # Strong downside breakout — pause to avoid catching falling knife
                if prev_status == "running":
                    self.pause()
                    logger.info(
                        "Breakout DOWN detected: price=%.4f < grid_lower=%.4f, SMA20=%.4f → paused",
                        current_price,
                        self.state["config"]["grid_lower"],
                        sma20,
                    )
                    return "paused_down"

            elif prev_status == "paused":
                # Price returned to range — resume grid
                grid_lower = self.state["config"]["grid_lower"]
                grid_upper = self.state["config"]["grid_upper"]
                if grid_lower <= current_price <= grid_upper:
                    # Re-init with adaptive params at new price
                    self._rebalance(current_price)
                    self.state["status"] = "running"
                    logger.info("Price returned to range → grid resumed")
                    return "resumed"

        except Exception as e:
            logger.warning("Breakout check failed: %s", e)

        return None

    # ────────────────────── Rebalance ──────────────────────

    def _should_rebalance(self, current_price: float) -> bool:
        config = self.state["config"]
        grid_lower = config["grid_lower"]
        grid_upper = config["grid_upper"]
        max_range = config["max_range_pct"] / 100

        # Price broke out of range
        if current_price > grid_upper * (
            1 + max_range
        ) or current_price < grid_lower * (1 - max_range):
            return True

        # Time-based rebalance
        last_rebalance = self.state["stats"].get("last_rebalance")
        if last_rebalance:
            last_dt = datetime.fromisoformat(last_rebalance)
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since >= config["rebalance_interval_hours"]:
                return True

        return False

    def _rebalance(self, current_price: float) -> bool:
        """Cancel all, recalculate grid, re-place orders."""
        symbol = self.state["symbol"]
        config = self.state["config"]

        logger.info(f"Rebalancing grid for {symbol} at price {current_price}")

        # Cancel all orders
        self.client.cancel_all_orders(symbol)

        # Sell any held coin at market
        coin_held = self._get_coin_balance(symbol)
        if coin_held > 0:
            precision = self._get_symbol_precision(symbol)
            qty = self._round_qty(coin_held, precision["qty_decimals"])
            if qty * current_price >= MIN_NOTIONAL:
                self.client.place_market_sell(symbol, qty)

        # Recalculate grid range
        range_pct = config["range_pct"]
        grid_lower = current_price * (1 - range_pct / 100)
        grid_upper = current_price * (1 + range_pct / 100)
        spacing = (grid_upper - grid_lower) / config["grid_count"]

        # Update state
        config["grid_lower"] = round(grid_lower, 8)
        config["grid_upper"] = round(grid_upper, 8)
        config["spacing"] = round(spacing, 8)

        for level in self.state["grid_levels"]:
            level["price"] = round(grid_lower + level["index"] * spacing, 8)
            level["buy_order_id"] = None
            level["sell_order_id"] = None
            level["coin_qty"] = 0.0
            level["status"] = "empty"

        # Re-place orders
        self.start()
        self.state["stats"]["last_rebalance"] = datetime.now(timezone.utc).isoformat()
        return True

    # ────────────────────── Backtest / Simulate ──────────────────────

    def simulate_grid(
        self,
        symbol: str,
        lower: float,
        upper: float,
        grids: int,
        investment: float,
        days: int = 30,
    ) -> Dict:
        """Simulate grid trading over historical data (alias for backtest).

        Args:
            symbol: Trading pair
            lower: Grid lower bound price
            upper: Grid upper bound price
            grids: Number of grid levels
            investment: Total capital to deploy
            days: Historical period to simulate

        Returns:
            Dict with trades_count, total_profit, max_drawdown, etc.
        """
        range_pct = ((upper - lower) / ((upper + lower) / 2)) * 100
        result = self.backtest(
            symbol, investment, grid_count=grids, range_pct=range_pct, days=days
        )
        # Normalize output format
        if "error" in result:
            return {"trades_count": 0, "total_profit": 0, "error": result["error"]}
        return {
            "symbol": result["symbol"],
            "trades_count": result["total_trades"],
            "total_profit": result["realized_pnl"],
            "total_return_pct": result["total_return_pct"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "final_equity": result["final_equity"],
            "buy_hold_pct": result["buy_hold_pct"],
            "period": result["period"],
        }

    def backtest(
        self,
        symbol: str,
        total_capital: float,
        grid_count: int = 8,
        range_pct: float = 5.0,
        days: int = 30,
    ) -> Dict:
        """Run historical simulation using 1h klines."""
        klines = self.client.get_klines(symbol, interval="1h", limit=days * 24)
        if len(klines) < 24:
            return {"error": f"Not enough data: {len(klines)} candles"}

        closes = [k["close"] for k in klines]
        highs = [k["high"] for k in klines]
        lows = [k["low"] for k in klines]

        start_price = closes[0]
        grid_lower = start_price * (1 - range_pct / 100)
        grid_upper = start_price * (1 + range_pct / 100)
        spacing = (grid_upper - grid_lower) / grid_count
        capital_per_grid = total_capital / grid_count

        grid_prices = [grid_lower + i * spacing for i in range(grid_count + 1)]

        # Simulation state
        cash = total_capital
        coin = 0.0
        trades = 0
        realized_pnl = 0.0
        peak_equity = total_capital
        max_drawdown = 0.0

        pending_buys = set()  # grid indices with pending buys
        pending_sells: Dict[int, float] = {}  # grid_index -> qty

        # Initialize buys below current price
        for i, gp in enumerate(grid_prices[:-1]):
            if gp < start_price:
                pending_buys.add(i)

        for hour in range(1, len(closes)):
            low = lows[hour]
            high = highs[hour]

            # Check buys
            for i in sorted(pending_buys):
                gp = grid_prices[i]
                if low <= gp and cash >= capital_per_grid:
                    qty = capital_per_grid / gp
                    fee = qty * gp * FEE_RATE
                    cash -= qty * gp + fee
                    coin += qty
                    pending_buys.discard(i)
                    # Place sell at next level
                    if i + 1 < len(grid_prices):
                        pending_sells[i + 1] = pending_sells.get(i + 1, 0) + qty
                    trades += 1

            # Check sells
            for i in list(pending_sells.keys()):
                gp = grid_prices[i]
                sell_qty = pending_sells[i]
                if high >= gp and sell_qty > 0 and coin >= sell_qty:
                    fee = sell_qty * gp * FEE_RATE
                    cash += sell_qty * gp - fee
                    coin -= sell_qty
                    # Calculate profit
                    buy_level = i - 1
                    if buy_level >= 0:
                        profit = (
                            sell_qty * (gp - grid_prices[buy_level])
                            - sell_qty * gp * FEE_RATE * 2
                        )
                        realized_pnl += profit
                    pending_sells.pop(i)
                    pending_buys.add(i - 1)
                    trades += 1

            # Track equity
            equity = cash + coin * closes[hour]
            peak_equity = max(peak_equity, equity)
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            max_drawdown = max(max_drawdown, dd)

        final_equity = cash + coin * closes[-1]
        total_return = (final_equity - total_capital) / total_capital * 100
        buy_hold = (closes[-1] - closes[0]) / closes[0] * 100

        return {
            "symbol": symbol,
            "period": f"{days} days ({len(closes)} hours)",
            "grid_count": grid_count,
            "range_pct": range_pct,
            "spacing_pct": round(spacing / start_price * 100, 2),
            "total_return_pct": round(total_return, 2),
            "buy_hold_pct": round(buy_hold, 2),
            "total_trades": trades,
            "avg_trades_per_day": round(trades / days, 1),
            "realized_pnl": round(realized_pnl, 4),
            "max_drawdown_pct": round(max_drawdown, 2),
            "final_equity": round(final_equity, 2),
            "start_price": closes[0],
            "end_price": closes[-1],
        }

    # ────────────────────── Status ──────────────────────

    def get_status(self) -> Dict:
        if not self.state:
            return {"status": "none", "message": "No grid initialized"}

        equity = self._calculate_equity()
        config = self.state["config"]

        return {
            "symbol": self.state["symbol"],
            "status": self.state["status"],
            "grid_range": f"{config['grid_lower']:.4f} - {config['grid_upper']:.4f}",
            "spacing": f"{config['spacing']:.4f} ({config['spacing'] / ((config['grid_lower'] + config['grid_upper']) / 2) * 100:.2f}%)",
            "capital_per_grid": config["capital_per_grid"],
            "total_capital": config["total_capital"],
            "equity": equity,
            "stats": self.state["stats"],
            "active_levels": sum(
                1 for l in self.state["grid_levels"] if l["status"] not in ("empty",)
            ),
        }

    # ────────────────────── Helpers ──────────────────────

    def _get_current_price(self, symbol: str) -> Optional[float]:
        stats = self.client.get_24hr_stats(symbol)
        return stats.get("last_price") if stats else None

    def _get_coin_balance(self, symbol: str) -> float:
        pos = self.client.get_position(symbol)
        return pos.get("free", 0) if pos else 0

    def _get_symbol_precision(self, symbol: str) -> Dict:
        try:
            exchange_info = self.client.get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol), None
            )
            if sym_info:
                price_dec = 8
                qty_dec = 4
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        price_dec = len(f["tickSize"].rstrip("0").split(".")[-1])
                    elif f["filterType"] == "LOT_SIZE":
                        qty_dec = len(f["stepSize"].rstrip("0").split(".")[-1])
                return {"price_decimals": price_dec, "qty_decimals": qty_dec}
        except Exception:
            logger.error(
                "Failed to get symbol precision for %s from exchange info",
                symbol,
                exc_info=True,
            )
        return {"price_decimals": 4, "qty_decimals": 4}

    def _round_qty(self, qty: float, decimals: int) -> float:
        """Floor quantity to avoid exceeding actual balance (Binance rejects oversell)."""
        import math

        if decimals <= 0:
            return float(math.floor(qty))
        step = 10**-decimals
        return math.floor(qty / step) * step

    def _calculate_equity(self) -> Dict:
        symbol = self.state.get("symbol")
        if not symbol:
            return {"cash": 0, "coin_value": 0, "total": 0}

        self.state["config"]
        cash = self.client.get_free_balance("USDT") or 0
        coin_qty = self._get_coin_balance(symbol)
        price = self._get_current_price(symbol) or 0
        coin_value = coin_qty * price
        total = cash + coin_value

        return {
            "cash": round(cash, 2),
            "coin_qty": round(coin_qty, 4),
            "coin_value": round(coin_value, 2),
            "total": round(total, 2),
        }
