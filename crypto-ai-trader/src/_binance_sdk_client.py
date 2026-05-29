"""
Binance SPOT API Client Wrapper
SPOT ONLY - no futures, no margin, no leverage
"""

import os
import time
import logging
import ssl
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from binance.spot import Spot as BinanceSpotClient
from binance.error import ClientError
from dotenv import load_dotenv
import requests
from urllib3.exceptions import SSLError as Urllib3SSLError

try:
    from src.app_secrets import CRYPTO_SECRETS, GENERAL_SECRETS, load_secret_file
except ImportError:
    CRYPTO_SECRETS = GENERAL_SECRETS = None
    load_secret_file = lambda x: {}

logger = logging.getLogger(__name__)

# TLS certificate verification config.
# For production, consider implementing certificate pinning to prevent MITM attacks.
# See: https://requests.readthedocs.io/en/latest/user/advanced/#ssl-cert-verification
VERIFY_SSL = os.environ.get("VERIFY_SSL", "true").lower() in ("true", "1", "yes")

# Patterns to redact from error messages (API keys, secrets, etc.)
_SENSITIVE_PATTERN = re.compile(
    r"(api_key|api_secret|apiKey|apiSecret|secret|token)"
    r"\s*[=:]\s*"
    r"['\"]?([A-Za-z0-9_\-/+=]{8,})['\"]?",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Remove potential secrets from error messages before logging."""
    return _SENSITIVE_PATTERN.sub(r"\1=***REDACTED***", msg)


def _parse_retry_after(error: ClientError, default_wait: int) -> int:
    """Safely parse Retry-After header from a Binance ClientError.
    
    Handles: missing headers, None headers dict, non-numeric values.
    Caps at 60s to prevent excessive delays.
    Note: binance SDK uses error.header (singular), not error.headers.
    """
    try:
        hdr = getattr(error, 'header', None) or getattr(error, 'headers', None)
        raw_ra = hdr.get('Retry-After') if hdr else None
        wait = int(raw_ra) if raw_ra is not None else default_wait
    except (ValueError, TypeError):
        wait = default_wait
    return min(wait, 60)


class BinanceClient:
    """Binance SPOT API Client with error handling and retry logic"""
    
    def __init__(self, testnet: bool = False):
        self.testnet = testnet
        # Always load .env for non-key vars (BINANCE_BASE_URL etc.)
        # load_dotenv won't override existing env vars
        project_root = Path(__file__).parent.parent
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
        self._load_keys()
        
        # Validate API keys are present
        if not self.api_key or not self.api_key.strip():
            raise ValueError(
                "Binance API key not found. Please set BINANCE_API_KEY / BINANCE_TESTNET_API_KEY "
                "in your .env file or environment variables. "
                "See .env.example for reference."
            )
        if not self.api_secret or not self.api_secret.strip():
            raise ValueError(
                "Binance API secret not found. Please set BINANCE_API_SECRET / BINANCE_TESTNET_API_SECRET "
                "in your .env file or environment variables. "
                "See .env.example for reference."
            )
        
        if testnet:
            self.base_url = "https://testnet.binance.vision"
        else:
            self.base_url = os.environ.get("BINANCE_BASE_URL", "https://api3.binance.com")
        
        self.client = BinanceSpotClient(
            base_url=self.base_url,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        self.recv_window = int(os.environ.get('BINANCE_RECV_WINDOW', '10000'))
        # Cache for get_balance to avoid hitting the heavy account() endpoint too often
        self._balance_cache: Dict[str, tuple] = {}  # asset -> (value, timestamp)
        self._balance_cache_ttl = 30  # seconds
        # Cache for exchange_info to avoid 1-2MB fetch on every call
        self._exchange_info_cache: Optional[Dict] = None
        self._exchange_info_timestamp: float = 0.0
        self._exchange_info_ttl = 3600  # 1 hour
        logger.info(f"BinanceClient initialized (testnet={testnet})")
    
    def _load_keys(self):
        """Load API keys from environment, .env file, or centralized secrets files.
        
        Priority (highest to lowest):
        1. Environment variables (BINANCE_API_KEY, BINANCE_API_SECRET)
        2. .env file in project root
        3. Centralized secrets files (~/.config/crypto-ai-trader/*.env)
        4. testnet variants (BINANCE_TESTNET_API_KEY)
        """
        # 1. Try environment first
        self.api_key = os.environ.get("BINANCE_API_KEY") or os.environ.get("BINANCE_TESTNET_API_KEY", "")
        self.api_secret = os.environ.get("BINANCE_API_SECRET") or os.environ.get("BINANCE_TESTNET_API_SECRET", "")
        source = "environment" if self.api_key else None

        # 2. Try .env file in project root (override if env not set)
        if not self.api_key:
            # FIX A8: Support PROJECT_ROOT env override for packaged installs
            project_root = os.environ.get("PROJECT_ROOT")
            if project_root:
                project_root = Path(project_root)
            else:
                project_root = Path(__file__).parent.parent
            env_file = project_root / ".env"
            if env_file.exists():
                load_dotenv(env_file)
                self.api_key = os.environ.get("BINANCE_API_KEY", "")
                self.api_secret = os.environ.get("BINANCE_API_SECRET", "")
                source = ".env file" if self.api_key else source

        # 3. Try centralized secrets files (via src.secrets)
        if not self.api_key:
            for secrets_file in [CRYPTO_SECRETS, GENERAL_SECRETS]:
                secrets = load_secret_file(secrets_file)
                if secrets.get("BINANCE_API_KEY"):
                    self.api_key = secrets["BINANCE_API_KEY"]
                    self.api_secret = secrets.get("BINANCE_API_SECRET", "")
                    source = f"secrets file ({Path(secrets_file).name})"
                    break
        
        if source:
            logger.info(f"API keys loaded from: {source}")
        else:
            logger.warning("No API keys found — trading operations will fail")
    
    # ==================== Market Data ====================
    
    def get_symbols(self, quote: str = "USDT") -> List[str]:
        """Get all trading symbols for a quote currency"""
        try:
            exchange_info = self._get_exchange_info()
            symbols = [
                s["symbol"] for s in exchange_info["symbols"]
                if s["quoteAsset"] == quote and s["status"] == "TRADING"
            ]
            return symbols
        except Exception as e:
            logger.error(f"Failed to get symbols: {e}")
            return []
    
    def get_exchange_info(self) -> Dict:
        """Public interface: Get cached exchange_info or fetch fresh if expired.
        Matches ExchangeClient Protocol method name.
        """
        return self._get_exchange_info()

    def _get_exchange_info(self) -> Dict:
        """Get cached exchange_info or fetch fresh if expired."""
        now = time.time()
        if self._exchange_info_cache and (now - self._exchange_info_timestamp) < self._exchange_info_ttl:
            return self._exchange_info_cache
        try:
            self._exchange_info_cache = self.client.exchange_info()
            self._exchange_info_timestamp = now
            return self._exchange_info_cache
        except Exception as e:
            logger.error(f"Failed to fetch exchange_info: {e}")
            # Return stale cache if available, else empty dict
            return self._exchange_info_cache or {}
    
    def validate_symbol(self, symbol: str) -> bool:
        """Validate symbol is allowed for trading (allowlist check)"""
        allowlist = os.environ.get("ALLOWED_SYMBOLS", "").split(",")
        if allowlist and allowlist[0]:  # If allowlist is configured
            return symbol.upper() in [s.upper() for s in allowlist]
        # No allowlist configured - allow all (SPOT client handles all pairs)
        return True
    
    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        max_retries: int = 5
    ) -> List[Dict[str, Any]]:
        """Get K-lines/candlestick data with SSL retry logic
        
        Args:
            symbol: Trading pair (e.g., 'BTCUSDT')
            interval: 1m, 5m, 15m, 1h, 4h, 1d
            limit: Number of candles (max 1500)
            start_time: Optional start timestamp in milliseconds
            end_time: Optional end timestamp in milliseconds
            max_retries: Maximum retry attempts for SSL errors
        """
        for attempt in range(max_retries):
            try:
                kwargs = dict(symbol=symbol, interval=interval, limit=limit)
                if start_time is not None:
                    kwargs["startTime"] = start_time
                if end_time is not None:
                    kwargs["endTime"] = end_time
                data = self.client.klines(**kwargs)
                result = []
                for k in data:
                    result.append({
                        "open_time": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": k[6],
                        "quote_volume": float(k[7]),
                        "trades": int(k[8]),
                        "is_closed": bool(k[9])
                    })
                return result
            except ClientError as e:
                msg = _sanitize_error(str(e))
                if e.status_code in (429, 418, 400):
                    logger.warning(f"Binance API warning (klines {symbol}): [{e.status_code}] {msg}")
                else:
                    logger.error(f"Binance API error (klines {symbol}): {msg}")
                return []
            except (ssl.SSLError, Urllib3SSLError, requests.exceptions.SSLError) as e:
                wait_time = min(2 ** attempt * 0.5, 8)  # Cap at 8 seconds
                logger.warning(f"SSL error for {symbol} (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time:.1f}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                else:
                    logger.error(f"SSL error persists for {symbol} after {max_retries} attempts: {_sanitize_error(str(e))}")
                    return []
            except Exception as e:
                # Unexpected errors - try once more
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"Unexpected error for {symbol} (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"Failed to get klines for {symbol} after {max_retries} attempts: {_sanitize_error(str(e))}")
                    return []
        return []
    
    def get_24hr_stats(self, symbol: str = None, max_retries: int = 3) -> "Dict[str, Any] | List[Dict[str, Any]]":
        """Get 24hr ticker statistics with SSL retry
        
        Args:
            symbol: If provided, returns Dict for that symbol. If None, returns List[Dict] for all USDT pairs.
        """
        for attempt in range(max_retries):
            try:
                if symbol:
                    data = self.client.ticker_24hr(symbol)
                    return {
                        "symbol": data["symbol"],
                        "price_change": float(data["priceChange"]),
                        "price_change_pct": float(data["priceChangePercent"]),
                        "volume": float(data["volume"]),
                        "quote_volume": float(data["quoteVolume"]),
                        "high": float(data["highPrice"]),
                        "low": float(data["lowPrice"]),
                        "last_price": float(data["lastPrice"])
                    }
                else:
                    # Get all tickers
                    data = self.client.ticker_24hr()
                    result = []
                    for t in data:
                        if t.get("quoteAsset") == "USDT" or t.get("symbol", "").endswith("USDT"):
                            result.append({
                                "symbol": t["symbol"],
                                "price_change_pct": float(t.get("priceChangePercent", 0)),
                                "volume": float(t.get("volume", 0)),
                                "quote_volume": float(t.get("quoteVolume", 0)),
                                "last_price": float(t.get("lastPrice", 0))
                            })
                    return result
            except (ssl.SSLError, Urllib3SSLError, requests.exceptions.SSLError) as e:
                wait_time = 2 ** attempt
                logger.warning(f"SSL error getting 24hr stats (attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                else:
                    logger.error(f"SSL error persists for 24hr stats after {max_retries} attempts")
                    return {} if symbol else []
            except Exception as e:
                logger.error(f"Failed to get 24hr stats: {e}")
                return {} if symbol else []
        return {} if symbol else []
    
    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        """Get order book depth"""
        try:
            data = self.client.depth(symbol, limit=limit)
            return {
                "bids": [[float(p), float(q)] for p, q in data["bids"]],
                "asks": [[float(p), float(q)] for p, q in data["asks"]]
            }
        except Exception as e:
            logger.error(f"Failed to get order book: {e}")
            return {"bids": [], "asks": []}
    
    # ==================== Account & Positions ====================
    
    def get_account(self) -> Dict:
        """Get account information with retry"""
        for attempt in range(3):
            try:
                return self.client.account(recvWindow=self.recv_window)
            except ClientError as e:
                if e.status_code in (429, 418):
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited getting account (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to get account: {e}")
                    return {}
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    logger.warning(f"Network error getting account (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to get account after 3 attempts: {e}")
                    return {}
        return {}
    
    def get_balance(self, asset: str = "USDT") -> float:
        """Get total balance (free + locked) for a specific asset.

        Note: Calls the full account() endpoint which returns all balances.
        An in-memory cache (30s TTL) is used to avoid redundant heavy calls.
        """
        import time as _time
        now = _time.time()
        cached = self._balance_cache.get(asset)
        if cached and (now - cached[1]) < self._balance_cache_ttl:
            return cached[0]

        try:
            account = self.get_account()
            for balance in account.get("balances", []):
                if balance["asset"] == asset:
                    val = float(balance["free"]) + float(balance["locked"])
                    self._balance_cache[asset] = (val, now)
                    return val
            self._balance_cache[asset] = (0.0, now)
            return 0.0
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            return 0.0

    def get_free_balance(self, asset: str = "USDT") -> float:
        """Get free (available) balance for order sizing.

        Unlike get_balance(), this excludes locked amounts already committed to open orders.
        """
        try:
            account = self.get_account()
            for balance in account.get("balances", []):
                if balance["asset"] == asset:
                    return float(balance["free"])
            return 0.0
        except Exception as e:
            logger.error(f"Failed to get free balance: {e}")
            return 0.0
    
    def get_position(self, symbol: str) -> Dict:
        """Get current position for a symbol"""
        try:
            account = self.get_account()
            # FIX: Properly extract base asset from symbol (e.g. BTCUSDT -> BTC)
            base_asset = symbol.upper().replace("USDT", "") if symbol.upper().endswith("USDT") else symbol.upper()
            for balance in account.get("balances", []):
                if balance["asset"] == base_asset:
                    free = float(balance["free"])
                    locked = float(balance["locked"])
                    return {
                        "asset": balance["asset"],
                        "free": free,
                        "locked": locked,
                        "total": free + locked
                    }
            return {"asset": base_asset, "free": 0, "locked": 0, "total": 0}
        except Exception as e:
            logger.error(f"Failed to get position: {e}")
            return {}
    
    # ==================== Orders ====================
    
    def get_price_precision(self, symbol: str) -> int:
        """Get price decimal precision for a symbol"""
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            if sym_info:
                for f in sym_info.get('filters', []):
                    if f['filterType'] == 'PRICE_FILTER':
                        return len(f['tickSize'].rstrip('0').split('.')[-1])
        except Exception:
            logger.error("Failed to get price precision for %s from exchange info", symbol, exc_info=True)
        return 4
    
    def _get_precision_from_step(self, step_str: str) -> int:
        """Get decimal precision from a stepSize/tickSize string safely.

        Uses Decimal normalization to handle trailing zeros correctly.
        """
        from decimal import Decimal
        d = Decimal(step_str).normalize()
        s = format(d, 'f')
        if '.' in s:
            return len(s.split('.')[-1])
        return 0

    def _floor_to_step(self, value: float, step_str: str) -> float:
        """Floor a value to a given step size using Decimal for precision safety.

        Handles edge cases like stepSize='1.0' (integer lots) correctly.
        """
        from decimal import Decimal, ROUND_DOWN, InvalidOperation
        try:
            d_value = Decimal(str(value))
            d_step = Decimal(step_str)
            if d_step <= 0:
                return float(value)
            floored = (d_value // d_step) * d_step
            return float(floored)
        except (InvalidOperation, ValueError):
            return float(value)

    def place_order(
        self,
        symbol: str,
        side: str,  # BUY or SELL
        order_type: str,  # MARKET, LIMIT, STOP_LOSS, STOP_LOSS_LIMIT
        quantity: float = None,
        price: float = None,
        stop_price: float = None,
        time_in_force: str = "GTC",
        retry: int = 3
    ) -> Optional[Dict]:
        """Place an order with retry logic"""
        # Validate symbol against allowlist before executing
        if not self.validate_symbol(symbol):
            logger.error(f"Order rejected: {symbol} is not in the allowlist")
            return None

        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type
        }
        # Fetch symbol filters for proper precision
        price_decimals = 8
        qty_decimals = 4
        lot_size_filter = None
        price_filter = None
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            if sym_info:
                for f in sym_info.get('filters', []):
                    if f['filterType'] == 'PRICE_FILTER':
                        price_filter = f
                        price_decimals = self._get_precision_from_step(f['tickSize'])
                    elif f['filterType'] == 'LOT_SIZE':
                        lot_size_filter = f
                        qty_decimals = self._get_precision_from_step(f['stepSize'])
        except Exception:
            logger.error("Failed to fetch symbol filters for %s in place_order, using defaults", symbol, exc_info=True)

        if quantity is not None:
            # Floor to stepSize to avoid exceeding available balance on SELL orders.
            # Use Decimal-based flooring for precision safety.
            if lot_size_filter:
                floored = self._floor_to_step(quantity, lot_size_filter['stepSize'])
                min_qty = float(lot_size_filter['minQty'])
                max_qty = float(lot_size_filter['maxQty'])
            else:
                import math
                step = 10 ** (-qty_decimals)
                floored = math.floor(quantity / step) * step
                min_qty = 0.0
                max_qty = float('inf')

            # Reject if quantity becomes 0 after flooring
            if floored <= 0:
                logger.error(
                    f"Order rejected: quantity {quantity} floored to 0 for {symbol}"
                )
                return None
            # Validate against minQty
            if floored < min_qty:
                logger.error(
                    f"Order rejected: quantity {floored:.{qty_decimals}f} < minQty {min_qty} for {symbol}"
                )
                return None
            # Validate against maxQty
            if floored > max_qty:
                logger.error(
                    f"Order rejected: quantity {floored:.{qty_decimals}f} > maxQty {max_qty} for {symbol}"
                )
                return None

            params["quantity"] = f"{floored:.{qty_decimals}f}"

            # FIX: Enforce minNotional before sending order (avoids -1013 NOTIONAL error)
            notional = floored * (price or stop_price or 0)
            if notional > 0:
                min_notional = 0.0
                for f in sym_info.get('filters', []):
                    if f['filterType'] in ('MIN_NOTIONAL', 'NOTIONAL'):
                        min_notional = float(f.get('minNotional', 0))
                        break
                if notional < min_notional:
                    logger.error(
                        f"Order rejected: notional {notional:.2f} < minNotional {min_notional} for {symbol}"
                    )
                    return None
        if price:
            # Also floor price to tickSize for safety
            if price_filter:
                price = self._floor_to_step(price, price_filter['tickSize'])
                # FIX: Enforce minPrice / maxPrice from PRICE_FILTER
                min_price = float(price_filter.get('minPrice', 0))
                max_price = float(price_filter.get('maxPrice', float('inf')))
                if price < min_price:
                    logger.error(f"Order rejected: price {price} < minPrice {min_price} for {symbol}")
                    return None
                if price > max_price:
                    logger.error(f"Order rejected: price {price} > max_price {max_price} for {symbol}")
                    return None
            params["price"] = f"{price:.{price_decimals}f}"
        if stop_price:
            if price_filter:
                stop_price = self._floor_to_step(stop_price, price_filter['tickSize'])
            params["stopPrice"] = f"{stop_price:.{price_decimals}f}"
        if order_type in ["LIMIT", "STOP_LOSS_LIMIT"]:
            params["timeInForce"] = time_in_force

        # Generate unique client order ID for idempotency (prevents duplicate orders on retry)
        import uuid as _uuid
        _ts = str(int(time.time() * 1000))[-8:]
        _hex = _uuid.uuid4().hex[:6]
        client_order_id = f"cat_{symbol}_{side}_{_ts}_{_hex}"[:36]
        client_order_id = __import__('re').sub(r'[^A-Za-z0-9_-]', '', client_order_id)
        if not client_order_id:
            client_order_id = f"cat_{_ts}_{_hex}"
        params["newClientOrderId"] = client_order_id

        for attempt in range(retry):
            try:
                result = self.client.new_order(**params)
                logger.info(f"Order placed: {side} {symbol} {order_type} (id={client_order_id})")
                return result
            except ClientError as e:
                # Rate limit — wait and retry
                if e.status_code in (429, 418) and attempt < retry - 1:
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited on order (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                    continue
                # Business error from API - do NOT retry
                logger.error(f"Order failed (API error): {e}")
                return None
            except requests.exceptions.RequestException as e:
                # Network/timeout error - retry
                logger.warning(f"Order network error (attempt {attempt+1}/{retry}): {e}")
                if attempt < retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Order failed after {retry} attempts: {e}")
                    return None
            except Exception as e:
                # Catch-all for unexpected errors (SSL, parsing, etc.)
                logger.error(f"Order unexpected error: {e}")
                return None
        return None
    
    def place_market_buy(self, symbol: str, quantity: float) -> Optional[Dict]:
        """Place market buy order"""
        return self.place_order(symbol, "BUY", "MARKET", quantity=quantity)
    
    def place_market_sell(self, symbol: str, quantity: float) -> Optional[Dict]:
        """Place market sell order"""
        return self.place_order(symbol, "SELL", "MARKET", quantity=quantity)
    
    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Optional[Dict]:
        """Place limit buy order"""
        return self.place_order(symbol, "BUY", "LIMIT", quantity=quantity, price=price)
    
    def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Optional[Dict]:
        """Place limit sell order"""
        return self.place_order(symbol, "SELL", "LIMIT", quantity=quantity, price=price)
    
    def place_stop_loss_market(self, symbol: str, quantity: float, stop_price: float, limit_price: float = None) -> Optional[Dict]:
        """Place stop loss order as STOP_LOSS_LIMIT (SPOT-safe, Binance requires limit for spot)."""
        if limit_price is None:
            limit_price = round(stop_price * 0.995, 8)  # 0.5% slippage buffer
        return self.place_order(
            symbol, "SELL", "STOP_LOSS_LIMIT",
            quantity=quantity, price=limit_price, stop_price=stop_price
        )
    
    def place_stop_loss_limit(self, symbol: str, quantity: float, price: float, stop_price: float) -> Optional[Dict]:
        """Place stop loss limit order"""
        return self.place_order(
            symbol, "SELL", "STOP_LOSS_LIMIT",
            quantity=quantity, price=price, stop_price=stop_price
        )

    def place_oco(
        self,
        symbol: str,
        quantity: float,
        tp_price: float,
        sl_price: float,
        sl_limit_price: float = None,
    ) -> Optional[Dict]:
        """Place OCO (One-Cancels-Other) order: TP limit + SL stop-limit.

        When TP fills, SL is auto-cancelled. When SL triggers, TP is auto-cancelled.
        Perfect for replacing multiple scattered TP orders with a single atomic pair.

        Args:
            symbol: Trading pair
            quantity: Amount to sell
            tp_price: Take-profit limit price
            sl_price: Stop-loss trigger price
            sl_limit_price: Stop-loss limit price (defaults to sl_price * 0.995 for slippage buffer)

        Returns:
            Binance OCO order response or None on failure
        """
        if not self.validate_symbol(symbol):
            logger.error(f"OCO rejected: {symbol} not in allowlist")
            return None

        if sl_limit_price is None:
            sl_limit_price = round(sl_price * 0.995, 8)  # 0.5% slippage buffer

        # Get precision and filters
        price_decimals = 8
        qty_decimals = 4
        lot_size_filter = None
        price_filter = None
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            if sym_info:
                for f in sym_info.get('filters', []):
                    if f['filterType'] == 'PRICE_FILTER':
                        price_filter = f
                        price_decimals = self._get_precision_from_step(f['tickSize'])
                    elif f['filterType'] == 'LOT_SIZE':
                        lot_size_filter = f
                        qty_decimals = self._get_precision_from_step(f['stepSize'])
        except Exception:
            logger.error("Failed to fetch symbol filters for %s in place_oco, using defaults", symbol, exc_info=True)

        # Floor qty to avoid exceeding free balance (same fix as place_order)
        if lot_size_filter:
            _oco_floored = self._floor_to_step(quantity, lot_size_filter['stepSize'])
            min_qty = float(lot_size_filter['minQty'])
            max_qty = float(lot_size_filter['maxQty'])
        else:
            import math as _math
            _oco_step = 10 ** (-qty_decimals)
            _oco_floored = _math.floor(quantity / _oco_step) * _oco_step
            min_qty = 0.0
            max_qty = float('inf')

        # Validate quantity
        if _oco_floored <= 0:
            logger.error(f"OCO rejected: quantity {quantity} floored to 0 for {symbol}")
            return None
        if _oco_floored < min_qty:
            logger.error(f"OCO rejected: quantity {_oco_floored:.{qty_decimals}f} < minQty {min_qty} for {symbol}")
            return None
        if _oco_floored > max_qty:
            logger.error(f"OCO rejected: quantity {_oco_floored:.{qty_decimals}f} > maxQty {max_qty} for {symbol}")
            return None

        # Floor prices to tickSize
        if price_filter:
            tp_price = self._floor_to_step(tp_price, price_filter['tickSize'])
            sl_price = self._floor_to_step(sl_price, price_filter['tickSize'])
            sl_limit_price = self._floor_to_step(sl_limit_price, price_filter['tickSize'])

        for attempt in range(3):
            try:
                result = self.client.new_oco_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=f"{_oco_floored:.{qty_decimals}f}",
                    aboveType="LIMIT_MAKER",
                    belowType="STOP_LOSS_LIMIT",
                    abovePrice=f"{tp_price:.{price_decimals}f}",
                    belowPrice=f"{sl_limit_price:.{price_decimals}f}",
                    belowStopPrice=f"{sl_price:.{price_decimals}f}",
                    belowTimeInForce="GTC",
                    listClientOrderId=f"oco_{symbol}_{int(time.time()*1000)}"[-36:],
                )
                logger.info(
                    "OCO placed: %s qty=%s TP=%s SL=%s",
                    symbol, f"{_oco_floored:.{qty_decimals}f}",
                    f"{tp_price:.{price_decimals}f}", f"{sl_price:.{price_decimals}f}",
                )
                return result
            except ClientError as e:
                # CRITICAL FIX A4: Don't retry business errors (insufficient balance, invalid price, etc.)
                if e.status_code in (429, 418):
                    # Rate limit — retry with backoff
                    logger.warning("OCO attempt %d rate limited: %s", attempt + 1, e)
                    if attempt < 2:
                        wait = _parse_retry_after(e, 2 ** attempt)
                        time.sleep(wait)
                        continue
                elif e.status_code in (400, 401, 403):
                    # Business error — don't retry, will fail again
                    logger.error("OCO business error (no retry): %s", e)
                    return None
                else:
                    # Unknown status code — don't retry, could be a business error
                    logger.error("OCO failed with unhandled status %d (no retry): %s", e.status_code, e)
                    return None
            except requests.exceptions.RequestException as e:
                # Network error — retry
                logger.warning("OCO attempt %d network error: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
            except Exception as e:
                # Unexpected error — log and don't retry blindly
                logger.error("OCO unexpected error (no retry): %s", e)
                return None
        logger.error("OCO failed after 3 attempts")
        return None
    
    def cancel_order(self, symbol: str, order_id: int) -> Optional[Dict]:
        """Cancel an order with retry"""
        for attempt in range(3):
            try:
                return self.client.cancel_order(symbol=symbol, orderId=order_id)
            except ClientError as e:
                if e.status_code in (429, 418):
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited canceling order (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to cancel order: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    logger.warning(f"Network error canceling order (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to cancel order after 3 attempts: {e}")
                    return None
        return None
    
    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Get all open orders with retry"""
        for attempt in range(3):
            try:
                if symbol:
                    return self.client.get_open_orders(symbol=symbol, recvWindow=self.recv_window)
                return self.client.get_open_orders(recvWindow=self.recv_window)
            except ClientError as e:
                if e.status_code in (429, 418):
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited getting open orders (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to get open orders: {e}")
                    return []
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    logger.warning(f"Network error getting open orders (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to get open orders after 3 attempts: {e}")
                    return []
        return []
    
    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol with retry"""
        for attempt in range(3):
            try:
                self.client.cancel_open_orders(symbol=symbol)
                return True
            except ClientError as e:
                if e.status_code in (429, 418):
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited canceling all orders (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to cancel all orders: {e}")
                    return False
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    logger.warning(f"Network error canceling all orders (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to cancel all orders after 3 attempts: {e}")
                    return False
        return False
    
    # ==================== Utilities ====================
    
    def close(self):
        """Clean up resources (requests.Session etc.)"""
        if hasattr(self.client, '_Client__session') and self.client._Client__session:
            try:
                self.client._Client__session.close()
            except Exception:
                logger.warning("Failed to close Binance client session", exc_info=True)
    
    def get_server_time(self) -> int:
        """Get server time"""
        try:
            return self.client.time()["serverTime"]
        except Exception:
            logger.warning("get_server_time API failed, falling back to local time", exc_info=True)
            return int(time.time() * 1000)
    
    def format_price(self, symbol: str, price: float) -> str:
        """Format price - strip trailing zeros for natural precision"""
        from decimal import Decimal
        return f"{Decimal(str(price)).normalize():f}"
    
    def format_quantity(self, symbol: str, quantity: float) -> str:
        """Format quantity - strip trailing zeros for natural precision"""
        from decimal import Decimal
        return f"{Decimal(str(quantity)).normalize():f}"

    # ==================== ExchangeClient Protocol compliance ====================

    def get_quantity_precision(self, symbol: str) -> int:
        """Get quantity (lot size) decimal precision for a symbol."""
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            if sym_info:
                for f in sym_info.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_str = f['stepSize'].rstrip('0').rstrip('.')
                        return len(step_str.split('.')[-1]) if '.' in step_str else 0
        except Exception:
            logger.error("Failed to get quantity precision for %s from exchange info", symbol, exc_info=True)
        return 4  # default fallback

    def get_symbol_filters(self, symbol: str) -> Dict[str, Any]:
        """Get all relevant trading filters for a symbol."""
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next((s for s in exchange_info['symbols'] if s['symbol'] == symbol), None)
            if not sym_info:
                return {}

            filters = {}
            for f in sym_info.get('filters', []):
                if f['filterType'] == 'LOT_SIZE':
                    filters['minQty'] = float(f['minQty'])
                    filters['maxQty'] = float(f['maxQty'])
                    filters['stepSize'] = float(f['stepSize'])
                    step_str = f['stepSize'].rstrip('0').rstrip('.')
                    filters['qty_decimals'] = len(step_str.split('.')[-1]) if '.' in step_str else 0
                elif f['filterType'] == 'PRICE_FILTER':
                    filters['tickSize'] = float(f['tickSize'])
                    tick_str = f['tickSize'].rstrip('0').rstrip('.')
                    filters['price_decimals'] = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
                elif f['filterType'] == 'MIN_NOTIONAL':
                    filters['minNotional'] = float(f['minNotional'])
            return filters
        except Exception:
            logger.error("Failed to parse symbol filters for %s — returning empty dict", symbol, exc_info=True)
            return {}

    def get_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]:
        """Query a single order by ID with retry."""
        for attempt in range(3):
            try:
                return self.client.get_order(symbol=symbol, orderId=order_id)
            except ClientError as e:
                if e.status_code in (429, 418):
                    wait = _parse_retry_after(e, 2 ** (attempt + 1))
                    logger.warning(f"Rate limited getting order (attempt {attempt+1}), waiting {wait}s")
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to get order: {e}")
                    return None
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    logger.warning(f"Network error getting order (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to get order after 3 attempts: {e}")
                    return None
        return None

    def get_ticker_price(self, symbol: str) -> float:
        """Get current ticker price for a symbol."""
        try:
            resp = self.client.ticker_price(symbol=symbol)
            if isinstance(resp, dict) and 'price' in resp:
                return float(resp['price'])
            return float(resp)
        except Exception as e:
            logger.error(f"Failed to get ticker price for {symbol}: {e}")
            return 0.0

    def get_exchange_info(self) -> Dict[str, Any]:
        """Get exchange info (public, no auth required)."""
        return self._get_exchange_info()

    def get_trades(self, symbol: str, limit: int = 1000) -> List[Dict]:
        """Get recent public trades (SDK-compatible format)."""
        try:
            return self.client.trades(symbol=symbol, limit=limit)
        except Exception as e:
            logger.error(f"Failed to get trades for {symbol}: {e}")
            return []

    def get_my_trades(self, symbol: str, limit: int = 100, from_id: int = None) -> List[Dict]:
        """Get account trades (SDK-compatible format)."""
        try:
            params = {"symbol": symbol, "limit": limit}
            if from_id is not None:
                params["fromId"] = from_id
            return self.client.my_trades(**params)
        except Exception as e:
            logger.error(f"Failed to get my trades for {symbol}: {e}")
            return []
