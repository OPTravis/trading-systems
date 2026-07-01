#!/usr/bin/env python3
"""
Verification script for the Prometheus Metrics Exporter.

Starts a server on port 8001, updates every metric type,
fetches /metrics, and checks that expected metric names appear.
"""

import sys
import time
import urllib.request

# Add project src to path
sys.path.insert(0, "/home/travis/crypto-ai-trader")

from src.metrics_exporter import MetricsExporter, get_metrics

TEST_PORT = 8001
BASE_URL = f"http://localhost:{TEST_PORT}"

REQUIRED_METRICS = [
    "portfolio_total_value_usdt",
    "portfolio_cash_usdt",
    "portfolio_positions_count",
    "position_pnl_pct",
    "trades_total",
    "trades_pnl_total_usdt",
    "trailing_stop_active",
    "trailing_stop_sl_price",
    "daily_loss_tier",
    "drawdown_pct",
    "drawdown_level",
    "hmm_regime",
    "circuit_breaker_tripped",
    "trade_latency_seconds",
    "garch_volatility",
    "bandit_recommendation",
]


def main() -> int:
    exporter = get_metrics()

    try:
        print(f"[1] Starting metrics server on port {TEST_PORT} ...")
        exporter.start_server(TEST_PORT)
        time.sleep(1)  # let the server bind

        print("[2] Updating all metric types ...")
        exporter.update_portfolio_metrics(total_value=15000.50, cash=3200.0, positions_count=5)

        exporter.update_position_pnl("BTCUSDT", 2.5)
        exporter.update_position_pnl("ETHUSDT", -1.2)

        exporter.record_trade("buy", 150.0)
        exporter.record_trade("sell", -30.0)

        exporter.update_trailing_stop("BTCUSDT", active=True, sl_price=62000.0)
        exporter.update_trailing_stop("ETHUSDT", active=False, sl_price=3100.0)

        exporter.update_risk_metrics(daily_tier=2, drawdown_pct=5.3, drawdown_level=0.45)

        exporter.update_hmm_regime("bull")

        exporter.update_circuit_breaker(tripped=False)

        exporter.record_trade_latency(0.045)
        exporter.record_trade_latency(0.120)

        exporter.update_garch_vol("BTCUSDT", 0.035)
        exporter.update_garch_vol("ETHUSDT", 0.048)

        exporter.update_bandit("market_trend", 0.78)
        exporter.update_bandit("volatility_regime", 0.22)

        time.sleep(0.5)  # flush

        print(f"[3] Fetching {BASE_URL}/metrics ...")
        with urllib.request.urlopen(f"{BASE_URL}/metrics", timeout=5) as resp:
            body = resp.read().decode("utf-8")

        print("[4] Checking required metric names ...")
        missing = []
        for name in REQUIRED_METRICS:
            if name not in body:
                missing.append(name)

        if missing:
            print(f"FAIL — missing metrics: {missing}")
            return 1

        print("[5] Stopping server ...")
        exporter.stop_server()

        print(f"\n✓ All {len(REQUIRED_METRICS)} metric groups verified.")
        return 0

    except Exception as exc:
        print(f"FAIL — {exc}")
        try:
            exporter.stop_server()
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
