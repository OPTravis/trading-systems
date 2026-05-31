"""
Portfolio Manager — Multi-currency portfolio with T+1 settlement tracking.

Handles:
- Multi-currency support (USD, HKD, CNY)
- Position management (add, reduce, close)
- P&L calculation (realized + unrealized)
- NAV calculation
- Cash available vs unsettled funds (T+1 US, T+2 HK)
- Broker sync
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# FX rate cache (module-level, shared across PortfolioManager instances)
_FX_CACHE: Dict[str, float] = {}
_FX_CACHE_TS: float = 0.0
_FX_CACHE_TTL: float = 3600.0  # 1 hour


# ─── Settlement rules per market ────────────────────────────────────────────


def _get_fx_to_usd(currency: str) -> float:
    """Get FX rate to convert `currency` to 1 USD. Cached for 1 hour.

    Returns:
        Multiplier: amount_in_usd = amount_in_foreign / rate.
        For USD, returns 1.0. For HKD, returns ~7.8 (7.8 HKD = 1 USD).
    """
    global _FX_CACHE, _FX_CACHE_TS
    if currency == "USD":
        return 1.0

    now = time.time()
    if _FX_CACHE and (now - _FX_CACHE_TS) < _FX_CACHE_TTL:
        return _FX_CACHE.get(currency, 1.0)

    # Refresh cache from yfinance
    try:
        import yfinance as yf

        pairs = {
            "HKD": "USDHKD=X",
            "CNY": "USDCNY=X",
            "CNH": "USDCNH=X",
            "JPY": "USDJPY=X",
            "EUR": "EURUSD=X",  # 1 EUR = X USD (multiply)
            "GBP": "GBPUSD=X",  # 1 GBP = X USD (multiply)
            "AUD": "AUDUSD=X",  # 1 AUD = X USD (multiply)
        }
        symbols = list(set(pairs.values()))
        tickers = yf.Tickers(" ".join(symbols))
        new_cache: Dict[str, float] = {}
        for cur, pair in pairs.items():
            try:
                info = tickers.tickers[pair].info
                price = info.get("regularMarketPrice") or info.get("previousClose", 0)
                if price and price > 0:
                    # For USD-denominated pairs (USDCNY, USDHKD, USDJPY):
                    #   rate = X means 1 USD = X foreign → divide by rate
                    # For foreign-denominated pairs (EURUSD, GBPUSD, AUDUSD):
                    #   rate = X means 1 foreign = X USD → multiply by rate
                    if pair.startswith("USD"):
                        new_cache[cur] = price  # divide later
                    else:
                        new_cache[cur] = 1.0 / price  # convert to "per USD" rate
            except Exception:
                pass
        if new_cache:
            _FX_CACHE = new_cache
            _FX_CACHE_TS = now
            logger.info(
                "FX rates refreshed: %s", {k: round(v, 4) for k, v in new_cache.items()}
            )
    except Exception as e:
        logger.warning("Failed to fetch FX rates: %s", e)

    rate = _FX_CACHE.get(currency, 1.0)
    if currency != "USD" and rate == 1.0:
        logger.warning(
            "FX rate for %s not available, using fallback rate 1.0 — "
            "NAV calculations may be inaccurate",
            currency,
        )
    return rate


SETTLEMENT_DAYS = {
    "US": 1,  # T+1
    "HK": 2,  # T+2
    "CN": 1,  # T+1 (A-share)
    "DEFAULT": 2,
}

CURRENCY_SYMBOLS = {
    "USD": "$",
    "HKD": "HK$",
    "CNY": "¥",
}


# ─── Data models ────────────────────────────────────────────────────────────


@dataclass
class UnsettledTrade:
    """Record of an unsettled trade proceeds or purchase."""

    amount: float
    currency: str
    market: str
    trade_date: date
    settle_date: date
    trade_type: str  # 'BUY' or 'SELL'


@dataclass
class Position:
    """Single stock position."""

    symbol: str
    quantity: float
    entry_price: float
    current_price: float
    currency: str = "USD"
    market: str = "US"
    sector: str = ""
    strategy: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    highest_price: float = 0.0
    opened_at: str = ""
    updated_at: str = ""
    unrealized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.entry_price

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price / self.entry_price - 1) * 100

    def to_dict(self) -> dict:
        def _safe(v):
            """Replace NaN/inf with None for JSON serialization safety."""
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v

        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "entry_price": _safe(self.entry_price),
            "current_price": _safe(self.current_price),
            "currency": self.currency,
            "market": self.market,
            "sector": self.sector,
            "strategy": self.strategy,
            "stop_loss": _safe(self.stop_loss),
            "take_profit": _safe(self.take_profit),
            "highest_price": _safe(self.highest_price),
            "market_value": _safe(self.market_value),
            "cost_basis": _safe(self.cost_basis),
            "unrealized_pnl": _safe(self.unrealized_pnl),
            "unrealized_pnl_pct": _safe(self.unrealized_pnl_pct),
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
        }


@dataclass
class RealizedTrade:
    """Record of a closed (realized) trade."""

    symbol: str
    side: str  # 'BUY' or 'SELL'
    quantity: float
    price: float
    pnl: float = 0.0
    currency: str = "USD"
    market: str = "US"
    timestamp: str = ""


@dataclass
class CashAccount:
    """Cash balance for a single currency."""

    currency: str
    total_cash: float = 0.0
    unsettled: List[UnsettledTrade] = field(default_factory=list)

    def available(self, today: Optional[date] = None) -> float:
        """Cash available for trading (excludes unsettled)."""
        today = today or date.today()
        self._settle_past(today)
        unsettled_total = sum(
            t.amount for t in self.unsettled if t.trade_type == "SELL"
        )
        reserved = sum(t.amount for t in self.unsettled if t.trade_type == "BUY")
        return max(0.0, self.total_cash - unsettled_total - reserved)

    def unsettled_amount(self, today: Optional[date] = None) -> float:
        today = today or date.today()
        self._settle_past(today)
        return sum(t.amount for t in self.unsettled)

    def record_sell(
        self, amount: float, market: str = "US", trade_date: Optional[date] = None
    ):
        """Record sale proceeds; funds unavailable until settlement."""
        trade_date = trade_date or date.today()
        settle_days = SETTLEMENT_DAYS.get(market.upper(), SETTLEMENT_DAYS["DEFAULT"])
        settle_date = trade_date + timedelta(days=settle_days)
        self.unsettled.append(
            UnsettledTrade(
                amount=amount,
                currency=self.currency,
                market=market.upper(),
                trade_date=trade_date,
                settle_date=settle_date,
                trade_type="SELL",
            )
        )
        self.total_cash += amount
        logger.info(
            "SELL proceeds $%.2f %s settles %s", amount, self.currency, settle_date
        )

    def record_buy(
        self, amount: float, market: str = "US", trade_date: Optional[date] = None
    ):
        """Record cash deduction for a buy."""
        trade_date = trade_date or date.today()
        self.total_cash -= amount

    def _settle_past(self, today: date):
        before = len(self.unsettled)
        self.unsettled = [t for t in self.unsettled if t.settle_date > today]
        settled = before - len(self.unsettled)
        if settled:
            logger.info("Settled %d trades in %s account", settled, self.currency)


# ─── Portfolio Manager ──────────────────────────────────────────────────────


class PortfolioManager:
    """
    Multi-currency portfolio manager for spot-only stock trading.

    Tracks positions, cash (per currency), realized/unrealized P&L,
    and settlement schedules for US (T+1) and HK (T+2) markets.
    """

    DUST_THRESHOLD_USD = 1.0
    MAX_POSITIONS = 20

    def __init__(self, db=None):
        """
        Args:
            db: Optional StateDB instance for persistence.
        """
        self._db = db
        self._positions: Dict[str, Position] = {}
        self._cash: Dict[str, CashAccount] = {
            "USD": CashAccount(currency="USD"),
            "HKD": CashAccount(currency="HKD"),
            "CNY": CashAccount(currency="CNY"),
        }
        self._realized_trades: List[RealizedTrade] = []
        self._last_save_time = 0.0
        self._save_debounce_sec = 2

        # Load from DB if available
        if self._db is not None:
            try:
                self._load_from_db()
            except Exception as e:
                logger.warning("Failed to load portfolio from DB: %s", e)

    # ── Position Management ─────────────────────────────────────────────

    def add_position(
        self,
        symbol: str,
        quantity: float,
        price: float,
        currency: str = "USD",
        market: str = "US",
        sector: str = "",
        strategy: str = "",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Position:
        """Open or add to a position. Returns the updated position."""
        now = datetime.now().isoformat()

        # Deduct cash
        cash = self._get_cash(currency)
        cost = quantity * price
        if cash.available() < cost:
            raise ValueError(
                f"Insufficient {currency} cash: need {cost:.2f}, "
                f"available {cash.available():.2f}"
            )
        cash.record_buy(cost, market=market)

        if symbol in self._positions:
            # Merge: weighted average entry
            pos = self._positions[symbol]
            old_qty = pos.quantity
            new_qty = old_qty + quantity
            new_entry = (old_qty * pos.entry_price + quantity * price) / new_qty
            pos.quantity = new_qty
            pos.entry_price = new_entry
            pos.current_price = price
            pos.updated_at = now
            if stop_loss is not None:
                pos.stop_loss = stop_loss
            if take_profit is not None:
                pos.take_profit = take_profit
            logger.info(
                "Merged position: %s -> %.2f @ %.2f", symbol, new_qty, new_entry
            )
        else:
            pos = Position(
                symbol=symbol,
                quantity=quantity,
                entry_price=price,
                current_price=price,
                currency=currency,
                market=market,
                sector=sector,
                strategy=strategy,
                stop_loss=stop_loss,
                take_profit=take_profit,
                highest_price=price,
                opened_at=now,
                updated_at=now,
            )
            self._positions[symbol] = pos
            logger.info(
                "Opened position: %s %.2f @ %.2f (%s)",
                symbol,
                quantity,
                price,
                currency,
            )

        # Record trade
        self._realized_trades.append(
            RealizedTrade(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                price=price,
                currency=currency,
                market=market,
                timestamp=now,
            )
        )

        self._save(force=True)
        return pos

    def reduce_position(
        self,
        symbol: str,
        quantity: float,
        price: float,
    ) -> dict:
        """Reduce a position by selling partial quantity. Returns P&L details."""
        if symbol not in self._positions:
            raise ValueError(f"No position in {symbol}")

        pos = self._positions[symbol]
        if quantity > pos.quantity:
            raise ValueError(
                f"Cannot sell {quantity} of {symbol}, only hold {pos.quantity}"
            )

        now = datetime.now().isoformat()
        pnl = (price - pos.entry_price) * quantity
        proceeds = quantity * price
        entry_price = pos.entry_price
        pos_currency = pos.currency
        pos_market = pos.market

        # Credit cash
        cash = self._get_cash(pos_currency)
        cash.record_sell(proceeds, market=pos_market)

        # Reduce quantity
        pos.quantity -= quantity
        pos.current_price = price
        pos.updated_at = now

        # Capture remaining quantity before potential deletion
        remaining_qty = pos.quantity

        # Remove position if fully closed
        if pos.quantity <= 0:
            del self._positions[symbol]

        # Record realized trade
        trade = RealizedTrade(
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=price,
            pnl=pnl,
            currency=pos_currency,
            market=pos_market,
            timestamp=now,
        )
        self._realized_trades.append(trade)

        self._save(force=True)
        logger.info("Reduced %s: %.2f @ %.2f, P&L=%.2f", symbol, quantity, price, pnl)

        return {
            "symbol": symbol,
            "quantity_sold": quantity,
            "price": price,
            "proceeds": proceeds,
            "pnl": pnl,
            "pnl_pct": (price / entry_price - 1) * 100 if entry_price > 0 else 0,
            "remaining_qty": remaining_qty,
        }

    def close_position(self, symbol: str, price: float) -> dict:
        """Close entire position. Returns P&L details."""
        if symbol not in self._positions:
            raise ValueError(f"No position in {symbol}")
        pos = self._positions[symbol]
        return self.reduce_position(symbol, pos.quantity, price)

    def update_price(self, symbol: str, price: float):
        """Update current price for a position."""
        if symbol in self._positions:
            pos = self._positions[symbol]
            pos.current_price = price
            if price > pos.highest_price:
                pos.highest_price = price
            pos.updated_at = datetime.now().isoformat()

    def update_prices(self, prices: Dict[str, float]):
        """Batch update prices for multiple symbols."""
        for sym, price in prices.items():
            self.update_price(sym, price)

    # ── Queries ─────────────────────────────────────────────────────────

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def get_all_positions(self) -> List[Position]:
        return list(self._positions.values())

    def get_position_dicts(self) -> List[dict]:
        return [p.to_dict() for p in self._positions.values()]

    @property
    def position_count(self) -> int:
        return len(self._positions)

    # ── P&L ─────────────────────────────────────────────────────────────

    def get_unrealized_pnl(self, currency: Optional[str] = None) -> float:
        """Total unrealized P&L across positions (optionally filtered by currency)."""
        total = 0.0
        for pos in self._positions.values():
            if currency and pos.currency != currency:
                continue
            total += pos.unrealized_pnl
        return total

    def get_realized_pnl(self, currency: Optional[str] = None) -> float:
        """Total realized P&L from closed trades."""
        total = 0.0
        for t in self._realized_trades:
            if currency and t.currency != currency:
                continue
            total += t.pnl
        return total

    def get_total_pnl(self, currency: Optional[str] = None) -> float:
        """Total P&L = realized + unrealized."""
        return self.get_realized_pnl(currency) + self.get_unrealized_pnl(currency)

    def get_realized_trades(self, limit: int = 100) -> List[RealizedTrade]:
        return self._realized_trades[-limit:]

    # ── NAV & Exposure ──────────────────────────────────────────────────

    def get_market_value(self, currency: Optional[str] = None) -> float:
        """Total market value of all positions."""
        total = 0.0
        for pos in self._positions.values():
            if currency and pos.currency != currency:
                continue
            total += pos.market_value
        return total

    def get_nav(self) -> float:
        """Net Asset Value = cash + market value across all currencies (in USD-equivalent)."""
        nav = 0.0
        for cur, cash in self._cash.items():
            rate = _get_fx_to_usd(cur)
            nav += cash.total_cash / rate if rate > 0 else cash.total_cash
        # Market value — positions store market_value in their native currency
        for pos in self._positions.values():
            rate = _get_fx_to_usd(pos.currency)
            nav += pos.market_value / rate if rate > 0 else pos.market_value
        return nav

    def get_cash_balance(self, currency: str = "USD") -> float:
        """Total cash in a currency (including unsettled)."""
        return self._get_cash(currency).total_cash

    def get_available_cash(self, currency: str = "USD") -> float:
        """Cash available for trading (excluding unsettled)."""
        return self._get_cash(currency).available()

    def get_unsettle_breakdown(self, currency: str = "USD") -> Dict[str, float]:
        """Unsettled amounts by market for a currency."""
        cash = self._get_cash(currency)
        today = date.today()
        cash._settle_past(today)
        breakdown: Dict[str, float] = {}
        for t in cash.unsettled:
            breakdown[t.market] = breakdown.get(t.market, 0) + t.amount
        return breakdown

    def get_exposure_pct(self) -> float:
        """Portfolio exposure as percentage of NAV."""
        nav = self.get_nav()
        if nav <= 0:
            return 0.0
        return (self.get_market_value() / nav) * 100

    def get_sector_exposure(self) -> Dict[str, float]:
        """Exposure by sector as percentage of NAV."""
        nav = self.get_nav()
        if nav <= 0:
            return {}
        exposure: Dict[str, float] = {}
        for pos in self._positions.values():
            sector = pos.sector or "Unknown"
            exposure[sector] = exposure.get(sector, 0.0) + pos.market_value
        return {k: (v / nav) * 100 for k, v in exposure.items()}

    # ── Broker Sync ─────────────────────────────────────────────────────

    def sync_from_broker(self, broker) -> bool:
        """
        Sync positions and cash from broker API.

        Args:
            broker: BrokerProtocol instance.

        Returns:
            True if sync succeeded.
        """
        try:
            account = broker.get_account()
            positions = broker.get_portfolio()
        except Exception as e:
            logger.error("Broker sync failed: %s", e)
            return False

        # Save for rollback
        old_positions = dict(self._positions)
        old_cash = dict(self._cash)

        try:
            # Sync cash
            currency = account.currency or "USD"
            self._get_cash(currency).total_cash = account.total_cash

            # Sync positions
            self._positions = {}
            for pos in positions:
                if abs(pos.quantity) < 0.001:
                    continue
                contract = pos.contract
                # Determine market from exchange
                market = "US"
                if contract.exchange in ("SEHK", "HKFE"):
                    market = "HK"
                elif contract.exchange in ("SSE", "SZSE"):
                    market = "CN"

                self._positions[contract.symbol] = Position(
                    symbol=contract.symbol,
                    quantity=pos.quantity,
                    entry_price=pos.avg_cost,
                    current_price=(
                        pos.market_value / pos.quantity
                        if pos.quantity
                        else pos.avg_cost
                    ),
                    currency=contract.currency,
                    market=market,
                )
                self._positions[contract.symbol].unrealized_pnl = pos.unrealized_pnl

            self._save(force=True)
            logger.info(
                "Portfolio synced: %d positions, cash=%s%.2f",
                len(self._positions),
                currency,
                account.total_cash,
            )
            return True

        except Exception as e:
            logger.error("Sync failed mid-process: %s. Rolling back.", e)
            self._positions = old_positions
            self._cash = old_cash
            return False

    # ── Summary ─────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Full portfolio summary."""
        positions = self.get_position_dicts()
        return {
            "timestamp": datetime.now().isoformat(),
            "nav": self.get_nav(),
            "positions_count": self.position_count,
            "market_value": self.get_market_value(),
            "unrealized_pnl": self.get_unrealized_pnl(),
            "realized_pnl": self.get_realized_pnl(),
            "total_pnl": self.get_total_pnl(),
            "exposure_pct": self.get_exposure_pct(),
            "cash": {
                cur: {
                    "total": acct.total_cash,
                    "available": acct.available(),
                    "unsettled": acct.unsettled_amount(),
                }
                for cur, acct in self._cash.items()
            },
            "sector_exposure": self.get_sector_exposure(),
            "positions": positions,
        }

    # ── Internal ────────────────────────────────────────────────────────

    def _get_cash(self, currency: str) -> CashAccount:
        if currency not in self._cash:
            self._cash[currency] = CashAccount(currency=currency)
        return self._cash[currency]

    def _save(self, force: bool = False):
        """Persist state to DB."""
        if self._db is None:
            return
        now = time.monotonic()
        if not force and now - self._last_save_time < self._save_debounce_sec:
            return
        self._last_save_time = now

        try:
            for sym, pos in self._positions.items():
                self._db.portfolio_set(sym, pos.to_dict())
            # Remove closed positions
            db_positions = self._db.portfolio_get_all()
            for sym in db_positions:
                if sym not in self._positions:
                    self._db.portfolio_remove(sym)
            # Save cash balances
            for cur, acct in self._cash.items():
                self._db.kv_set(f"cash_{cur}", str(acct.total_cash))
        except Exception as e:
            logger.error("Failed to save portfolio: %s", e)

    def _load_from_db(self):
        """Load state from DB."""
        if self._db is None:
            return
        try:
            db_positions = self._db.portfolio_get_all()
            for sym, data in (db_positions or {}).items():
                self._positions[sym] = Position(
                    symbol=sym,
                    quantity=data.get("quantity", 0),
                    entry_price=data.get("entry_price", 0),
                    current_price=data.get("current_price", data.get("entry_price", 0)),
                    currency=data.get("currency", "USD"),
                    market=data.get("market", "US"),
                    sector=data.get("sector", ""),
                    strategy=data.get("strategy", ""),
                    stop_loss=data.get("stop_loss"),
                    take_profit=data.get("take_profit"),
                    highest_price=data.get("highest_price", 0),
                    opened_at=data.get("opened_at", ""),
                    updated_at=data.get("updated_at", ""),
                )
            # Load cash
            for cur in self._cash:
                val = self._db.kv_get(f"cash_{cur}")
                if val:
                    self._cash[cur].total_cash = float(val)

            logger.info("Loaded %d positions from DB", len(self._positions))
        except Exception as e:
            logger.warning("DB load failed: %s", e)
