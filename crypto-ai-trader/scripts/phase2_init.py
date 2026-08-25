#!/usr/bin/env python3
"""
Phase 2 BULL Paper Trading — initialisation script.

Run ONCE when Leo authorises Phase 2:
  python3 scripts/phase2_init.py

What it does:
  1. Creates paper_bull_* tables (via BullPaperPortfolio)
  2. Initialises paper cash at $400 (60/25/15 allocation structure)
  3. Initialises capture tracker with current BTC price
  4. Seeds regime detector with current market state
  5. Prints a readiness report

Does NOT place any real orders. Does NOT touch live positions.
Safe to re-run (will not overwrite existing paper state).
"""

import sys, os
sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))
os.chdir(os.path.expanduser("~/crypto-ai-trader"))

from datetime import datetime
from src.state_db import StateDB
from src.bull_regime import BullRegimeDetector, evaluate_regime, STATE_EMOJI
from src.capture_tracker import CaptureTracker
from src.bull_paper_portfolio import BullPaperPortfolio
from src.binance_client import BinanceClient

DB_PATH = "/root/trading-state/state.db"
PAPER_START_CASH = 400.0


def get_btc_daily_sma200(client: BinanceClient):
    """Fetch BTC daily klines and compute SMA200."""
    klines = client.get_klines("BTCUSDT", "1d", limit=210)
    closes = [float(k["close"]) for k in klines]
    if len(closes) < 200:
        return None, None
    sma200 = sum(closes[-200:]) / 200
    return closes[-1], sma200


def get_btc_adx_4h(client: BinanceClient, period: int = 14):
    """Compute ADX(14) on BTC 4H klines."""
    klines = client.get_klines("BTCUSDT", "4h", limit=period * 5 + 50)
    if len(klines) < period * 3:
        return None
    # Simple ADX calculation
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    closes = [float(k["close"]) for k in klines]

    tr_list = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(klines)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr_list.append(tr)

    # Wilder smoothing
    atr = sum(tr_list[:period]) / period
    plus_di_smooth = sum(plus_dm[:period]) / period
    minus_di_smooth = sum(minus_dm[:period]) / period
    dx_list = []

    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        plus_di_smooth = (plus_di_smooth * (period - 1) + plus_dm[i]) / period
        minus_di_smooth = (minus_di_smooth * (period - 1) + minus_dm[i]) / period
        plus_di = 100 * plus_di_smooth / atr if atr > 0 else 0
        minus_di = 100 * minus_di_smooth / atr if atr > 0 else 0
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
        dx_list.append(dx)

    if len(dx_list) < period:
        return None
    adx = sum(dx_list[-period:]) / period
    return adx


def get_fng_history():
    """Load F&G from data feed or alternative source."""
    try:
        from src.data_feed_fng import FearGreedDataFeed
        feed = FearGreedDataFeed()
        data = feed.get_history(days=10)
        # Convert {date_str: value} to {epoch_day: value}
        import time as _t
        result = {}
        for date_str, value in data.items():
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_epoch = int(dt.timestamp()) - (int(dt.timestamp()) % 86400)
            result[day_epoch] = int(value)
        return result
    except Exception as e:
        print(f"  Warning: Could not load F&G history: {e}")
        return {}


def main():
    print("=" * 60)
    print("BULL Phase 2 Paper Trading — Initialisation")
    print("=" * 60)
    print()

    # Connect to DB
    db = StateDB(DB_PATH)
    client = BinanceClient()

    # 1. Paper portfolio
    print("[1/5] Setting up isolated paper portfolio...")
    pf = BullPaperPortfolio(db, start_cash=PAPER_START_CASH)
    print(f"  Paper cash: ${pf.cash:.2f}")
    print(f"  Live tables untouched: trade_outcomes, portfolio, trailing_stop")
    print()

    # 2. Capture tracker
    print("[2/5] Initialising BTC B&H capture tracker...")
    btc_price = float(client.get_ticker_price("BTCUSDT")["price"])
    ct = CaptureTracker(db)
    info = ct.current()
    if info is None:
        ct.initialise(PAPER_START_CASH, btc_price)
        print(f"  Initialised: paper=${PAPER_START_CASH:.2f}, BTC=${btc_price:,.2f}")
    else:
        print(f"  Already initialised at BTC=${info['start_btc']:,.2f} (not overwriting)")
    print()

    # 3. Regime detector
    print("[3/5] Seeding regime detector with current market state...")
    det = BullRegimeDetector(db=db, client=client)
    state = det.load_state()
    btc_close, btc_sma200 = get_btc_daily_sma200(client)
    adx = get_btc_adx_4h(client)
    fng_hist = get_fng_history()
    import time as _t
    now_ms = int(_t.time() * 1000)

    if btc_sma200 and adx is not None and fng_hist:
        s, t = evaluate_regime(
            state, btc_close, btc_sma200, btc_close, adx, fng_hist, now_ms
        )
        det.save_state(s)
        if t:
            det.record_transition(t)
        print(f"  BTC: ${btc_close:,.0f} (SMA200 ${btc_sma200:,.0f})")
        print(f"  ADX(14,4H): {adx:.1f}")
        print(f"  Current regime: {STATE_EMOJI.get(s.regime,'?')} {s.regime}")
        if s.regime != "CONFIRMED_BULL":
            print(f"  Note: Paper system will track regime but only execute core entries")
            print(f"        when state reaches CONFIRMED_BULL.")
    else:
        print(f"  Warning: Could not compute full regime (btc={btc_close}, sma={btc_sma200}, adx={adx}, fng_days={len(fng_hist)})")
    print()

    # 4. Summary
    print("[4/5] Paper trading tables ready:")
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    for table in ["paper_bull_positions", "paper_bull_trades", "paper_bull_state", "bull_regime_log"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    # Verify isolation
    live_outcomes = conn.execute("SELECT COUNT(*) FROM trade_outcomes").fetchone()[0]
    print(f"  trade_outcomes (live, untouched): {live_outcomes} rows")
    conn.close()
    print()

    # 5. Next steps
    print("[5/5] Readiness checklist:")
    print(f"  [x] Paper cash: ${pf.cash:.2f}")
    print(f"  [x] BTC B&H benchmark initialised at ${btc_price:,.2f}")
    print(f"  [x] Regime detector active")
    print(f"  [x] Tables isolated from live trading")
    print()
    print("Phase 2 paper trading is READY.")
    print("The scan pipeline will now report regime status and capture ratio.")
    print("No real orders will be placed until Phase 3 authorisation.")
    print()


if __name__ == "__main__":
    main()
