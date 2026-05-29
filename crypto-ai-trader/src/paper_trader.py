"""
Paper Trader — simulates order execution without placing real orders.

Drop-in replacement for BinanceClient that:
  • Intercepts place_order() calls and simulates fills with slippage + fees
  • Tracks a simulated portfolio (balance, positions, P&L) separately from real funds
  • Logs all paper trades to StateDB (paper_trades table)
  • Uses ccxt for real-time price data (same endpoint as live, no API keys needed for reads)
  • Switchable via TRADING_MODE env var ('paper' | 'live')

All market-data methods (klines, ticker, exchange_info, etc.) are delegated to the
real BinanceClient so the scanner/researcher/strategy pipeline sees live prices.
"""

import os
import time
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import ccxt
from dotenv import load_dotenv

# Load .env early
_project_root = Path(__file__).parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file, override=False)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PAPER_SLIPPAGE_PCT = float(os.environ.get("PAPER_SLIPPAGE_PCT", "0.05"))   # 0.05%
PAPER_FEE_RATE     = float(os.environ.get("PAPER_FEE_RATE", "0.001"))      # 0.1% (Binance standard)
PAPER_MIN_ORDER_USDT = float(os.environ.get("PAPER_MIN_ORDER_USDT", "10")) # $10 USDT minimum
PAPER_INITIAL_BALANCE = float(os.environ.get("PAPER_INITIAL_BALANCE", "10000"))  # $10,000 USDT starting


def is_paper_mode() -> bool:
    """Check if TRADING_MODE env var is set to 'paper'."""
    return os.environ.get("TRADING_MODE", "live").strip().lower() == "paper"


class _BinanceSDKProxy:
    """Proxy for the `.client` attribute on BinanceClient.

    Some code accesses `client.get_ticker_price(symbol)` directly
    (the python-binance SDK). This proxy redirects those calls to the
    PaperTrader's price-fetching methods so paper mode doesn't crash.
    """

    def __init__(self, paper_trader: "PaperTrader"):
        self._pt = paper_trader

    def ticker_price(self, symbol: str) -> Dict:
        """Return price in Binance SDK format: {"price": "123.45"}."""
        price = self._pt.get_current_price(symbol)
        return {"price": str(price)}

    def klines(self, symbol: str, interval: str = "1h", limit: int = 500, **kwargs):
        """Delegate klines to live client. Returns list of dicts (ccxt format)."""
        client = self._pt._get_live_client()
        if client and hasattr(client, "get_klines"):
            return client.get_klines(symbol, interval=interval, limit=limit, **kwargs)
        return []

    def depth(self, symbol: str, limit: int = 20) -> Dict:
        """Delegate order book depth to live client."""
        client = self._pt._get_live_client()
        if client and hasattr(client, "get_order_book"):
            return client.get_order_book(symbol, limit)
        return {"bids": [], "asks": []}

    def trades(self, symbol: str, limit: int = 1000) -> list:
        """Delegate recent trades to live client. Returns SDK-compatible format."""
        client = self._pt._get_live_client()
        if client and hasattr(client, "get_trades"):
            return client.get_trades(symbol, limit=limit)
        return []


class PaperTrader:
    """Paper trading engine that wraps BinanceClient for market data
    and simulates all order execution locally.

    Usage:
        if is_paper_mode():
            client = PaperTrader()           # paper mode
        else:
            client = BinanceClient()         # live mode
    """

    def __init__(self):
        # ── Read-only ccxt instance for price data (no API keys needed) ──
        self._price_exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        try:
            self._price_exchange.load_markets()
        except Exception as e:
            logger.warning("PaperTrader: failed to load markets on init: %s", e)

        # ── Delegating BinanceClient for market data that needs auth ──
        # Lazy-init so we don't require API keys just for paper mode
        self._live_client = None

        # ── Proxy `.client` attribute for code that accesses client.get_ticker_price() ──
        self.client = _BinanceSDKProxy(self)

        # ── Simulated state (persisted in StateDB) ──
        self._db = None  # lazy
        self._init_simulated_state()

        logger.info(
            "PaperTrader initialised — slippage=%.3f%% fee=%.3f%% min_order=$%.0f initial=$%.0f",
            PAPER_SLIPPAGE_PCT, PAPER_FEE_RATE * 100, PAPER_MIN_ORDER_USDT, PAPER_INITIAL_BALANCE,
        )

    # ------------------------------------------------------------------ helpers

    def _get_db(self):
        """Lazy StateDB import & singleton."""
        if self._db is None:
            from src.state_db import get_state_db
            self._db = get_state_db()
            self._ensure_paper_tables()
        return self._db

    def _conn(self):
        """Get DB connection (ensures lazy init)."""
        return self._get_db()._get_conn()

    def _ensure_paper_tables(self):
        """Create paper_trades and paper_portfolio tables if they don't exist."""
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                fill_price REAL NOT NULL,
                slippage_pct REAL,
                fee_usdt REAL,
                notional_usdt REAL,
                status TEXT DEFAULT 'filled',
                timestamp REAL NOT NULL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_time ON paper_trades(timestamp);

            CREATE TABLE IF NOT EXISTS paper_portfolio (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_pending_orders (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                stop_price REAL,
                status TEXT DEFAULT 'open',
                created_at REAL NOT NULL,
                expires_at REAL,
                details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_pending_symbol ON paper_pending_orders(symbol);
        """)
        conn.commit()

    def _get_sim_value(self, key: str, default: str = "0") -> str:
        db = self._get_db()  # ensure DB is initialized
        row = db._get_conn().execute(
            "SELECT value FROM paper_portfolio WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def _set_sim_value(self, key: str, value: str):
        now = time.time()
        db = self._get_db()
        db._get_conn().execute(
            """INSERT INTO paper_portfolio (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
               value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now),
        )
        self._conn().commit()

    def _init_simulated_state(self):
        """Initialize simulated balance from DB or set default."""
        # We need DB access here; use lazy pattern
        pass  # _get_sim_value handles defaults

    def _get_sim_balance(self) -> float:
        return float(self._get_sim_value("cash_balance", str(PAPER_INITIAL_BALANCE)))

    def _set_sim_balance(self, bal: float):
        self._set_sim_value("cash_balance", str(bal))

    def _get_sim_positions(self) -> Dict[str, Dict]:
        raw = self._get_sim_value("positions", "{}")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _set_sim_positions(self, positions: Dict[str, Dict]):
        self._set_sim_value("positions", json.dumps(positions))

    def _get_sim_pnl(self) -> float:
        return float(self._get_sim_value("realized_pnl", "0"))

    def _set_sim_pnl(self, pnl: float):
        self._set_sim_value("realized_pnl", str(pnl))

    def _get_sim_order_counter(self) -> int:
        return int(self._get_sim_value("order_counter", "0"))

    def _increment_order_counter(self) -> int:
        c = self._get_sim_order_counter() + 1
        self._set_sim_value("order_counter", str(c))
        return c

    # ---------------------------------------------------------------- price data

    def _get_live_client(self):
        """Lazy-init live BinanceClient for authenticated market data."""
        if self._live_client is None:
            try:
                from src.binance_client import BinanceClient
                self._live_client = BinanceClient(testnet=False)
            except Exception as e:
                logger.error("PaperTrader: failed to init live BinanceClient: %s", e)
        return self._live_client

    def get_current_price(self, symbol: str) -> float:
        """Fetch current price via public ccxt (no auth needed)."""
        try:
            ticker = self._price_exchange.fetch_ticker(symbol)
            return float(ticker.get("last", 0) or 0)
        except Exception as e:
            logger.error("PaperTrader: failed to get price for %s: %s", symbol, e)
            return 0.0

    # ---- Delegated market data methods (pass-through to live client) ----

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 500, **kwargs):
        client = self._get_live_client()
        if client:
            return client.get_klines(symbol, interval, limit, **kwargs)
        return []

    def get_24hr_stats(self, symbol: str = None, **kwargs):
        client = self._get_live_client()
        if client:
            return client.get_24hr_stats(symbol, **kwargs)
        return {} if symbol else []

    def get_order_book(self, symbol: str, limit: int = 20):
        client = self._get_live_client()
        if client:
            return client.get_order_book(symbol, limit)
        return {"bids": [], "asks": []}

    def get_symbols(self, quote: str = "USDT"):
        client = self._get_live_client()
        if client:
            return client.get_symbols(quote)
        return []

    def get_exchange_info(self) -> Dict:
        client = self._get_live_client()
        if client:
            return client.get_exchange_info()
        return {}

    def get_price_precision(self, symbol: str) -> int:
        client = self._get_live_client()
        if client:
            return client.get_price_precision(symbol)
        return 4

    def get_quantity_precision(self, symbol: str) -> int:
        client = self._get_live_client()
        if client:
            return client.get_quantity_precision(symbol)
        return 4

    def get_symbol_filters(self, symbol: str) -> Dict:
        client = self._get_live_client()
        if client:
            return client.get_symbol_filters(symbol)
        return {}

    def validate_symbol(self, symbol: str) -> bool:
        client = self._get_live_client()
        if client:
            return client.validate_symbol(symbol)
        return True

    def get_ticker_price(self, symbol: str) -> float:
        price = self.get_current_price(symbol)
        if price > 0:
            return price
        client = self._get_live_client()
        if client:
            return client.get_ticker_price(symbol)
        return 0.0

    # ============================================================ Simulated account

    def get_balance(self, asset: str = "USDT") -> float:
        """Get total simulated balance for an asset."""
        if asset == "USDT":
            return self._get_sim_balance()
        # For non-USDT assets, return position qty
        positions = self._get_sim_positions()
        pos = positions.get(asset, {})
        return pos.get("qty", 0.0)

    def get_free_balance(self, asset: str = "USDT") -> float:
        """Get free (available) simulated balance."""
        return self.get_balance(asset)

    def get_position(self, symbol: str) -> Dict:
        """Get simulated position for a symbol."""
        positions = self._get_sim_positions()
        base = symbol.replace("USDT", "").replace("USDC", "")
        pos = positions.get(base, {})
        return {
            "asset": base,
            "free": pos.get("qty", 0.0),
            "locked": 0.0,
            "total": pos.get("qty", 0.0),
        }

    def get_account(self) -> Dict:
        """Return simulated account balances in the same shape as BinanceClient.get_account()."""
        positions = self._get_sim_positions()
        usdt_bal = self._get_sim_balance()
        balances = [{"asset": "USDT", "free": str(usdt_bal), "locked": "0.0"}]
        for asset, pos in positions.items():
            qty = pos.get("qty", 0.0)
            balances.append({
                "asset": asset,
                "free": str(qty),
                "locked": "0.0",
            })
        return {"balances": balances}

    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Return simulated pending orders (limit orders awaiting fill)."""
        conn = self._conn()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM paper_pending_orders WHERE symbol = ? AND status = 'open'",
                (symbol,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_pending_orders WHERE status = 'open'"
            ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================ Order simulation

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float = None,
        price: float = None,
        stop_price: float = None,
        time_in_force: str = "GTC",
        retry: int = 3,
    ) -> Optional[Dict]:
        """Simulate an order fill.

        Returns a dict matching BinanceClient.place_order() shape:
            { 'orderId': ..., 'symbol': ..., 'side': ..., 'type': ...,
              'price': ..., 'origQty': ..., 'executedQty': ..., 'fills': [...] }
        """
        # Validate symbol
        if not self.validate_symbol(symbol):
            logger.error("PaperTrader: %s not in allowlist", symbol)
            return None

        current_price = self.get_current_price(symbol)
        if current_price <= 0:
            logger.error("PaperTrader: cannot get price for %s", symbol)
            return None

        side_upper = side.upper()
        type_upper = order_type.upper()

        # ── MARKET orders: instant fill at current price + slippage ──
        if type_upper == "MARKET":
            return self._fill_market(symbol, side_upper, quantity, current_price)

        # ── LIMIT orders: place as pending, fill later if price reaches ──
        if type_upper == "LIMIT" and price is not None:
            return self._place_limit(symbol, side_upper, quantity, price, stop_price)

        # ── STOP_LOSS_LIMIT: treat as limit that triggers at stop_price ──
        if type_upper in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            # For paper mode, simulate as limit at the stop_price (slippage-adjusted)
            limit_price = price if price is not None else stop_price
            if limit_price is None:
                logger.error("PaperTrader: STOP_LOSS_LIMIT requires price or stop_price")
                return None
            return self._place_limit(symbol, side_upper, quantity, limit_price, stop_price)

        logger.error("PaperTrader: unsupported order type %s", order_type)
        return None

    def place_market_buy(self, symbol: str, quantity: float) -> Optional[Dict]:
        return self.place_order(symbol, "BUY", "MARKET", quantity=quantity)

    def place_market_sell(self, symbol: str, quantity: float) -> Optional[Dict]:
        return self.place_order(symbol, "SELL", "MARKET", quantity=quantity)

    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Optional[Dict]:
        return self.place_order(symbol, "BUY", "LIMIT", quantity=quantity, price=price)

    def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Optional[Dict]:
        return self.place_order(symbol, "SELL", "LIMIT", quantity=quantity, price=price)

    def place_stop_loss_market(self, symbol: str, quantity: float, stop_price: float,
                               limit_price: float = None) -> Optional[Dict]:
        if limit_price is None:
            limit_price = round(stop_price * 0.995, 8)
        return self.place_order(
            symbol, "SELL", "STOP_LOSS_LIMIT",
            quantity=quantity, price=limit_price, stop_price=stop_price,
        )

    def place_stop_loss_limit(self, symbol: str, quantity: float,
                              price: float, stop_price: float) -> Optional[Dict]:
        return self.place_order(
            symbol, "SELL", "STOP_LOSS_LIMIT",
            quantity=quantity, price=price, stop_price=stop_price,
        )

    def place_oco(self, symbol: str, quantity: float,
                  tp_price: float, sl_price: float, sl_limit_price: float = None) -> Optional[Dict]:
        """Simulate OCO: place a limit SELL at tp_price and a stop-loss at sl_price."""
        # In paper mode, place the TP limit; the SL will be monitored separately
        if sl_limit_price is None:
            sl_limit_price = round(sl_price * 0.995, 8)

        # Place TP as limit sell
        tp_result = self.place_order(symbol, "SELL", "LIMIT", quantity=quantity, price=tp_price)
        if tp_result is None:
            return None

        # Place SL as stop_loss_limit
        sl_result = self.place_order(
            symbol, "SELL", "STOP_LOSS_LIMIT",
            quantity=quantity, price=sl_limit_price, stop_price=sl_price,
        )

        return {
            "orderId": tp_result.get("orderId", 0),
            "orderReports": [
                tp_result,
                sl_result or {"status": "failed"},
            ],
            "symbol": symbol,
            "type": "OCO",
            "side": "SELL",
            "origQty": str(quantity),
            "status": "filled" if tp_result else "partial",
        }

    # ---- Internal fill logic ----

    def _fill_market(self, symbol: str, side: str, quantity: float, current_price: float) -> Optional[Dict]:
        """Simulate a market order fill with slippage and fees."""
        if quantity is None or quantity <= 0:
            logger.error("PaperTrader: invalid quantity %s for %s", quantity, symbol)
            return None

        # Apply slippage
        if side == "BUY":
            fill_price = current_price * (1 + PAPER_SLIPPAGE_PCT / 100)
        else:
            fill_price = current_price * (1 - PAPER_SLIPPAGE_PCT / 100)

        notional = quantity * fill_price
        fee = notional * PAPER_FEE_RATE

        # Validate minimum order
        if notional < PAPER_MIN_ORDER_USDT:
            logger.warning(
                "PaperTrader: order too small $%.2f < $%.0f minimum", notional, PAPER_MIN_ORDER_USDT
            )
            return None

        # Check balance
        if side == "BUY":
            total_cost = notional + fee
            bal = self._get_sim_balance()
            if total_cost > bal:
                logger.error(
                    "PaperTrader: insufficient balance $%.2f (need $%.2f with fee)",
                    bal, total_cost,
                )
                return None
            # Deduct USDT
            self._set_sim_balance(bal - total_cost)
            # Add position
            self._add_position(symbol, quantity, fill_price)
        else:  # SELL
            base = symbol.replace("USDT", "").replace("USDC", "")
            positions = self._get_sim_positions()
            pos = positions.get(base, {})
            held = pos.get("qty", 0.0)
            if quantity > held + 1e-10:  # small float tolerance
                logger.error(
                    "PaperTrader: insufficient %s qty %.8f (have %.8f)",
                    base, quantity, held,
                )
                return None
            # Add USDT (net of fee)
            bal = self._get_sim_balance()
            self._set_sim_balance(bal + notional - fee)
            # Remove position
            self._remove_position(symbol, quantity)

        # Calculate slippage percentage
        slippage_pct = (fill_price - current_price) / current_price * 100

        # Calculate realized P&L for sells
        pnl = 0.0
        if side == "SELL":
            base = symbol.replace("USDT", "").replace("USDC", "")
            positions = self._get_sim_positions()
            # P&L was calculated before position removal, estimate from entry
            # Use the entry price from position data
            try:
                db = self._get_db()
                conn = db._get_conn()
                row = conn.execute(
                    "SELECT entry_price FROM paper_trades WHERE symbol = ? AND side = 'BUY' ORDER BY timestamp DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                if row:
                    entry = float(row["entry_price"])
                    pnl = (fill_price - entry) * quantity - fee
                    self._set_sim_pnl(self._get_sim_pnl() + pnl)
            except Exception:
                logger.error("Failed to calculate simulated PnL for SELL trade on %s", symbol, exc_info=True)

        # Record trade
        order_id = self._increment_order_counter()
        trade_id = f"paper_{order_id}_{int(time.time())}"
        now = time.time()

        conn = self._conn()
        conn.execute(
            """INSERT INTO paper_trades
               (id, symbol, side, order_type, quantity, fill_price, slippage_pct,
                fee_usdt, notional_usdt, status, timestamp, details)
               VALUES (?, ?, ?, 'MARKET', ?, ?, ?, ?, ?, 'filled', ?, ?)""",
            (trade_id, symbol, side, quantity, fill_price, slippage_pct,
             fee, notional, now, json.dumps({
                 "current_price": current_price,
                 "slippage_pct": slippage_pct,
                 "fee_rate": PAPER_FEE_RATE,
             })),
        )
        conn.commit()

        # Also record in standard trades table for backward compat
        try:
            self._db.trade_add(symbol, side, quantity, fill_price, pnl)
        except Exception:
            logger.error("Failed to record trade in trades table for %s %s", side, symbol, exc_info=True)

        fills_qty = quantity
        fills_price = fill_price

        result = {
            "orderId": order_id,
            "clientOrderId": f"paper_{trade_id}",
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "price": str(fill_price),
            "origQty": str(quantity),
            "executedQty": str(fills_qty),
            "cummulativeQuoteQty": str(notional),
            "fills": [{
                "price": str(fills_price),
                "qty": str(fills_qty),
                "commission": str(fee),
                "commissionAsset": "USDT",
            }],
            "_paper": {
                "slippage_pct": slippage_pct,
                "fee_usdt": fee,
                "pnl": pnl,
            },
        }

        logger.info(
            "📝 PAPER TRADE: %s %s %.8f @ $%.6f (slip=%.4f%% fee=$%.4f) bal=$%.2f",
            side, symbol, quantity, fill_price, slippage_pct, fee, self._get_sim_balance(),
        )

        return result

    def _place_limit(self, symbol: str, side: str, quantity: float, price: float,
                     stop_price: float = None) -> Optional[Dict]:
        """Place a simulated limit / stop-loss-limit order (pending until price fills)."""
        order_id = self._increment_order_counter()
        now = time.time()

        conn = self._conn()
        conn.execute(
            """INSERT INTO paper_pending_orders
               (id, symbol, side, order_type, quantity, price, stop_price, status, created_at, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
            (str(order_id), symbol, side,
             "STOP_LOSS_LIMIT" if stop_price else "LIMIT",
             quantity, price, stop_price, now, json.dumps({
                 "stop_price": stop_price,
             })),
        )
        conn.commit()

        logger.info(
            "📝 PAPER LIMIT ORDER: %s %s %.8f @ $%.6f (id=%s)",
            side, symbol, quantity, price, order_id,
        )

        # Check if limit is already fillable
        current_price = self.get_current_price(symbol)
        if current_price > 0:
            if side == "BUY" and current_price <= price:
                # Immediately fillable
                return self._fill_limit_order(str(order_id), current_price)
            elif side == "SELL" and current_price >= price:
                return self._fill_limit_order(str(order_id), current_price)

        # Return pending order info
        return {
            "orderId": order_id,
            "clientOrderId": f"paper_limit_{order_id}",
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "status": "NEW",
            "price": str(price),
            "origQty": str(quantity),
            "executedQty": "0",
            "_paper": {"pending": True, "stop_price": stop_price},
        }

    def _fill_limit_order(self, order_id: str, trigger_price: float) -> Optional[Dict]:
        """Fill a pending limit order."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM paper_pending_orders WHERE id = ? AND status = 'open'",
            (order_id,),
        ).fetchone()
        if not row:
            return None

        order = dict(row)
        symbol = order["symbol"]
        side = order["side"]
        quantity = order["quantity"]
        limit_price = order["price"]
        order_type = order.get("order_type", "LIMIT")

        # Fill at the limit price (with slippage)
        return self._fill_market(symbol, side, quantity, limit_price)

    # ---- Position management ----

    def _add_position(self, symbol: str, qty: float, entry_price: float):
        """Add to or create a position."""
        base = symbol.replace("USDT", "").replace("USDC", "")
        positions = self._get_sim_positions()
        if base in positions:
            old = positions[base]
            old_qty = old.get("qty", 0.0)
            old_entry = old.get("entry_price", 0.0)
            new_qty = old_qty + qty
            new_entry = (old_entry * old_qty + entry_price * qty) / new_qty if new_qty > 0 else entry_price
            positions[base] = {
                "qty": new_qty,
                "entry_price": new_entry,
                "symbol": symbol,
                "opened_at": old.get("opened_at", time.time()),
                "updated_at": time.time(),
            }
        else:
            positions[base] = {
                "qty": qty,
                "entry_price": entry_price,
                "symbol": symbol,
                "opened_at": time.time(),
                "updated_at": time.time(),
            }
        self._set_sim_positions(positions)

    def _remove_position(self, symbol: str, qty: float):
        """Remove qty from a position. Removes entirely if qty >= held."""
        base = symbol.replace("USDT", "").replace("USDC", "")
        positions = self._get_sim_positions()
        if base in positions:
            old = positions[base]
            new_qty = old.get("qty", 0.0) - qty
            if new_qty <= 1e-10:
                del positions[base]
            else:
                positions[base]["qty"] = new_qty
                positions[base]["updated_at"] = time.time()
            self._set_sim_positions(positions)

    # ============================================================ P&L & history

    def get_pnl(self) -> Dict:
        """Return paper trading P&L summary."""
        positions = self._get_sim_positions()
        balance = self._get_sim_balance()
        realized_pnl = self._get_sim_pnl()

        # Calculate unrealized P&L from open positions
        unrealized_pnl = 0.0
        position_details = []
        for base, pos in positions.items():
            symbol = pos.get("symbol", f"{base}USDT")
            current = self.get_current_price(symbol)
            entry = pos.get("entry_price", 0)
            qty = pos.get("qty", 0)
            unrealized = (current - entry) * qty if current > 0 else 0
            unrealized_pnl += unrealized
            position_details.append({
                "symbol": symbol,
                "qty": qty,
                "entry_price": entry,
                "current_price": current,
                "unrealized_pnl": unrealized,
                "pnl_pct": ((current / entry - 1) * 100) if entry > 0 else 0,
            })

        # Calculate total portfolio value
        total_value = balance
        for pos in positions.values():
            sym = pos.get("symbol", f"{pos.get('entry_price', 0)}")
            # Use qty * entry as conservative estimate
            total_value += pos.get("qty", 0) * pos.get("entry_price", 0)

        return {
            "cash_balance": balance,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": realized_pnl + unrealized_pnl,
            "positions": position_details,
            "total_positions": len(positions),
            "portfolio_value": total_value,
        }

    def get_trade_history(self, symbol: str = None, limit: int = 50) -> List[Dict]:
        """Get paper trade history."""
        conn = self._conn()
        if symbol:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_positions(self) -> Dict[str, Dict]:
        """Get all simulated positions."""
        positions = self._get_sim_positions()
        # Enrich with current prices
        for base, pos in positions.items():
            symbol = pos.get("symbol", f"{base}USDT")
            pos["current_price"] = self.get_current_price(symbol)
            if pos.get("entry_price", 0) > 0:
                pos["pnl_pct"] = ((pos["current_price"] / pos["entry_price"] - 1) * 100
                                  if pos["current_price"] > 0 else 0)
            else:
                pos["pnl_pct"] = 0
        return positions

    # ---- Limit order checking (call periodically) ----

    def check_pending_orders(self):
        """Check and fill any pending limit orders that have been triggered."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM paper_pending_orders WHERE status = 'open'"
        ).fetchall()

        for row in rows:
            order = dict(row)
            symbol = order["symbol"]
            side = order["side"]
            price = order["price"]

            current = self.get_current_price(symbol)
            if current <= 0:
                continue

            should_fill = False
            if side == "BUY" and current <= price:
                should_fill = True
            elif side == "SELL" and current >= price:
                should_fill = True

            if should_fill:
                logger.info(
                    "📝 PAPER LIMIT FILLED: %s %s %.8f @ $%.6f (triggered at $%.6f)",
                    side, symbol, order["quantity"], price, current,
                )
                self._fill_limit_order(str(order["id"]), current)

    # ---- Cleanup ----

    def close(self):
        """Clean up resources."""
        pass  # No persistent connections in paper mode

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------
_paper_trader_instance: Optional[PaperTrader] = None


def get_paper_trader() -> PaperTrader:
    """Get or create the PaperTrader singleton."""
    global _paper_trader_instance
    if _paper_trader_instance is None:
        _paper_trader_instance = PaperTrader()
    return _paper_trader_instance


def get_trading_client():
    """Factory: returns PaperTrader if TRADING_MODE=paper, else BinanceClient.

    Usage in scan_orchestrator / trade_executor:
        from src.paper_trader import get_trading_client
        client = get_trading_client()
    """
    if is_paper_mode():
        return get_paper_trader()
    else:
        from src.binance_client import BinanceClient
        return BinanceClient(testnet=False)
