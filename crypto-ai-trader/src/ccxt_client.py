"""
Binance SPOT API Client — ccxt-backed drop-in replacement
SPOT ONLY — no futures, no margin, no leverage

Drop-in replacement for binance_client.py using ccxt for:
  • Automatic retries with exponential backoff
  • Built-in rate-limit handling (respects Retry-After / 429 / 418)
  • Endpoint failover (ccxt rotates between api1/api2/api3…)
  • Normalised klines / ticker / order-book formats
  • Structured exception hierarchy (NetworkError / ExchangeError)
"""

import os
import math
import time
import logging
import re
import uuid
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Any

import ccxt
from dotenv import load_dotenv

try:
    from src.app_secrets import CRYPTO_SECRETS, GENERAL_SECRETS, load_secret_file
except ImportError:
    CRYPTO_SECRETS = GENERAL_SECRETS = None
    load_secret_file = lambda x: {}

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers (identical to binance_client.py for backward compat)
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERN = re.compile(
    r"(api_key|api_secret|apiKey|apiSecret|secret|token)"
    r"\s*[=:]\s*"
    r"['\"]?([A-Za-z0-9_\-/+=]{8,})['\"]?",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Remove potential secrets from error messages before logging."""
    return _SENSITIVE_PATTERN.sub(r"\1=***REDACTED***", msg)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class BinanceClient:
    """Binance SPOT API Client — ccxt-backed.

    Drop-in replacement for the python-binance based BinanceClient.
    Same public interface, same return shapes, same edge-case handling.
    """

    def __init__(self, testnet: bool = False):
        self.testnet = testnet

        # Always load .env for non-key vars (BINANCE_BASE_URL etc.)
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
            base_url = "https://testnet.binance.vision"
        else:
            base_url = os.environ.get("BINANCE_BASE_URL", "https://api3.binance.com")

        # --- Build ccxt exchange instance ---
        self.exchange = ccxt.binance({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,           # honour Binance rate limits
            "options": {
                "defaultType": "spot",         # SPOT ONLY
                "fetchMarkets": ["spot"],       # Skip futures/options markets (dapi blocked)
                "adjustForTimeDifference": True,
                "recvWindow": int(os.environ.get("BINANCE_RECV_WINDOW", "10000")),
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            },
        })

        # ccxt uses api.binance.com by default (not api3) — no URL override needed
        # For testnet, set sandbox mode
        if testnet:
            self.exchange.set_sandbox_mode(True)

        self.base_url = base_url
        self.recv_window = int(os.environ.get("BINANCE_RECV_WINDOW", "10000"))

        # Load markets (cached inside ccxt)
        try:
            self.exchange.load_markets()
        except Exception as e:
            logger.warning("Failed to load markets on init (will retry on first call): %s", e)

        # ---- caches ----
        self._balance_cache: Dict[str, tuple] = {}   # asset -> (value, timestamp)
        self._balance_cache_ttl = 30                   # seconds
        self._exchange_info_cache: Optional[Dict] = None
        self._exchange_info_timestamp: float = 0.0
        self._exchange_info_ttl = 3600                 # 1 hour

        logger.info("BinanceClient initialised (ccxt-backed, testnet=%s)", testnet)

    # ------------------------------------------------------------------ keys
    def _load_keys(self):
        """Load API keys — same priority chain as binance_client.py."""
        # 1. Environment variables
        self.api_key = os.environ.get("BINANCE_API_KEY") or os.environ.get(
            "BINANCE_TESTNET_API_KEY", ""
        )
        self.api_secret = os.environ.get("BINANCE_API_SECRET") or os.environ.get(
            "BINANCE_TESTNET_API_SECRET", ""
        )
        source = "environment" if self.api_key else None

        # 2. .env file
        if not self.api_key:
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

        # 3. Centralised secrets files
        if not self.api_key:
            for secrets_file in [CRYPTO_SECRETS, GENERAL_SECRETS]:
                secrets = load_secret_file(secrets_file)
                if secrets.get("BINANCE_API_KEY"):
                    self.api_key = secrets["BINANCE_API_KEY"]
                    self.api_secret = secrets.get("BINANCE_API_SECRET", "")
                    source = f"secrets file ({Path(secrets_file).name})"
                    break

        if source:
            logger.info("API keys loaded from: %s", source)
        else:
            logger.warning("No API keys found — trading operations will fail")

    # ========================================================= Market Data

    def get_symbols(self, quote: str = "USDT") -> List[str]:
        """Get all trading symbols for a quote currency."""
        try:
            exchange_info = self._get_exchange_info()
            symbols = [
                s["symbol"]
                for s in exchange_info["symbols"]
                if s["quoteAsset"] == quote and s["status"] == "TRADING"
            ]
            return symbols
        except Exception as e:
            logger.error("Failed to get symbols: %s", e)
            return []

    def get_exchange_info(self) -> Dict:
        """Public interface: Get cached exchange_info or fetch fresh if expired."""
        return self._get_exchange_info()

    def _get_exchange_info(self) -> Dict:
        """Get cached exchange_info or fetch fresh if expired."""
        now = time.time()
        if self._exchange_info_cache and (now - self._exchange_info_timestamp) < self._exchange_info_ttl:
            return self._exchange_info_cache
        try:
            # Use ccxt's raw request to get the exact same JSON shape as python-binance
            response = self.exchange.publicGetExchangeInfo()
            self._exchange_info_cache = response
            self._exchange_info_timestamp = now
            return self._exchange_info_cache
        except Exception as e:
            logger.error("Failed to fetch exchange_info: %s", e)
            return self._exchange_info_cache or {}

    def validate_symbol(self, symbol: str) -> bool:
        """Validate symbol is allowed for trading (allowlist check)."""
        allowlist = os.environ.get("ALLOWED_SYMBOLS", "").split(",")
        if allowlist and allowlist[0]:
            return symbol.upper() in [s.upper() for s in allowlist]
        return True

    # ------------------------------------------------------------- K-lines

    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        max_retries: int = 5,
    ) -> List[Dict[str, Any]]:
        """Get K-lines/candlestick data with retry logic.

        Returns list of dicts with keys matching the original BinanceClient:
            open_time, open, high, low, close, volume,
            close_time, quote_volume, trades, is_closed
        """
        for attempt in range(max_retries):
            try:
                params: Dict[str, Any] = {}
                if start_time is not None:
                    params["startTime"] = start_time
                if end_time is not None:
                    params["endTime"] = end_time

                # Use Binance raw API for full 12-field kline data (quote_volume, trades)
                raw = self.exchange.publicGetKlines(params={
                    "symbol": symbol.replace("/", ""),
                    "interval": interval,
                    "limit": limit,
                    **({"startTime": start_time} if start_time else {}),
                    **({"endTime": end_time} if end_time else {}),
                })

                result: List[Dict[str, Any]] = []
                for k in raw:
                    result.append({
                        "open_time": int(k[0]),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": int(k[6]),
                        "quote_volume": float(k[7]),
                        "trades": int(k[8]),
                        "is_closed": True,
                    })
                return result

            except ccxt.NetworkError as e:
                wait = min(2 ** attempt * 0.5, 8)
                logger.warning(
                    "SSL/network error for %s (attempt %d/%d): %s. Retrying in %.1fs…",
                    symbol, attempt + 1, max_retries, _sanitize_error(str(e)), wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error(
                        "Network error persists for %s after %d attempts: %s",
                        symbol, max_retries, _sanitize_error(str(e)),
                    )
                    return []

            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** attempt * 0.5, 8)
                logger.warning(
                    "Rate-limited fetching klines for %s (attempt %d/%d), waiting %.1fs",
                    symbol, attempt + 1, max_retries, wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    return []

            except ccxt.ExchangeError as e:
                logger.error("Binance API error (klines %s): %s", symbol, _sanitize_error(str(e)))
                return []

            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Unexpected error for %s (attempt %d/%d): %s. Retrying in %ds…",
                        symbol, attempt + 1, max_retries, e, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "Failed to get klines for %s after %d attempts: %s",
                        symbol, max_retries, _sanitize_error(str(e)),
                    )
                    return []
        return []

    @staticmethod
    def _close_time_from_open(open_time_ms: int, interval: str) -> int:
        """Approximate close_time from open_time + interval – 1 ms."""
        _INTERVALS_MS = {
            "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
            "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
            "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
            "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
            "1w": 604_800_000, "1M": 2_592_000_000,
        }
        ms = _INTERVALS_MS.get(interval, 3_600_000)
        return open_time_ms + ms - 1

    # ---------------------------------------------------------- 24hr stats

    def get_24hr_stats(
        self, symbol: str = None, max_retries: int = 3
    ) -> "Dict[str, Any] | List[Dict[str, Any]]":
        """Get 24hr ticker statistics with retry."""
        for attempt in range(max_retries):
            try:
                if symbol:
                    data = self.exchange.fetch_ticker(symbol)
                    return {
                        "symbol": data["symbol"],
                        "price_change": float(data.get("change", 0) or 0),
                        "price_change_pct": float(data.get("percentage", 0) or 0),
                        "volume": float(data.get("baseVolume", 0) or 0),
                        "quote_volume": float(data.get("quoteVolume", 0) or 0),
                        "high": float(data.get("high", 0) or 0),
                        "low": float(data.get("low", 0) or 0),
                        "last_price": float(data.get("last", 0) or 0),
                    }
                else:
                    tickers = self.exchange.fetch_tickers()
                    result = []
                    for sym, t in tickers.items():
                        if t.get("quoteVolume") and (
                            sym.endswith("USDT")
                        ):
                            result.append({
                                "symbol": t.get("symbol", sym),
                                "price_change_pct": float(t.get("percentage", 0) or 0),
                                "volume": float(t.get("baseVolume", 0) or 0),
                                "quote_volume": float(t.get("quoteVolume", 0) or 0),
                                "last_price": float(t.get("last", 0) or 0),
                            })
                    return result

            except (ccxt.NetworkError,) as e:
                wait = 2 ** attempt
                logger.warning(
                    "Network error getting 24hr stats (attempt %d/%d): %s. Retrying in %ds…",
                    attempt + 1, max_retries, e, wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error("Network error persists for 24hr stats after %d attempts", max_retries)
                    return {} if symbol else []

            except (ccxt.RateLimitExceeded,) as e:
                wait = min(2 ** attempt * 0.5, 8)
                logger.warning(
                    "Rate limited on 24hr stats (attempt %d/%d), waiting %.1fs",
                    attempt + 1, max_retries, wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                else:
                    return {} if symbol else []

            except Exception as e:
                logger.error("Failed to get 24hr stats: %s", e)
                return {} if symbol else []
        return {} if symbol else []

    # ----------------------------------------------------------- Order book

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        """Get order book depth."""
        try:
            data = self.exchange.fetch_order_book(symbol=symbol, limit=limit)
            return {
                "bids": [[float(p), float(q)] for p, q in (data.get("bids") or [])],
                "asks": [[float(p), float(q)] for p, q in (data.get("asks") or [])],
            }
        except Exception as e:
            logger.error("Failed to get order book: %s", e)
            return {"bids": [], "asks": []}

    # ================================================== Account & Positions

    def get_account(self) -> Dict:
        """Get account information with retry."""
        for attempt in range(3):
            try:
                # ccxt's fetch_balance returns a unified structure; we need
                # the raw Binance response for backward compat.
                raw = self.exchange.private_get_account()
                return raw
            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** attempt * 0.5, 60)
                logger.warning(
                    "Rate limited getting account (attempt %d), waiting %.1fs",
                    attempt + 1, wait,
                )
                time.sleep(wait)
            except ccxt.NetworkError as e:
                if attempt < 2:
                    logger.warning("Network error getting account (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to get account after 3 attempts: %s", e)
                    return {}
            except Exception as e:
                logger.error("Failed to get account: %s", e)
                return {}
        return {}

    def get_balance(self, asset: str = "USDT") -> float:
        """Get total balance (free + locked) for a specific asset.

        Uses an in-memory 30s cache to avoid hitting the heavy account()
        endpoint too often.
        """
        now = time.time()
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
            logger.error("Failed to get balance: %s", e)
            return 0.0

    def get_free_balance(self, asset: str = "USDT") -> float:
        """Get free (available) balance for order sizing."""
        try:
            account = self.get_account()
            for balance in account.get("balances", []):
                if balance["asset"] == asset:
                    return float(balance["free"])
            return 0.0
        except Exception as e:
            logger.error("Failed to get free balance: %s", e)
            return 0.0

    def get_position(self, symbol: str) -> Dict:
        """Get current position for a symbol."""
        try:
            account = self.get_account()
            base_asset = (
                symbol.upper().replace("USDT", "")
                if symbol.upper().endswith("USDT")
                else symbol.upper()
            )
            for balance in account.get("balances", []):
                if balance["asset"] == base_asset:
                    free = float(balance["free"])
                    locked = float(balance["locked"])
                    return {
                        "asset": balance["asset"],
                        "free": free,
                        "locked": locked,
                        "total": free + locked,
                    }
            return {"asset": base_asset, "free": 0, "locked": 0, "total": 0}
        except Exception as e:
            logger.error("Failed to get position: %s", e)
            return {}

    # =========================================================== Precision

    def get_price_precision(self, symbol: str) -> int:
        """Get price decimal precision for a symbol."""
        try:
            # Prefer ccxt market info if available
            if symbol in self.exchange.markets:
                market = self.exchange.markets[symbol]
                prec = market.get("price", {}).get("precision")
                if prec is not None:
                    return prec
            # Fallback: exchange_info
            exchange_info = self._get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol),
                None,
            )
            if sym_info:
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        return len(f["tickSize"].rstrip("0").split(".")[-1])
        except Exception:
            logger.error(
                "Failed to get price precision for %s", symbol, exc_info=True
            )
        return 4

    def get_quantity_precision(self, symbol: str) -> int:
        """Get quantity (lot size) decimal precision for a symbol."""
        try:
            if symbol in self.exchange.markets:
                market = self.exchange.markets[symbol]
                prec = market.get("amount", {}).get("precision")
                if prec is not None:
                    return prec
            exchange_info = self._get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol),
                None,
            )
            if sym_info:
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        step_str = f["stepSize"].rstrip("0").rstrip(".")
                        return (
                            len(step_str.split(".")[-1]) if "." in step_str else 0
                        )
        except Exception:
            logger.error(
                "Failed to get quantity precision for %s", symbol, exc_info=True
            )
        return 4  # default fallback

    def get_symbol_filters(self, symbol: str) -> Dict[str, Any]:
        """Get all relevant trading filters for a symbol."""
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol),
                None,
            )
            if not sym_info:
                return {}

            filters: Dict[str, Any] = {}
            for f in sym_info.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    filters["minQty"] = float(f["minQty"])
                    filters["maxQty"] = float(f["maxQty"])
                    filters["stepSize"] = float(f["stepSize"])
                    step_str = f["stepSize"].rstrip("0").rstrip(".")
                    filters["qty_decimals"] = (
                        len(step_str.split(".")[-1]) if "." in step_str else 0
                    )
                elif f["filterType"] == "PRICE_FILTER":
                    filters["tickSize"] = float(f["tickSize"])
                    tick_str = f["tickSize"].rstrip("0").rstrip(".")
                    filters["price_decimals"] = (
                        len(tick_str.split(".")[-1]) if "." in tick_str else 0
                    )
                elif f["filterType"] == "MIN_NOTIONAL":
                    filters["minNotional"] = float(f["minNotional"])
            return filters
        except Exception:
            logger.error("get_symbol_filters failed, returning empty filters dict", exc_info=True)
            return {}

    # --------------------------------------------------- Decimal utilities

    @staticmethod
    def _get_precision_from_step(step_str: str) -> int:
        """Get decimal precision from a stepSize/tickSize string safely."""
        d = Decimal(step_str).normalize()
        s = format(d, "f")
        if "." in s:
            return len(s.split(".")[-1])
        return 0

    @staticmethod
    def _floor_to_step(value: float, step_str: str) -> float:
        """Floor a value to a given step size using Decimal."""
        try:
            d_value = Decimal(str(value))
            d_step = Decimal(step_str)
            if d_step <= 0:
                return float(value)
            floored = (d_value // d_step) * d_step
            return float(floored)
        except (InvalidOperation, ValueError):
            return float(value)

    # ============================================================= Orders

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
        """Place an order with retry logic."""
        # Validate symbol against allowlist
        if not self.validate_symbol(symbol):
            logger.error("Order rejected: %s is not in the allowlist", symbol)
            return None

        # --- Fetch symbol filters for proper precision ---
        price_decimals = 8
        qty_decimals = 4
        lot_size_filter = None
        price_filter = None
        sym_info = None
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol),
                None,
            )
            if sym_info:
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        price_filter = f
                        price_decimals = self._get_precision_from_step(f["tickSize"])
                    elif f["filterType"] == "LOT_SIZE":
                        lot_size_filter = f
                        qty_decimals = self._get_precision_from_step(f["stepSize"])
        except Exception:
            logger.error(
                "Failed to parse exchange filters for %s in place_order",
                symbol,
                exc_info=True,
            )

        # --- Quantity validation & formatting ---
        qty_str: Optional[str] = None
        if quantity is not None:
            if lot_size_filter:
                floored = self._floor_to_step(quantity, lot_size_filter["stepSize"])
                min_qty = float(lot_size_filter["minQty"])
                max_qty = float(lot_size_filter["maxQty"])
            else:
                step = 10 ** (-qty_decimals)
                floored = math.floor(quantity / step) * step
                min_qty = 0.0
                max_qty = float("inf")

            if floored <= 0:
                logger.error(
                    "Order rejected: quantity %s floored to 0 for %s",
                    quantity, symbol,
                )
                return None
            if floored < min_qty:
                logger.error(
                    "Order rejected: quantity %s < minQty %s for %s",
                    f"{floored:.{qty_decimals}f}", min_qty, symbol,
                )
                return None
            if floored > max_qty:
                logger.error(
                    "Order rejected: quantity %s > maxQty %s for %s",
                    f"{floored:.{qty_decimals}f}", max_qty, symbol,
                )
                return None
            qty_str = f"{floored:.{qty_decimals}f}"

            # Enforce minNotional
            notional = floored * (price or stop_price or 0)
            if notional > 0 and sym_info:
                min_notional = 0.0
                for f in sym_info.get("filters", []):
                    if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                        min_notional = float(f.get("minNotional", 0))
                        break
                if notional < min_notional:
                    logger.error(
                        "Order rejected: notional %.2f < minNotional %s for %s",
                        notional, min_notional, symbol,
                    )
                    return None

        # --- Price validation & formatting ---
        price_val: Optional[str] = None
        if price is not None:
            if price_filter:
                price = self._floor_to_step(price, price_filter["tickSize"])
                min_price = float(price_filter.get("minPrice", 0))
                max_price = float(price_filter.get("maxPrice", float("inf")))
                if price < min_price:
                    logger.error(
                        "Order rejected: price %s < minPrice %s for %s",
                        price, min_price, symbol,
                    )
                    return None
                if price > max_price:
                    logger.error(
                        "Order rejected: price %s > max_price %s for %s",
                        price, max_price, symbol,
                    )
                    return None
            price_val = f"{price:.{price_decimals}f}"

        # --- Stop price ---
        stop_price_val: Optional[str] = None
        if stop_price is not None:
            if price_filter:
                stop_price = self._floor_to_step(stop_price, price_filter["tickSize"])
            stop_price_val = f"{stop_price:.{price_decimals}f}"

        # --- Map to ccxt order type ---
        # ccxt maps: MARKET -> market, LIMIT -> limit
        # Binance-specific: STOP_LOSS -> stop_loss, STOP_LOSS_LIMIT -> stop_loss_limit
        ccxt_type = order_type.lower()
        # ccxt recognises "limit", "market" natively.  For STOP_LOSS_LIMIT we
        # pass the Binance-specific type via params.
        params: Dict[str, Any] = {}
        if ccxt_type in ("stop_loss", "stop_loss_limit"):
            params["type"] = order_type  # Binance-native type

        # timeInForce for limit orders
        if order_type in ("LIMIT", "STOP_LOSS_LIMIT"):
            params["timeInForce"] = time_in_force

        # Binance uses lowercase "buy"/"sell" in ccxt
        ccxt_side = side.lower()

        # Generate unique client order ID for idempotency
        # Binance allows: [A-Za-z0-9_-] (max 36 chars)
        # Use short format: cat_<base><quote>_<side>_<ts>_<hex6>
        _ts = str(int(time.time() * 1000))[-8:]  # last 8 digits of ms timestamp
        _hex = uuid.uuid4().hex[:6]
        client_order_id = f"cat_{symbol}_{side}_{_ts}_{_hex}"[:36]
        # Sanitize: remove any non-alphanumeric/underscore/hyphen chars
        client_order_id = re.sub(r'[^A-Za-z0-9_-]', '', client_order_id)
        if not client_order_id:
            client_order_id = f"cat_{_ts}_{_hex}"
        params["newClientOrderId"] = client_order_id

        # Normalize symbol to ccxt unified format (CFXUSDT -> CFX/USDT)
        ccxt_symbol = symbol
        if "/" not in symbol and symbol.endswith("USDT"):
            ccxt_symbol = f"{symbol[:-4]}/USDT"

        for attempt in range(retry):
            try:
                if order_type == "MARKET":
                    result = self.exchange.create_order(
                        symbol=ccxt_symbol,
                        type="market",
                        side=ccxt_side,
                        amount=float(qty_str) if qty_str else None,
                        params=params,
                    )
                elif order_type == "LIMIT":
                    result = self.exchange.create_order(
                        symbol=ccxt_symbol,
                        type="limit",
                        side=ccxt_side,
                        amount=float(qty_str) if qty_str else None,
                        price=float(price_val) if price_val else None,
                        params=params,
                    )
                else:
                    # STOP_LOSS / STOP_LOSS_LIMIT — use Binance-native type
                    result = self.exchange.create_order(
                        symbol=ccxt_symbol,
                        type=ccxt_type,
                        side=ccxt_side,
                        amount=float(qty_str) if qty_str else None,
                        price=float(price_val) if price_val else None,
                        params={
                            **params,
                            **({"stopPrice": float(stop_price_val)} if stop_price_val else {}),
                        },
                    )
                logger.info(
                    "Order placed: %s %s %s (id=%s)", side, symbol, order_type, client_order_id,
                )
                return result

            except ccxt.RateLimitExceeded as e:
                if attempt < retry - 1:
                    wait = min(2 ** (attempt + 1), 60)
                    logger.warning(
                        "Rate limited on order (attempt %d), waiting %ds", attempt + 1, wait,
                    )
                    time.sleep(wait)
                    continue
                logger.error("Order failed (rate limit exhausted): %s", _sanitize_error(str(e)))
                return None

            except ccxt.NetworkError as e:
                logger.warning(
                    "Order network error (attempt %d/%d): %s", attempt + 1, retry, e,
                )
                if attempt < retry - 1:
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Order failed after %d attempts: %s", retry, e)
                    return None

            except ccxt.InsufficientFunds as e:
                logger.error("Order rejected (insufficient funds): %s", _sanitize_error(str(e)))
                return None

            except ccxt.InvalidOrder as e:
                logger.error("Order rejected (invalid): %s", _sanitize_error(str(e)))
                return None

            except ccxt.ExchangeError as e:
                # Business error — do NOT retry
                logger.error("Order failed (API error): %s", _sanitize_error(str(e)))
                return None

            except Exception as e:
                logger.error("Order unexpected error: %s", e)
                return None
        return None

    # ---- Convenience wrappers ----

    def place_market_buy(self, symbol: str, quantity: float) -> Optional[Dict]:
        return self.place_order(symbol, "BUY", "MARKET", quantity=quantity)

    def place_market_sell(self, symbol: str, quantity: float) -> Optional[Dict]:
        return self.place_order(symbol, "SELL", "MARKET", quantity=quantity)

    def place_limit_buy(
        self, symbol: str, quantity: float, price: float
    ) -> Optional[Dict]:
        return self.place_order(symbol, "BUY", "LIMIT", quantity=quantity, price=price)

    def place_limit_sell(
        self, symbol: str, quantity: float, price: float
    ) -> Optional[Dict]:
        return self.place_order(symbol, "SELL", "LIMIT", quantity=quantity, price=price)

    def place_stop_loss_market(
        self,
        symbol: str,
        quantity: float,
        stop_price: float,
        limit_price: float = None,
    ) -> Optional[Dict]:
        """Place stop loss as STOP_LOSS_LIMIT (Binance spot requires limit)."""
        if limit_price is None:
            limit_price = round(stop_price * 0.995, 8)  # 0.5% slippage buffer
        return self.place_order(
            symbol,
            "SELL",
            "STOP_LOSS_LIMIT",
            quantity=quantity,
            price=limit_price,
            stop_price=stop_price,
        )

    def place_stop_loss_limit(
        self,
        symbol: str,
        quantity: float,
        price: float,
        stop_price: float,
    ) -> Optional[Dict]:
        """Place stop loss limit order."""
        return self.place_order(
            symbol,
            "SELL",
            "STOP_LOSS_LIMIT",
            quantity=quantity,
            price=price,
            stop_price=stop_price,
        )

    # ----------------------------------------------------------------- OCO

    def place_oco(
        self,
        symbol: str,
        quantity: float,
        tp_price: float,
        sl_price: float,
        sl_limit_price: float = None,
    ) -> Optional[Dict]:
        """Place OCO (One-Cancels-Other) order: TP limit + SL stop-limit."""
        if not self.validate_symbol(symbol):
            logger.error("OCO rejected: %s not in allowlist", symbol)
            return None

        if sl_limit_price is None:
            sl_limit_price = round(sl_price * 0.995, 8)

        # --- Fetch symbol filters ---
        price_decimals = 8
        qty_decimals = 4
        lot_size_filter = None
        price_filter = None
        try:
            exchange_info = self._get_exchange_info()
            sym_info = next(
                (s for s in exchange_info["symbols"] if s["symbol"] == symbol),
                None,
            )
            if sym_info:
                for f in sym_info.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        price_filter = f
                        price_decimals = self._get_precision_from_step(f["tickSize"])
                    elif f["filterType"] == "LOT_SIZE":
                        lot_size_filter = f
                        qty_decimals = self._get_precision_from_step(f["stepSize"])
        except Exception:
            logger.error(
                "Failed to parse exchange filters for %s in place_oco",
                symbol,
                exc_info=True,
            )

        # --- Quantity validation ---
        if lot_size_filter:
            oco_floored = self._floor_to_step(quantity, lot_size_filter["stepSize"])
            min_qty = float(lot_size_filter["minQty"])
            max_qty = float(lot_size_filter["maxQty"])
        else:
            oco_step = 10 ** (-qty_decimals)
            oco_floored = math.floor(quantity / oco_step) * oco_step
            min_qty = 0.0
            max_qty = float("inf")

        if oco_floored <= 0:
            logger.error("OCO rejected: quantity %s floored to 0 for %s", quantity, symbol)
            return None
        if oco_floored < min_qty:
            logger.error(
                "OCO rejected: quantity %s < minQty %s for %s",
                f"{oco_floored:.{qty_decimals}f}", min_qty, symbol,
            )
            return None
        if oco_floored > max_qty:
            logger.error(
                "OCO rejected: quantity %s > maxQty %s for %s",
                f"{oco_floored:.{qty_decimals}f}", max_qty, symbol,
            )
            return None

        # Floor prices
        if price_filter:
            tp_price = self._floor_to_step(tp_price, price_filter["tickSize"])
            sl_price = self._floor_to_step(sl_price, price_filter["tickSize"])
            sl_limit_price = self._floor_to_step(sl_limit_price, price_filter["tickSize"])

        oco_params = {
            "symbol": symbol,
            "side": "sell",
            "quantity": float(f"{oco_floored:.{qty_decimals}f}"),
            "price": float(f"{tp_price:.{price_decimals}f}"),
            "stopPrice": float(f"{sl_price:.{price_decimals}f}"),
            "stopLimitPrice": float(f"{sl_limit_price:.{price_decimals}f}"),
            "stopLimitTimeInForce": "GTC",
        }

        for attempt in range(3):
            try:
                result = self.exchange.private_post_order_oco(oco_params)
                logger.info(
                    "OCO placed: %s qty=%s TP=%s SL=%s",
                    symbol,
                    f"{oco_floored:.{qty_decimals}f}",
                    f"{tp_price:.{price_decimals}f}",
                    f"{sl_price:.{price_decimals}f}",
                )
                return result

            except ccxt.RateLimitExceeded as e:
                logger.warning("OCO attempt %d rate limited: %s", attempt + 1, e)
                if attempt < 2:
                    wait = min(2 ** attempt, 60)
                    time.sleep(wait)
                    continue

            except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as e:
                logger.error("OCO business error (no retry): %s", _sanitize_error(str(e)))
                return None

            except ccxt.ExchangeError as e:
                logger.error("OCO business error (no retry): %s", _sanitize_error(str(e)))
                return None

            except ccxt.NetworkError as e:
                logger.warning("OCO attempt %d network error: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue

            except Exception as e:
                logger.error("OCO unexpected error (no retry): %s", e)
                return None

        logger.error("OCO failed after 3 attempts")
        return None

    # -------------------------------------------------------- Cancel / Query

    def cancel_order(self, symbol: str, order_id: int) -> Optional[Dict]:
        """Cancel an order with retry."""
        for attempt in range(3):
            try:
                return self.exchange.cancel_order(str(order_id), symbol)
            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** (attempt + 1), 60)
                logger.warning(
                    "Rate limited cancelling order (attempt %d), waiting %ds",
                    attempt + 1, wait,
                )
                time.sleep(wait)
            except ccxt.NetworkError as e:
                if attempt < 2:
                    logger.warning("Network error cancelling order (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to cancel order after 3 attempts: %s", e)
                    return None
            except Exception as e:
                logger.error("Failed to cancel order: %s", e)
                return None
        return None

    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Get all open orders with retry."""
        for attempt in range(3):
            try:
                if symbol:
                    return self.exchange.fetch_open_orders(symbol)
                # Without symbol — return all open orders
                return self.exchange.fetch_open_orders()
            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** (attempt + 1), 60)
                logger.warning(
                    "Rate limited getting open orders (attempt %d), waiting %ds",
                    attempt + 1, wait,
                )
                time.sleep(wait)
            except ccxt.NetworkError as e:
                if attempt < 2:
                    logger.warning("Network error getting open orders (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to get open orders after 3 attempts: %s", e)
                    return []
            except Exception as e:
                logger.error("Failed to get open orders: %s", e)
                return []
        return []

    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol with retry."""
        for attempt in range(3):
            try:
                self.exchange.private_delete_open_orders({"symbol": symbol})
                return True
            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** (attempt + 1), 60)
                logger.warning(
                    "Rate limited cancelling all orders (attempt %d), waiting %ds",
                    attempt + 1, wait,
                )
                time.sleep(wait)
            except ccxt.NetworkError as e:
                if attempt < 2:
                    logger.warning("Network error cancelling all orders (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to cancel all orders after 3 attempts: %s", e)
                    return False
            except Exception as e:
                logger.error("Failed to cancel all orders: %s", e)
                return False
        return False

    def get_order(self, symbol: str, order_id: int) -> Optional[Dict[str, Any]]:
        """Query a single order by ID with retry."""
        for attempt in range(3):
            try:
                return self.exchange.fetch_order(str(order_id), symbol)
            except ccxt.RateLimitExceeded as e:
                wait = min(2 ** (attempt + 1), 60)
                logger.warning(
                    "Rate limited getting order (attempt %d), waiting %ds",
                    attempt + 1, wait,
                )
                time.sleep(wait)
            except ccxt.NetworkError as e:
                if attempt < 2:
                    logger.warning("Network error getting order (attempt %d): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)
                else:
                    logger.error("Failed to get order after 3 attempts: %s", e)
                    return None
            except Exception as e:
                logger.error("Failed to get order: %s", e)
                return None
        return None

    # ------------------------------------------------------ Trades (public)

    def get_trades(self, symbol: str, limit: int = 1000) -> List[Dict]:
        """Get recent public trades. SDK-compatible format (price, qty, time, id, isBuyerMaker)."""
        try:
            raw = self.exchange.fetch_trades(symbol, limit=limit)
            return [{
                "id": t.get("id", ""),
                "price": t.get("price", ""),
                "qty": t.get("amount", ""),
                "time": t.get("timestamp", 0),
                "isBuyerMaker": t.get("side", "") == "sell",
            } for t in raw]
        except Exception as e:
            logger.error("Failed to get trades for %s: %s", symbol, e)
            return []

    # ------------------------------------------------------ Trades (private)

    def get_my_trades(self, symbol: str, limit: int = 100, from_id: int = None) -> List[Dict]:
        """Get account trades. SDK-compatible format (id, price, qty, time, isBuyer, commission, commissionAsset, orderId)."""
        try:
            params = {}
            if from_id is not None:
                params["fromId"] = from_id
            raw = self.exchange.fetch_my_trades(symbol, limit=limit, params=params)
            result = []
            for t in raw:
                info = t.get("info", {})
                result.append({
                    "id": int(info.get("id", 0)),
                    "price": info.get("price", ""),
                    "qty": info.get("qty", ""),
                    "quoteQty": info.get("quoteQty", ""),
                    "commission": info.get("commission", ""),
                    "commissionAsset": info.get("commissionAsset", ""),
                    "time": int(info.get("time", 0)),
                    "isBuyer": info.get("isBuyer", False),
                    "isMaker": info.get("isMaker", False),
                    "orderId": int(info.get("orderId", 0)),
                    "symbol": info.get("symbol", symbol),
                })
            return result
        except Exception as e:
            logger.error("Failed to get my trades for %s: %s", symbol, e)
            return []

    # ----------------------------------------------------------- Ticker

    def get_ticker_price(self, symbol: str) -> float:
        """Get current ticker price for a symbol."""
        try:
            data = self.exchange.fetch_ticker(symbol)
            return float(data.get("last", 0) or 0)
        except Exception as e:
            logger.error("Failed to get ticker price for %s: %s", symbol, e)
            return 0.0

    # -------------------------------------------------------- Server time

    def get_server_time(self) -> int:
        """Get server time."""
        try:
            raw = self.exchange.time()
            if isinstance(raw, dict):
                return int(raw.get("serverTime", time.time() * 1000))
            return int(raw)
        except Exception:
            logger.warning("get_server_time failed, falling back to local time", exc_info=True)
            return int(time.time() * 1000)

    # --------------------------------------------------------- Formatting

    def format_price(self, symbol: str, price: float) -> str:
        """Format price — strip trailing zeros for natural precision."""
        return f"{Decimal(str(price)).normalize():f}"

    def format_quantity(self, symbol: str, quantity: float) -> str:
        """Format quantity — strip trailing zeros for natural precision."""
        return f"{Decimal(str(quantity)).normalize():f}"

    # ----------------------------------------------------------- Dust

    def transfer_dust(self, asset: list) -> dict:
        """Convert dust assets to BNB.
        Binance endpoint: POST /sapi/v1/asset/dust
        Rate limit: 1 request/hour.
        
        Args:
            asset: List of asset symbols to convert (e.g. ['ADA', 'XRP'])
        Returns:
            Raw Binance response dict with keys: totalServiceCharge, totalTransfered, transferResult
        """
        try:
            return self.exchange.sapiPostAssetDust({"asset": asset})
        except Exception as e:
            logger.error("Dust transfer failed: %s", e)
            raise

    def bnb_convertible_assets(self) -> dict:
        """Get assets that can be converted to BNB via dust transfer.
        Binance endpoint: POST /sapi/v1/asset/dust-btc
        """
        try:
            return self.exchange.sapiPostAssetDustBtc()
        except Exception as e:
            logger.error("Failed to get BNB convertible assets: %s", e)
            raise

    def list_all_convert_pairs(self, fromAsset: str, toAsset: str) -> list:
        """List available convert pairs (Binance Convert, not dust).
        Binance endpoint: GET /sapi/v1/convert/exchangeInfo
        """
        try:
            params = {"fromAsset": fromAsset, "toAsset": toAsset}
            return self.exchange.sapiGetConvertExchangeInfo(params)
        except Exception as e:
            logger.error("Failed to list convert pairs: %s", e)
            return []

    def convert_get_quote(self, fromAsset: str, toAsset: str, fromAmount: str) -> dict:
        """Request a convert quote.
        Binance endpoint: POST /sapi/v1/convert/getQuote
        """
        try:
            params = {"fromAsset": fromAsset, "toAsset": toAsset, "fromAmount": fromAmount}
            return self.exchange.sapiPostConvertGetQuote(params)
        except Exception as e:
            logger.error("Failed to get convert quote: %s", e)
            raise

    def convert_accept_quote(self, quoteId: str) -> dict:
        """Accept a convert quote.
        Binance endpoint: POST /sapi/v1/convert/acceptQuote
        """
        try:
            return self.exchange.sapiPostConvertAcceptQuote({"quoteId": quoteId})
        except Exception as e:
            logger.error("Failed to accept convert quote: %s", e)
            raise

    # ----------------------------------------------------------- Cleanup

    def close(self):
        """Clean up resources."""
        try:
            if hasattr(self.exchange, "session") and self.exchange.session:
                self.exchange.session.close()
        except Exception:
            logger.error("Failed to close ccxt HTTP session", exc_info=True)
