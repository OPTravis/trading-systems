#!/usr/bin/env python3
"""
Standalone runner for Prometheus Metrics Exporter.
Runs a background thread that pulls live data from Binance every 60s.
Prometheus scrapes /metrics every 30s — data stays fresh.
"""
import sys
import os
import signal
import time
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load env from crypto-secrets.env (has 'export' prefixes, parse manually)
_env_file = os.path.expanduser('~/crypto-ai-trader/crypto-secrets.env')
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                line = line.removeprefix('export ')
                if '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

from src.metrics_exporter import start_metrics_server, get_metrics

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('metrics-runner')


def update_metrics():
    """Pull live data from Binance and update Prometheus metrics."""
    try:
        from src.binance_client import BinanceClient
        from src.portfolio import PortfolioManager
        from src.circuit_breaker import get_circuit_breaker

        m = get_metrics()
        c = BinanceClient()
        pm = PortfolioManager()
        cb = get_circuit_breaker()

        # Portfolio metrics — use Binance account directly (NOT PortfolioManager which caches stale positions)
        try:
            account = c.get_account()
            tickers = c.get_24hr_stats()
            price_map = {t['symbol']: float(t['last_price']) for t in tickers if 'symbol' in t and 'last_price' in t}
            total_value = 0.0
            cash = 0.0
            position_count = 0
            position_syms = []
            for b in account['balances']:
                free = float(b['free'])
                locked = float(b['locked'])
                qty = free + locked
                if qty <= 0:
                    continue
                asset = b['asset']
                if asset == 'USDT':
                    cash = qty
                    total_value += qty
                elif asset in ('NTRN',):
                    total_value += qty * price_map.get(f'{asset}/USDT', 0)  # dust, count in total
                else:
                    price = price_map.get(f'{asset}/USDT', 0)
                    val = qty * price
                    total_value += val
                    if val >= 5.0:
                        position_count += 1
                        position_syms.append((f'{asset}/USDT', qty, price))
        except Exception:
            cash = pm.get_balance('cash')
            total_value = cash + sum(p.get('position_value', 0) for p in pm.get_all_positions())
            position_count = 0
            position_syms = []
        m.update_portfolio_metrics(total_value, cash, position_count)

        # Per-position PnL — derive entry from state.db, compare with live price
        try:
            import sqlite3
            _conn = sqlite3.connect(os.path.expanduser('~/crypto-ai-trader/data/state.db'))
            _conn.row_factory = sqlite3.Row
            _portfolio = {row['symbol']: dict(row) for row in _conn.execute("SELECT * FROM portfolio").fetchall()}
            _conn.close()
        except Exception:
            _portfolio = {}

        for sym, qty, price in position_syms:
            try:
                entry = 0
                # state.db stores symbols as 'WLDUSDT' (no slash), but position_syms
                # uses 'WLD/USDT' (ccxt format). Normalize for lookup.
                _db_sym = sym.replace('/', '')
                if _db_sym in _portfolio:
                    entry = float(_portfolio[_db_sym].get('entry_price', 0) or 0)
                elif sym in _portfolio:
                    entry = float(_portfolio[sym].get('entry_price', 0) or 0)
                if entry > 0:
                    pnl_pct = (price - entry) / entry * 100
                else:
                    pnl_pct = 0
                m.update_position_pnl(sym, pnl_pct)
            except Exception:
                pass

        # Risk metrics (drawdown from DB)
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.expanduser('~/crypto-ai-trader/data/state.db'))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT value FROM kv WHERE key='drawdown_state'").fetchone()
            if row:
                import json
                dd_state = json.loads(row['value'])
                dd_pct = dd_state.get('current_drawdown_pct', 0)
            else:
                dd_pct = 0
            conn.close()
        except Exception:
            dd_pct = 0

        m.update_risk_metrics(daily_tier=0, drawdown_pct=dd_pct, drawdown_level=0)

        # Cumulative PnL from trade_outcomes (closed trades)
        try:
            import sqlite3
            _pnl_conn = sqlite3.connect(os.path.expanduser('~/crypto-ai-trader/data/state.db'))
            _pnl_row = _pnl_conn.execute(
                "SELECT COALESCE(SUM(net_pnl_absolute), 0) FROM trade_outcomes WHERE status='closed'"
            ).fetchone()
            _total_pnl = float(_pnl_row[0]) if _pnl_row else 0.0
            _pnl_conn.close()
            m.trades_pnl_total_usdt.set(_total_pnl)
        except Exception:
            pass

        # Trade counts from trade_outcomes (real trades only, NOT phantom from trades table)
        try:
            import sqlite3
            _tc_conn = sqlite3.connect(os.path.expanduser('~/crypto-ai-trader/data/state.db'))
            _total_buys = _tc_conn.execute(
                "SELECT COUNT(*) FROM trade_outcomes"
            ).fetchone()[0]
            _total_sells = _tc_conn.execute(
                "SELECT COUNT(*) FROM trade_outcomes WHERE status='closed'"
            ).fetchone()[0]
            _tc_conn.close()
            m.trades_total.labels(side='buy')._value.set(float(_total_buys))
            m.trades_total.labels(side='sell')._value.set(float(_total_sells))
        except Exception:
            pass

        # Circuit breaker
        m.update_circuit_breaker(cb.is_tripped())

        # Trailing stops
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.expanduser('~/crypto-ai-trader/data/state.db'))
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT symbol, sl_price, activated FROM trailing_stop').fetchall()
            for r in rows:
                m.update_trailing_stop(r['symbol'], bool(r['activated']), r['sl_price'] or 0)
            conn.close()
        except Exception:
            pass

        log.debug("Metrics updated: %d positions, $%.2f total", position_count, total_value)

    except Exception as e:
        log.warning("Metrics update failed: %s", e)


def metrics_updater_loop():
    """Background thread: update metrics every 60 seconds."""
    while True:
        update_metrics()
        time.sleep(60)


def main():
    m = start_metrics_server(port=8000)
    log.info("Metrics Exporter started on :8000")

    # Initial data load
    update_metrics()

    # Start background updater
    t = threading.Thread(target=metrics_updater_loop, daemon=True)
    t.start()
    log.info("Background updater started (60s interval)")

    def shutdown(sig, frame):
        m.stop_server()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
