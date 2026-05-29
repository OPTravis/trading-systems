"""
Prometheus Metrics Exporter for Crypto AI Trading System.

Exposes trading system metrics via HTTP for Prometheus scraping.
Uses prometheus_client library with singleton pattern.
"""

import threading
from typing import Optional

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    start_http_server,
)


class MetricsExporter:
    """Thread-safe Prometheus metrics exporter for the trading system."""

    def __init__(self) -> None:
        # Portfolio metrics
        self.portfolio_total_value_usdt = Gauge(
            "portfolio_total_value_usdt",
            "Total portfolio value in USDT",
        )
        self.portfolio_cash_usdt = Gauge(
            "portfolio_cash_usdt",
            "Portfolio cash in USDT",
        )
        self.portfolio_positions_count = Gauge(
            "portfolio_positions_count",
            "Number of open positions",
        )

        # Position PnL per symbol
        self.position_pnl_pct = Gauge(
            "position_pnl_pct",
            "Position PnL percentage",
            ["symbol"],
        )

        # Trade counters
        self.trades_total = Counter(
            "trades_total",
            "Total number of trades",
            ["side"],
        )
        # Gauge (not Counter) because PnL can be negative (losses).
        self.trades_pnl_total_usdt = Gauge(
            "trades_pnl_total_usdt",
            "Cumulative trade PnL in USDT",
        )

        # Trailing stop metrics
        self.trailing_stop_active = Gauge(
            "trailing_stop_active",
            "Whether trailing stop is active (1=yes, 0=no)",
            ["symbol"],
        )
        self.trailing_stop_sl_price = Gauge(
            "trailing_stop_sl_price",
            "Trailing stop loss price",
            ["symbol"],
        )

        # Risk metrics
        self.daily_loss_tier = Gauge(
            "daily_loss_tier",
            "Current daily loss tier (0-3)",
        )
        self.drawdown_pct = Gauge(
            "drawdown_pct",
            "Current drawdown percentage",
        )
        self.drawdown_level = Gauge(
            "drawdown_level",
            "Current drawdown level",
        )

        # HMM regime
        self.hmm_regime = Gauge(
            "hmm_regime",
            "HMM detected market regime",
            ["regime"],
        )

        # Circuit breaker
        self.circuit_breaker_tripped = Gauge(
            "circuit_breaker_tripped",
            "Whether circuit breaker is tripped (1=yes, 0=no)",
        )

        # Trade latency histogram
        self.trade_latency_seconds = Histogram(
            "trade_latency_seconds",
            "Time from signal to execution in seconds",
            buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        )

        # GARCH volatility
        self.garch_volatility = Gauge(
            "garch_volatility",
            "GARCH estimated volatility",
            ["symbol"],
        )

        # Bandit recommendation
        self.bandit_recommendation = Gauge(
            "bandit_recommendation",
            "Bandit algorithm recommendation value",
            ["context"],
        )

        self._server: Optional[object] = None

    # ── Public API ──────────────────────────────────────────────────────

    def start_server(self, port: int = 8000) -> None:
        """Start the Prometheus HTTP metrics server in a background thread."""
        if self._server is not None:
            return  # already running
        self._server = start_http_server(port, addr="0.0.0.0")

    def stop_server(self) -> None:
        """Shutdown the metrics server if running."""
        if self._server is not None:
            server, _thread = self._server
            server.shutdown()
            self._server = None

    def update_portfolio_metrics(
        self,
        total_value: float,
        cash: float,
        positions_count: int,
    ) -> None:
        self.portfolio_total_value_usdt.set(total_value)
        self.portfolio_cash_usdt.set(cash)
        self.portfolio_positions_count.set(positions_count)

    def update_position_pnl(self, symbol: str, pnl_pct: float) -> None:
        self.position_pnl_pct.labels(symbol=symbol).set(pnl_pct)

    def record_trade(self, side: str, pnl_usdt: float) -> None:
        self.trades_total.labels(side=side).inc()
        self.trades_pnl_total_usdt.inc(pnl_usdt)

    def update_trailing_stop(
        self, symbol: str, active: bool, sl_price: float
    ) -> None:
        self.trailing_stop_active.labels(symbol=symbol).set(1 if active else 0)
        self.trailing_stop_sl_price.labels(symbol=symbol).set(sl_price)

    def update_risk_metrics(
        self,
        daily_tier: int,
        drawdown_pct: float,
        drawdown_level: float,
    ) -> None:
        self.daily_loss_tier.set(daily_tier)
        self.drawdown_pct.set(drawdown_pct)
        self.drawdown_level.set(drawdown_level)

    def update_hmm_regime(self, regime: str) -> None:
        # Reset all known regimes, then set the active one
        for r in ("bull", "bear", "sideways", "volatile"):
            self.hmm_regime.labels(regime=r).set(1 if r == regime else 0)

    def update_circuit_breaker(self, tripped: bool) -> None:
        self.circuit_breaker_tripped.set(1 if tripped else 0)

    def record_trade_latency(self, seconds: float) -> None:
        self.trade_latency_seconds.observe(seconds)

    def update_garch_vol(self, symbol: str, vol: float) -> None:
        self.garch_volatility.labels(symbol=symbol).set(vol)

    def update_bandit(self, context: str, recommendation: float) -> None:
        self.bandit_recommendation.labels(context=context).set(recommendation)


# ── Singleton ───────────────────────────────────────────────────────────

_instance: Optional[MetricsExporter] = None
_lock = threading.Lock()


def get_metrics() -> MetricsExporter:
    """Return the global singleton MetricsExporter instance."""
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = MetricsExporter()
    return _instance


def start_metrics_server(port: int = 8000) -> MetricsExporter:
    """Convenience: get singleton and start server."""
    m = get_metrics()
    m.start_server(port)
    return m
