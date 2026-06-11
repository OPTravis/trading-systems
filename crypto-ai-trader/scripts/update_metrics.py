#!/usr/bin/env python3
"""
Metrics updater — runs every 5 minutes as a cron script (no_agent mode).
Reads current portfolio state and updates Prometheus metrics.
Stdout is SILENT when healthy.
"""
import sys
sys.path.insert(0, str(Path.home() / 'trading-systems' / 'crypto-ai-trader'))

from src.metrics_exporter import start_metrics_server, get_metrics
from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager
from src.risk_manager import RiskManager
from src.circuit_breaker import get_circuit_breaker

def main():
    # Ensure metrics server is running
    start_metrics_server(port=8000)
    
    m = get_metrics()
    c = BinanceClient()
    pm = PortfolioManager()
    rm = RiskManager()
    cb = get_circuit_breaker()
    
    # Portfolio metrics
    positions = pm.get_all_positions()
    cash = pm.get_balance('cash')
    total_value = cash + sum(p.get('position_value', 0) for p in positions)
    m.update_portfolio_metrics(total_value, cash, len(positions))
    
    # Per-position PnL
    for p in positions:
        sym = p['symbol']
        try:
            price = c.get_ticker_price(symbol=sym)
            entry = p['entry_price']
            pnl_pct = ((price - entry) / entry * 100) if entry else 0
            m.update_position_pnl(sym, pnl_pct)
        except Exception:
            pass
    
    # Risk metrics
    try:
        dd_status = rm.drawdown_breaker.get_status() if rm.drawdown_breaker else {'current_drawdown_pct': 0}
    except Exception:
        dd_status = {'current_drawdown_pct': 0}
    m.update_risk_metrics(
        daily_tier=0,
        drawdown_pct=dd_status.get('current_drawdown_pct', 0),
        drawdown_level=0
    )
    
    # Circuit breaker
    m.update_circuit_breaker(1 if cb.is_tripped() else 0)
    
    # Trailing stops
    ts_all = rm.trailing_stop.get_all()
    for sym, ts_data in ts_all.items():
        m.update_trailing_stop(sym, ts_data.get('activated', False), ts_data.get('sl_price', 0))

if __name__ == '__main__':
    main()
