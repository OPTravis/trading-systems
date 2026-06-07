"""
Risk Manager Module - Comprehensive risk management for crypto trading.
Includes BTC trend filter, trailing stop, consecutive loss guard, and sector exposure limits.
All state is persisted to local JSON files in <project_root>/data/.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from ..core.exchange_client import ExchangeClient
from typing import Any, Dict, List, Optional, Set

from ..utils.indicators import Indicators

from .drawdown_breaker import DrawdownBreaker
from .correlation_risk import CorrelationRiskManager

logger = logging.getLogger(__name__)

# Base directory for persisted state
from ..utils.project_root import get_project_root

_DATA_DIR = get_project_root() / "data"


def _ensure_data_dir() -> Path:
    """Ensure the data directory exists."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def _load_json(filepath: Path, default: Optional[Any] = None) -> Any:
    """Load JSON from file, returning default on any error."""
    try:
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {filepath}: {e}")
    return default if default is not None else {}


def _save_json(filepath: Path, data: Any) -> bool:
    """Save data to JSON file atomically (write-then-rename)."""
    try:
        _ensure_data_dir()
        tmp_path = filepath.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_path.replace(filepath)
        return True
    except Exception as e:
        logger.error(f"Failed to save {filepath}: {e}")
        return False


# ---------------------------------------------------------------------------
# 1. TrendFilter – BTC 趨勢過濾器
# ---------------------------------------------------------------------------

class TrendFilter:
    """Use BTC daily chart to determine overall market trend bias."""

    BEARISH = "BEARISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"

    def __init__(self):
        self._cache: Dict = {}
        self._cache_ts: float = 0
        self._cache_ttl: int = 300  # 5 minutes

    def check_trend(self, binance_client: 'ExchangeClient') -> Dict:
        """Analyze BTC trend using multi-factor scoring.

        Uses Indicators.btc_trend_score() for composite 0-100 scoring.
        Legacy fields (sma_200, sma_50, adx) are kept for backward compatibility.

        Returns dict with:
            trend: BULLISH / BEARISH / NEUTRAL (score-based)
            allow_long: bool (False when BEARISH)
            score: float (0-100)
            factors: {ema_cross, rsi, macd, price_structure, volume}
            btc_close, sma_200, sma_50, adx
            size_multiplier: float (0.5 when weak, 1.0 when strong)
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            logger.debug("TrendFilter: returning cached result")
            return self._cache

        try:
            klines = binance_client.get_klines("BTCUSDT", "1d", 250)
            if not klines or len(klines) < 60:
                logger.warning("TrendFilter: insufficient kline data (%d) — fail-safe: NEUTRAL",
                               len(klines) if klines else 0)
                self._cache = {
                    "trend": self.NEUTRAL, "allow_long": False,
                    "score": 50, "factors": {},
                    "btc_close": 0, "sma_200": 0, "sma_50": 0,
                    "adx": 0, "size_multiplier": 0.5,
                }
                self._cache_ts = now
                return self._cache

            # --- Multi-factor scoring ---
            from ..utils.indicators import Indicators
            trend_result = Indicators.btc_trend_score(klines)

            closes = [k["close"] for k in klines]
            adx_value = Indicators.adx(klines, 14)

            # ADX filter: weak trend → reduce position size
            size_multiplier = 0.5 if adx_value < 20 else 1.0

            # Convert to legacy format
            if trend_result["trend"] == "BEARISH":
                trend = self.BEARISH
            elif trend_result["trend"] == "BULLISH":
                trend = self.BULLISH
            else:
                trend = self.NEUTRAL

            result = {
                "trend": trend,
                "allow_long": trend_result["allow_long"],
                "score": trend_result["score"],
                "factors": trend_result["factors"],
                "btc_close": trend_result["btc_close"],
                "sma_200": trend_result["sma_200"],
                "sma_50": trend_result["sma_50"],
                "ema_21": trend_result["ema_21"],
                "ema_55": trend_result["ema_55"],
                "rsi_14": trend_result["rsi_14"],
                "macd_hist": trend_result["macd_hist"],
                "adx": round(adx_value, 2),
                "size_multiplier": size_multiplier,
            }

            self._cache = result
            self._cache_ts = now

            logger.info(
                "TrendFilter: trend=%s score=%.1f allow_long=%s BTC=%.2f "
                "EMA21=%.0f EMA55=%.0f RSI=%.1f MACD_H=%.4f ADX=%.2f | "
                "factors: EMA=%.1f RSI=%.1f MACD=%.1f Struct=%.1f Vol=%.1f",
                trend, trend_result["score"], trend_result["allow_long"],
                trend_result["btc_close"],
                trend_result["ema_21"], trend_result["ema_55"],
                trend_result["rsi_14"], trend_result["macd_hist"],
                adx_value,
                trend_result["factors"]["ema_cross"],
                trend_result["factors"]["rsi"],
                trend_result["factors"]["macd"],
                trend_result["factors"]["price_structure"],
                trend_result["factors"]["volume"],
            )
            return result

        except Exception as e:
            logger.error(f"TrendFilter.check_trend failed: {e} — fail-safe: blocking longs")
            return {
                "trend": self.NEUTRAL, "allow_long": False,
                "score": 50, "factors": {},
                "btc_close": 0, "sma_200": 0, "sma_50": 0,
                "adx": 0, "size_multiplier": 0.5,
            }


# ---------------------------------------------------------------------------
# 2. TrailingStop – 追蹤止損
# ---------------------------------------------------------------------------

class TrailingStop:
    """Manage trailing stop-loss for open positions.

    PRIMARY STORAGE: SQLite state.db (via StateDB)
    Format: {symbol: {entry_price, highest_price, sl_price, activated, atr}}
    """

    ACTIVATION_ATR_MULT = 1.0    # activate trailing when profit >= 1.0 * ATR (was 2.0, too conservative)
    TRAILING_ATR_MULT = 1.0      # stop distance from high = 1.0 * ATR (was 1.5, too loose)

    def __init__(self):
        self._filepath = _DATA_DIR / "trailing_stops.json"
        self._last_save_ts = 0.0
        self._save_debounce = 2.0  # seconds — don't write SQLite more than once per 2s
        # Load from SQLite first, fallback to JSON
        self._state: Dict[str, Dict] = self._load_from_db()
        if not self._state:
            self._state = _load_json(self._filepath, default={})

    def _load_from_db(self) -> Dict[str, Dict]:
        """Load trailing stops from SQLite (primary)."""
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            rows = db.ts_get_all()
            if rows:
                logger.info(f"TrailingStop: loaded {len(rows)} entries from StateDB")
                return rows
        except Exception as e:
            logger.warning(f"TrailingStop: failed to load from StateDB: {e}")
        return {}

    def _save(self, force: bool = False) -> bool:
        """Persist to SQLite with debounce (2s min interval)."""
        now = time.time()
        if not force and (now - self._last_save_ts) < self._save_debounce:
            return True  # skip — saved recently
        self._last_save_ts = now
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            for sym, data in self._state.items():
                db.ts_set(sym, data)
            # Remove deleted entries from DB
            db_rows = db.ts_get_all()
            for sym in db_rows:
                if sym not in self._state:
                    db.ts_remove(sym)
            return True
        except Exception as e:
            logger.error(f"TrailingStop: SQLite save failed: {e}")
            return False

    def update(self, symbol: str, current_price: float, atr: float, entry_price: Optional[float] = None) -> Dict:
        """Update trailing stop for a symbol.

        Returns:
            {activated: False}                  – not yet profitable enough
            {activated: True, sl_price, ...}    – trailing active, no trigger
            {triggered: True}                   – price hit stop, should close
        """
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        price = float(current_price)
        atr = float(atr)

        if atr <= 0:
            # Use last known ATR as fallback instead of skipping entirely
            last_atr = self._state.get(symbol, {}).get("atr", 0) if symbol in self._state else 0
            if last_atr > 0:
                logger.warning("TrailingStop: ATR=%.4f for %s — using last valid ATR=%.4f", atr, symbol, last_atr)
                atr = last_atr
            elif symbol in self._state:
                logger.warning("TrailingStop: ATR=%.4f for %s — no valid ATR, using price-based fallback", atr, symbol)
                atr = price * 0.02  # 2% of price as emergency ATR
            else:
                logger.warning("TrailingStop: ATR=%.4f for %s — new symbol with no ATR, skipping", atr, symbol)
                return {"activated": False}

        # Add new symbol if not tracked
        if symbol not in self._state:
            # entry_price can be provided by caller (from trade history),
            # otherwise falls back to current market price
            if entry_price is None:
                logger.error("TrailingStop: entry_price not provided for %s, using market price as last resort", symbol)
                entry_price = price

            self._state[symbol] = {
                "entry_price": entry_price,
                "highest_price": price,
                "sl_price": 0,
                "activated": False,
                "atr": atr,
            }
            logger.info("TrailingStop: started tracking %s entry=%.4f ATR=%.4f", symbol, entry_price, atr)

        state = self._state[symbol]
        entry = state["entry_price"]
        highest = state["highest_price"]

        # Update ATR if it changed significantly (>20% drift)
        if atr > 0 and abs(atr - state.get("atr", atr)) / max(state.get("atr", 1e-9), 1e-9) > 0.2:
            state["atr"] = atr
            logger.debug("TrailingStop: updated ATR for %s to %.4f", symbol, atr)

        # Update highest price
        if price > highest:
            state["highest_price"] = price
            highest = price

        # Check if trailing is already activated
        if state["activated"]:
            # Adaptive trailing: step-wise tightening based on profit level (Phase 9)
            try:
                from ..strategy.adaptive_trailing import calculate_trailing_sl
            except ImportError:
                logger.error("adaptive_trailing module not available, using ATR fallback")
                new_sl = highest - self.TRAILING_ATR_MULT * atr
                if new_sl > state["sl_price"]:
                    state["sl_price"] = new_sl
            else:
                profit_pct = ((price - entry) / entry) * 100 if entry > 0 else 0
                vol_adj = 1.0
                try:
                    from ..analysis.garch_vol import get_vol_regime
                    daily_vol = atr / price if price > 0 else 0.02
                    vol_regime = get_vol_regime(daily_vol * 365**0.5)
                    vol_adj = {"low": 0.7, "normal": 1.0, "high": 1.3, "extreme": 1.5}.get(vol_regime, 1.0)
                except Exception:
                    logger.warning("GARCH vol unavailable for trailing stop, using vol_adj=1.0")
                try:
                    adaptive = calculate_trailing_sl(
                        entry_price=entry,
                        current_price=price,
                        highest_price=highest,
                        initial_sl=state.get("sl_price", entry * 0.95),
                        volatility_adjustment=vol_adj,
                    )
                    new_sl = adaptive["trailing_sl"]
                    if new_sl > state["sl_price"]:
                        state["sl_price"] = new_sl
                        state["adaptive_step"] = adaptive["step"]
                except Exception:
                    logger.warning("Adaptive trailing failed, using ATR fallback")
                    new_sl = highest - self.TRAILING_ATR_MULT * atr
                    if new_sl > state["sl_price"]:
                        state["sl_price"] = new_sl

            # Check if triggered
            if price <= state["sl_price"]:
                callback_pct = ((highest - price) / highest) * 100 if highest > 0 else 0
                result = {
                    "triggered": True,
                    "symbol": symbol,
                    "entry_price": entry,
                    "highest_price": highest,
                    "sl_price": state["sl_price"],
                    "current_price": price,
                    "callback_pct": round(callback_pct, 2),
                }
                logger.warning(
                    "TrailingStop: TRIGGERED %s entry=%.4f highest=%.4f sl=%.4f price=%.4f callback=%.2f%%",
                    symbol, entry, highest, state["sl_price"], price, callback_pct,
                )
                self._save()
                return result

            callback_pct = ((highest - state["sl_price"]) / highest) * 100 if highest > 0 else 0
            self._save()
            return {
                "activated": True,
                "symbol": symbol,
                "sl_price": round(state["sl_price"], 6),
                "highest_price": round(highest, 6),
                "callback_pct": round(callback_pct, 2),
            }

        # Not yet activated – check activation threshold
        profit = price - entry
        if profit >= self.ACTIVATION_ATR_MULT * atr:
            state["activated"] = True
            state["sl_price"] = highest - self.TRAILING_ATR_MULT * atr
            logger.info(
                "TrailingStop: ACTIVATED %s profit=%.4f >= %.4f threshold",
                symbol, profit, self.ACTIVATION_ATR_MULT * atr,
            )
            self._save()
            return {
                "activated": True,
                "symbol": symbol,
                "sl_price": round(state["sl_price"], 6),
                "highest_price": round(highest, 6),
                "callback_pct": round(((highest - state["sl_price"]) / highest) * 100, 2) if highest > 0 else 0,
            }

        self._save()
        return {"activated": False, "symbol": symbol}

    def remove(self, symbol: str) -> None:
        """Remove a symbol from trailing stop tracking."""
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        if symbol in self._state:
            del self._state[symbol]
            logger.info("TrailingStop: removed %s", symbol)
        # Always remove from SQLite (primary)
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            db.ts_remove(symbol)
            self._save()  # Sync JSON backup
        except Exception as e:
            logger.error(f"TrailingStop: SQLite remove failed for {symbol}: {e}")

    def get_all(self) -> Dict[str, Dict]:
        """Return all trailing stop states."""
        return dict(self._state)

    def get(self, symbol: str) -> Optional[Dict]:
        """Get trailing stop state for a single symbol."""
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        return self._state.get(symbol)

    def force_activate(self, symbol: str, entry_price: float, atr: float) -> None:
        """Force-activate trailing for a symbol (e.g., after position detected from Binance)."""
        symbol = symbol.upper()
        # Normalize symbol to always include USDT suffix for consistency with portfolio
        if not symbol.endswith("USDT"):
            symbol = symbol + "USDT"
        self._state[symbol] = {
            "entry_price": entry_price,
            "highest_price": entry_price,
            "sl_price": entry_price - self.TRAILING_ATR_MULT * atr,
            "activated": True,
            "atr": atr,
        }
        logger.info("TrailingStop: force-activated %s entry=%.4f ATR=%.4f", symbol, entry_price, atr)
        self._save()


# ---------------------------------------------------------------------------
# 3. ConsecutiveLossGuard – 連虧保護
# ---------------------------------------------------------------------------

class ConsecutiveLossGuard:
    """Pause trading after consecutive losing trades.

    PRIMARY STORAGE: SQLite state.db (via StateDB) - risk_guard table
    BACKUP: <project_root>/data/loss_guard.json (human-readable)
    """

    MAX_CONSECUTIVE_LOSSES_SOFT = 3   # After 3: reduce size by 50%
    MAX_CONSECUTIVE_LOSSES_HARD = 5  # After 5: full halt
    PAUSE_DURATION_SEC = 12 * 3600   # 12 hours (was 24)
    SIZE_REDUCTION_PCT = 0.5         # Reduce to 50% after soft threshold
    MAX_HISTORY = 50

    def __init__(self):
        self._filepath = _DATA_DIR / "loss_guard.json"
        # Load from SQLite first, fallback to JSON
        self._state: Dict = self._load_from_db()
        if not self._state:
            self._state = _load_json(self._filepath, default={
                "consecutive_losses": 0,
                "last_loss_time": None,
                "paused_until": None,
                "history": [],
            })

    def _load_from_db(self) -> Optional[Dict]:
        """Load loss guard state from SQLite (primary)."""
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            row = db.risk_get()
            if row:
                logger.info(f"ConsecutiveLossGuard: loaded from StateDB")
                return {
                    "consecutive_losses": row.get("streak", 0),
                    "last_loss_time": row.get("last_reset"),
                    "paused_until": row.get("paused_until"),  # Reconstructed from DB if available
                    "history": list(row.get("history", [])),  # Loaded from JSON backup
                }
        except Exception as e:
            logger.warning(f"ConsecutiveLossGuard: failed to load from StateDB: {e}")
        return None

    def _clear_db_state(self) -> None:
        """Clear any persisted DB state so tests start fresh."""
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            db.risk_set({"daily_pnl": 0, "streak": 0, "last_reset": None})
        except Exception as e:
            logger.warning(f"ConsecutiveLossGuard: failed to clear DB state: {e}")

    def _save(self) -> bool:
        """Persist to SQLite (primary) and JSON backup (disaster recovery)."""
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            db.risk_set({
                "daily_pnl": 0,  # Not tracked here, reserved for future
                "streak": self._state.get("consecutive_losses", 0),
                "last_reset": self._state.get("last_loss_time", time.time()),
                # NOTE: DB column is named "last_reset" but stores "last_loss_time"
                # (timestamp of the most recent loss). Schema migration needed to rename.
            })
            # Also persist history to JSON backup for disaster recovery
            _save_json(self._filepath, self._state)
            return True
        except Exception as e:
            logger.error(f"ConsecutiveLossGuard: SQLite save failed: {e}")
            return False

    def record_trade(self, symbol: str, pnl_usdt: float) -> Dict:
        """Record a trade result and update loss tracking.

        Returns updated status dict.
        """
        symbol = symbol.upper()
        now = time.time()

        trade_record = {
            "time": now,
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "symbol": symbol,
            "pnl": round(pnl_usdt, 4),
        }

        if pnl_usdt < 0:
            self._state["consecutive_losses"] += 1
            self._state["last_loss_time"] = now

            if self._state["consecutive_losses"] >= self.MAX_CONSECUTIVE_LOSSES_HARD:
                self._state["paused_until"] = now + self.PAUSE_DURATION_SEC
                logger.warning(
                    "ConsecutiveLossGuard: %d consecutive losses → HARD HALT until %s",
                    self._state["consecutive_losses"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._state["paused_until"])),
                )
            elif self._state["consecutive_losses"] >= self.MAX_CONSECUTIVE_LOSSES_SOFT:
                logger.warning(
                    "ConsecutiveLossGuard: %d consecutive losses → SOFT: size reduced by %.0f%%",
                    self._state["consecutive_losses"],
                    (1 - self.SIZE_REDUCTION_PCT) * 100,
                )
            else:
                logger.info(
                    "ConsecutiveLossGuard: loss on %s (PnL=%.4f USDT), consecutive=%d",
                    symbol, pnl_usdt, self._state["consecutive_losses"],
                )
        elif pnl_usdt > 0:
            # Only a genuine win (positive PnL) resets the streak.
            # Draw (pnl == 0) does NOT break the consecutive loss count.
            if self._state["consecutive_losses"] > 0:
                logger.info(
                    "ConsecutiveLossGuard: win on %s (PnL=%.4f USDT) broke %d-loss streak",
                    symbol, pnl_usdt, self._state["consecutive_losses"],
                )
            self._state["consecutive_losses"] = 0
            # Clear pause when a win occurs
            if self._state.get("paused_until") is not None:
                self._state["paused_until"] = None
        else:
            # pnl == 0 (draw): log it but don't change streak
            logger.info(
                "ConsecutiveLossGuard: draw on %s (PnL=0), streak unchanged at %d",
                symbol, self._state["consecutive_losses"],
            )

        # Append to history
        self._state["history"].append(trade_record)
        if len(self._state["history"]) > self.MAX_HISTORY:
            self._state["history"] = self._state["history"][-self.MAX_HISTORY:]

        self._save()
        return self.get_status()

    def is_paused(self) -> bool:
        """Check if trading is currently paused due to consecutive losses."""
        paused_until = self._state.get("paused_until")
        if paused_until is None:
            return False
        if time.time() >= paused_until:
            # Pause expired – clear it
            self._state["paused_until"] = None
            self._state["consecutive_losses"] = 0
            self._save()
            logger.info("ConsecutiveLossGuard: pause expired, trading resumed")
            return False
        return True

    def check_consecutive_losses(self) -> Dict:
        """Check consecutive loss state and return sizing/halt recommendation.

        Returns:
            {
                should_halt: bool,         # True after hard threshold (5 losses)
                size_multiplier: float,    # 0.5 after soft threshold, 1.0 otherwise
                consecutive_losses: int,
                level: str,                # "normal", "soft", "hard"
            }
        """
        streak = self._state.get("consecutive_losses", 0)

        # Hard halt: pause is active
        if self.is_paused():
            return {
                "should_halt": True,
                "size_multiplier": 0.0,
                "consecutive_losses": streak,
                "level": "hard",
            }

        # Soft threshold: reduce size
        if streak >= self.MAX_CONSECUTIVE_LOSSES_SOFT:
            if streak >= self.MAX_CONSECUTIVE_LOSSES_HARD:
                return {
                    "should_halt": True,
                    "size_multiplier": 0.0,
                    "consecutive_losses": streak,
                    "level": "hard",
                }
            return {
                "should_halt": False,
                "size_multiplier": self.SIZE_REDUCTION_PCT,
                "consecutive_losses": streak,
                "level": "soft",
            }

        return {
            "should_halt": False,
            "size_multiplier": 1.0,
            "consecutive_losses": streak,
            "level": "normal",
        }

    def get_status(self) -> Dict:
        """Return current guard status."""
        paused = self.is_paused()
        cl = self.check_consecutive_losses()
        return {
            "consecutive_losses": self._state["consecutive_losses"],
            "paused": paused,
            "paused_until": self._state.get("paused_until"),
            "paused_until_str": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._state["paused_until"]))
                if self._state.get("paused_until") else None
            ),
            "total_trades": len(self._state.get("history", [])),
            "last_loss_time": self._state.get("last_loss_time"),
            "size_multiplier": cl["size_multiplier"],
            "level": cl["level"],
        }

    def get_history(self) -> List[Dict]:
        """Return trade history."""
        return list(self._state.get("history", []))

    def reset(self) -> None:
        """Manually reset the guard (e.g., for testing or forced resume)."""
        self._state["consecutive_losses"] = 0
        self._state["paused_until"] = None
        self._save()
        logger.info("ConsecutiveLossGuard: manually reset")


# ---------------------------------------------------------------------------
# 4. SectorExposure – 板塊曝險管理
# ---------------------------------------------------------------------------

from ..analysis.sector_classifier import SectorExposure


# ---------------------------------------------------------------------------
# 5. RiskManager – 統一入口
# ---------------------------------------------------------------------------

class DailyLossLimit:
    """Pause trading when cumulative daily PnL exceeds a threshold.

    Inspired by freqtrade's max_drawdown_protection but simpler:
    tracks daily PnL in StateDB, blocks new trades when loss > threshold.
    Resets at midnight (local time).
    """

    MAX_DAILY_LOSS_PCT = 5.0   # stop trading if daily loss exceeds 5% of portfolio
    MAX_DAILY_LOSS_USDT_PCT = 0.01  # 1% of portfolio (replaces fixed $50)

    def __init__(self):
        self._daily_pnl: float = 0.0
        self._date: str = time.strftime("%Y-%m-%d")
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            row = db.risk_get()
            if row:
                stored_date = row.get("daily_pnl_date", "")
                if stored_date == self._date:
                    self._daily_pnl = float(row.get("daily_pnl", 0))
        except Exception:
            logger.error("Failed to load daily PnL from StateDB", exc_info=True)

    def _save(self) -> None:
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            db.risk_set({
                "daily_pnl": self._daily_pnl,
                "daily_pnl_date": self._date,
            })
        except Exception:
            logger.error("Failed to save daily PnL to StateDB", exc_info=True)

    def record_pnl(self, pnl_usdt: float) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._date:
            self._daily_pnl = 0.0
            self._date = today
        self._daily_pnl += pnl_usdt
        self._save()

    def is_blocked(self, portfolio_value: float = 0) -> Dict:
        today = time.strftime("%Y-%m-%d")
        if today != self._date:
            self._daily_pnl = 0.0
            self._date = today

        # Portfolio-relative absolute threshold: max(50, portfolio * 1%)
        max_daily_loss_usdt = max(50.0, portfolio_value * self.MAX_DAILY_LOSS_USDT_PCT)
        if self._daily_pnl < -max_daily_loss_usdt:
            return {"blocked": True, "reason": f"Daily loss ${abs(self._daily_pnl):.2f} > ${max_daily_loss_usdt:.0f}"}

        if portfolio_value > 0:
            loss_pct = abs(self._daily_pnl) / portfolio_value * 100
            if loss_pct > self.MAX_DAILY_LOSS_PCT:
                return {"blocked": True, "reason": f"Daily loss {loss_pct:.1f}% > {self.MAX_DAILY_LOSS_PCT:.0f}%"}

        return {"blocked": False, "daily_pnl": self._daily_pnl}


class PerPairCooldown:
    """Cooldown per trading pair after a trade closes.

    Prevents immediately re-entering the same pair after a loss.
    Inspired by freqtrade's cooldown_period protection.
    """

    COOLDOWN_SEC = 15 * 60  # 15 minutes (was 30)
    LOSS_COOLDOWN_SEC = 60 * 60  # 1 hour after a loss (unchanged)

    def __init__(self):
        self._cooldowns: Dict[str, float] = {}  # symbol -> cooldown_until timestamp
        self._load_from_db()

    def _load_from_db(self) -> None:
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            # Store cooldowns in the same risk_guard JSON blob
            row = db.risk_get()
            if row and "pair_cooldowns" in row:
                self._cooldowns = row["pair_cooldowns"]
        except Exception:
            logger.error("Failed to load per-pair cooldowns from StateDB", exc_info=True)

    def _save(self) -> None:
        try:
            from ..core.state_db import get_state_db
            db = get_state_db()
            existing = db.risk_get() or {}
            existing["pair_cooldowns"] = self._cooldowns
            db.risk_set(existing)
        except Exception:
            logger.error("Failed to save per-pair cooldowns to StateDB", exc_info=True)

    def record_close(self, symbol: str, pnl_usdt: float) -> None:
        symbol = symbol.upper()
        cooldown = self.LOSS_COOLDOWN_SEC if pnl_usdt < 0 else self.COOLDOWN_SEC
        self._cooldowns[symbol] = time.time() + cooldown
        self._save()
        logger.info("PerPairCooldown: %s on cooldown for %ds (PnL=%.4f)", symbol, cooldown, pnl_usdt)

    def is_on_cooldown(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        until = self._cooldowns.get(symbol, 0)
        now = time.time()
        if until > now:
            remaining = int(until - now)
            return {"blocked": True, "remaining_sec": remaining, "reason": f"Cooldown {remaining}s remaining"}
        return {"blocked": False}

    def clear(self, symbol: str) -> None:
        self._cooldowns.pop(symbol.upper(), None)
        self._save()


class RiskManager:
    """Unified risk management entry point.

    Orchestrates TrendFilter, TrailingStop, ConsecutiveLossGuard, and SectorExposure.
    """

    def __init__(self, binance_client: Optional['ExchangeClient'] = None):
        self.client = binance_client
        self.trend_filter = TrendFilter()
        self.trailing_stop = TrailingStop()
        self.loss_guard = ConsecutiveLossGuard()
        self.sector_exposure = SectorExposure()
        self.correlation_risk = CorrelationRiskManager(binance_client) if binance_client else None
        self.drawdown_breaker = DrawdownBreaker(binance_client) if binance_client else None
        self.daily_loss = DailyLossLimit()
        self.pair_cooldown = PerPairCooldown()
        logger.info("RiskManager initialized (all sub-modules ready)")

    def pre_trade_check(
        self,
        symbol: str,
        price: float,
        atr: float,
        positions: Optional[List[Dict]] = None,
        score: Optional[float] = None,
        strategy: Optional[str] = None,
    ) -> Dict:
        """Run all pre-trade risk checks in order.

        Args:
            symbol: Trading pair (e.g., 'RNDRUSDT')
            price: Current price (unused by all checks currently, kept for future)
            atr: ATR value for the symbol
            positions: List of current position dicts with 'symbol' and 'value_usdt'
            score: Optional signal score (reserved for future use)
            strategy: Optional strategy name. DCA/RSI/Bollinger are allowed in BEARISH BTC.

        Returns:
            {
                allowed: bool,
                reasons: [str, ...],      # human-readable reasons if blocked
                adjustments: {             # suggested adjustments if allowed
                    size_multiplier: float,
                    ...
                },
            }
        """
        reasons: List[str] = []
        adjustments: Dict[str, Any] = {"size_multiplier": 1.0}
        allowed = True

        # 1. Trend filter
        if self.client:
            try:
                trend = self.trend_filter.check_trend(self.client)
                if not trend.get("allow_long", True):
                    # DCA/RSI/Bollinger allowed in bearish for fear-buying
                    fear_buy_strategies = {"dca", "rsi", "bollinger"}
                    if strategy and strategy.lower() in fear_buy_strategies:
                        adjustments["size_multiplier"] = 0.5  # half size in bear
                        reasons.append(
                            f"BTC trend={trend['trend']} – {strategy} allowed at half size"
                        )
                    else:
                        allowed = False
                        reasons.append(
                            f"BTC trend={trend['trend']} – longs not allowed"
                        )
                else:
                    adjustments["size_multiplier"] = trend.get("size_multiplier", 1.0)
                    if adjustments["size_multiplier"] < 1.0:
                        reasons.append(
                            f"BTC ADX={trend['adx']:.1f} (< 20) – reducing size to 50%"
                        )
            except Exception as e:
                logger.error(f"RiskManager: trend filter error: {e}")
                # Fail-open for trend filter but reduce size as safety measure
                adjustments["size_multiplier"] = min(
                    adjustments["size_multiplier"], 0.5
                )
                reasons.append("TrendFilter: API error — reducing size to 50% as safety")
        else:
            logger.debug("RiskManager: no binance_client, skipping trend filter")

        # 2. Sector exposure
        if positions is not None:
            try:
                sector = SectorExposure.classify_position(symbol)
                if not self.sector_exposure.is_sector_allowed(symbol, positions):
                    allowed = False
                    reasons.append(
                        f"Sector '{sector}' exposure at/above {SectorExposure.MAX_SECTOR_PCT}%"
                    )
            except Exception as e:
                logger.error(f"RiskManager: sector exposure check error: {e}")

        # 3. Correlation risk (if client available and positions exist)
        if self.correlation_risk and positions is not None and len(positions) > 0:
            try:
                current_symbols = [
                    (p.get("symbol") or p.get("asset", "")).replace("USDT", "")
                    for p in positions
                ]
                new_sym = symbol.replace("USDT", "")
                corr_check = self.correlation_risk.check_new_position(new_sym, current_symbols)
                if not corr_check["allowed"]:
                    allowed = False
                    reasons.append(f"Correlation: {corr_check['reason']}")
                else:
                    corr_sm = corr_check.get("size_multiplier", 1.0)
                    if corr_sm < 1.0:
                        adjustments["size_multiplier"] = round(
                            adjustments["size_multiplier"] * corr_sm, 2
                        )
                        reasons.append(
                            f"Correlation fail-open: reducing size to ×{corr_sm} — {corr_check['reason']}"
                        )
                    reasons.append(
                        f"Correlation OK: {corr_check['reason']}"
                    )
            except Exception as e:
                logger.error(f"RiskManager: correlation check error: {e}")

        # 4. Loss guard (paused check)
        try:
            if self.loss_guard.is_paused():
                allowed = False
                status = self.loss_guard.get_status()
                reasons.append(
                    f"Trading paused: {status['consecutive_losses']} consecutive losses "
                    f"(until {status['paused_until_str']})"
                )
        except Exception as e:
            logger.error(f"RiskManager: loss guard check error: {e}")

        # 4b. Daily loss limit (freqtrade pattern)
        try:
            # Compute actual portfolio value for daily loss limit
            _rm_portfolio_value = 0.0
            if self.client:
                try:
                    _rm_acct = self.client.get_account()
                    for b in _rm_acct.get("balances", []):
                        _asset = b["asset"]
                        _qty = float(b.get("free", 0)) + float(b.get("locked", 0))
                        if _qty > 0:
                            if _asset == "USDT":
                                _rm_portfolio_value += _qty
                            else:
                                try:
                                    _p = float(self.client.get_ticker_price(f"{_asset}USDT"))
                                    _rm_portfolio_value += _qty * _p
                                except Exception:
                                    pass
                except Exception:
                    logger.error("RiskManager: failed to compute portfolio value for daily loss", exc_info=True)
            daily = self.daily_loss.is_blocked(portfolio_value=_rm_portfolio_value)
            if daily.get("blocked"):
                allowed = False
                reasons.append(f"DailyLoss: {daily['reason']}")
        except Exception as e:
            logger.error(f"RiskManager: daily loss check error: {e}")

        # 4c. Per-pair cooldown (freqtrade pattern)
        try:
            cd = self.pair_cooldown.is_on_cooldown(symbol)
            if cd.get("blocked"):
                allowed = False
                reasons.append(f"PairCooldown: {cd['reason']}")
        except Exception as e:
            logger.error(f"RiskManager: pair cooldown check error: {e}")

        # 5. Drawdown breaker (10% hard stop)
        dd_check = {}  # Initialize before try block — used by stepwise drawdown below
        if self.drawdown_breaker and self.client:
            try:
                acct = self.client.get_account()
                # Use USDT-equivalent total (only USDT balance + current positions' value)
                usdt_bal = 0.0
                position_value = 0.0

                # Batch fetch all ticker prices in one API call (C3 fix)
                all_tickers = {}
                try:
                    tickers = self.client.get_24hr_stats()
                    if isinstance(tickers, list):
                        all_tickers = {
                            t["symbol"]: float(t.get("last_price", 0))
                            for t in tickers
                            if "symbol" in t
                        }
                except Exception as e:
                    logger.debug(f"RiskManager: batch ticker fetch failed: {e}")

                for b in acct.get("balances", []):
                    free = float(b.get("free", 0))
                    locked = float(b.get("locked", 0))
                    total_qty = free + locked
                    if total_qty <= 0:
                        continue
                    asset = b["asset"]
                    if asset == "USDT":
                        usdt_bal += total_qty
                    elif total_qty > 0.0001:
                        # Use batch-fetched price instead of per-asset API call
                        price_val = all_tickers.get(f"{asset}USDT", 0)
                        if price_val > 0:
                            position_value += total_qty * price_val
                total_equity = usdt_bal + position_value
                dd_check = self.drawdown_breaker.check_drawdown(total_equity)
                if dd_check["tripped"]:
                    allowed = False
                    reasons.append(f"DRAWDOWN BREAKER: {dd_check['reason']}")
                else:
                    reasons.append(f"Drawdown: {dd_check['drawdown_pct']:.1f}%")
            except Exception as e:
                logger.error(f"RiskManager: drawdown check error: {e}")
                # Fail-closed: if we can't check drawdown, block trading for safety
                allowed = False
                reasons.append(f"DRAWDOWN CHECK FAILED: {e} — blocking trade for safety")

        # 6. Stepwise drawdown — graduated risk reduction (P1: integrate into pre_trade_check)
        if self.drawdown_breaker and self.client:
            try:
                from .stepwise_drawdown import get_drawdown_action
                dd_pct = dd_check.get("drawdown_pct", 0.0)
                sd_action = get_drawdown_action(dd_pct)
                sd_sm = sd_action.get("size_multiplier", 1.0)
                if sd_sm < 1.0:
                    adjustments["size_multiplier"] = round(
                        adjustments["size_multiplier"] * sd_sm, 2
                    )
                    reasons.append(
                        f"StepwiseDrawdown {sd_action['level']}: size ×{sd_sm} — {sd_action['reason']}"
                    )
                if sd_action.get("block_new_trades"):
                    allowed = False
                    reasons.append(f"StepwiseDrawdown {sd_action['level']}: blocking new trades")
            except Exception as e:
                logger.error(f"RiskManager: stepwise drawdown check error: {e}")

        result = {
            "allowed": allowed,
            "reasons": reasons,
            "adjustments": adjustments,
        }

        if allowed:
            logger.info(
                "RiskManager: pre-trade PASS for %s (size_mult=%.1f)",
                symbol, adjustments["size_multiplier"],
            )
        else:
            logger.warning(
                "RiskManager: pre-trade BLOCK %s – %s",
                symbol, "; ".join(reasons),
            )

        return result

    def post_trade_update(self, symbol: str, pnl: float, remaining_qty: float = 0) -> None:
        """Update trailing stop and loss guard after a trade closes.

        Args:
            symbol: Symbol that was traded
            pnl: PnL in USDT (negative = loss)
            remaining_qty: Remaining position quantity after the trade.
                          If > 0, trailing stop is preserved (partial close).
                          If 0 (default), trailing stop is removed (full close). (H4 fix)
        """
        symbol = symbol.upper()

        # Update loss guard
        try:
            self.loss_guard.record_trade(symbol, pnl)
        except Exception as e:
            logger.error(f"RiskManager: failed to update loss guard: {e}")

        # Update daily loss limit
        try:
            self.daily_loss.record_pnl(pnl)
        except Exception as e:
            logger.error(f"RiskManager: failed to update daily loss: {e}")

        # Record pair cooldown
        try:
            self.pair_cooldown.record_close(symbol, pnl)
        except Exception as e:
            logger.error(f"RiskManager: failed to record pair cooldown: {e}")

        # If position fully closed, remove from trailing stop (H4 fix: only on full close)
        try:
            if remaining_qty <= 0:
                self.trailing_stop.remove(symbol)
            else:
                logger.info(
                    "RiskManager: trailing stop preserved for %s (remaining_qty=%.8f, partial close)",
                    symbol, remaining_qty,
                )
        except Exception as e:
            logger.error(f"RiskManager: failed to remove trailing stop: {e}")

        logger.info(
            "RiskManager: post-trade update for %s (PnL=%.4f USDT)",
            symbol, pnl,
        )

    def get_full_status(self) -> Dict:
        """Return a comprehensive status of all risk modules."""
        trend = self.trend_filter._cache if self.trend_filter._cache else {"trend": "N/A"}
        return {
            "trend_filter": trend,
            "trailing_stops": self.trailing_stop.get_all(),
            "loss_guard": self.loss_guard.get_status(),
            "drawdown_breaker": self.drawdown_breaker.get_status() if self.drawdown_breaker is not None else None,
            "sector_exposure": {
                "max_sector_pct": self.sector_exposure.MAX_SECTOR_PCT,
                "sectors": list(self.sector_exposure.SECTORS.keys()),
            },
        }

    def calculate_kelly_fraction(self, lookback_trades: int = 50) -> Dict:
        """Calculate Kelly Criterion fraction for position sizing.

        Kelly formula: f* = (bp - q) / b
        Where:
            b = avg_win / avg_loss (profit/loss ratio)
            p = win_rate (probability of winning)
            q = 1 - p (probability of losing)

        Args:
            lookback_trades: Number of recent trades to analyze

        Returns:
            {
                kelly_fraction: float,  # optimal fraction (0-1, capped at 0.25)
                win_rate: float,        # historical win rate
                profit_ratio: float,    # avg_win / avg_loss
                trades_analyzed: int,   # number of trades used
                recommendation: str,    # human-readable recommendation
            }
        """
        from ..core.state_db import get_state_db
        import sqlite3

        db = get_state_db()
        conn = db._get_conn()

        # Get recent closed trades
        rows = conn.execute(
            """SELECT net_pnl_pct, is_win FROM trade_outcomes
               WHERE status = 'closed' AND net_pnl_pct IS NOT NULL
               ORDER BY entry_time DESC LIMIT ?""",
            (lookback_trades,)
        ).fetchall()

        if not rows or len(rows) < 5:
            return {
                "kelly_fraction": 0.10,  # conservative default
                "win_rate": 0.5,
                "profit_ratio": 1.0,
                "trades_analyzed": len(rows) if rows else 0,
                "recommendation": "交易數據不足，使用保守倉位 10%",
            }

        # Calculate statistics
        wins = [r[0] for r in rows if r[0] > 0]
        losses = [abs(r[0]) for r in rows if r[0] < 0]

        p = len(wins) / len(rows)  # win rate
        q = 1 - p                  # loss rate

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 1

        b = avg_win / avg_loss if avg_loss > 0 else 1  # profit ratio

        # Kelly formula
        kelly = (b * p - q) / b if b > 0 else 0

        # Cap at 25% for safety (half-Kelly is common)
        kelly_capped = max(0, min(0.25, kelly))

        # Generate recommendation
        if kelly <= 0:
            recommendation = f"Kelly≤0 ({kelly:.2%})，系統不建議交易"
        elif kelly < 0.05:
            recommendation = f"Kelly極低 ({kelly:.2%})，建議最小倉位"
        elif kelly < 0.15:
            recommendation = f"Kelly={kelly:.2%}，建議標準倉位"
        else:
            recommendation = f"Kelly={kelly:.2%}，使用 capped 25% 倉位"

        return {
            "kelly_fraction": round(kelly_capped, 4),
            "win_rate": round(p, 4),
            "profit_ratio": round(b, 4),
            "trades_analyzed": len(rows),
            "recommendation": recommendation,
        }
