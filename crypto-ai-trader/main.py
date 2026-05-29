#!/usr/bin/env python3
"""
AI Crypto Trading System - Phase 3: Adaptive Strategy Engine

Usage:
    python main.py scan              # Scan market for opportunities
    python main.py cron-scan         # Phase 3: Scan → Research → Adapt → Execute
    python main.py cron-report       # Daily portfolio report
    python main.py strategy-status   # Show current adapted strategy config
    python main.py trade             # Run trading cycle
    python main.py status            # Show portfolio status
    python main.py sentiment         # Market sentiment analysis
    python main.py analyze <SYM>     # Multi-timeframe technical analysis
    python main.py onchain <SYM>     # On-chain / exchange data analysis
    python main.py backtest          # Run backtest
    python main.py trailing-check    # Update trailing stop-loss orders
    python main.py dust-check        # Auto-convert dust positions (< $1) to USDT/BNB
"""

import sys
import os
import time

# Python 3.11.15 (uv build) removed random.randbits; numpy/pandas need it
import random as _random
if not hasattr(_random, 'randbits'):
    _random.randbits = _random.getrandbits

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.binance_client import BinanceClient
from src.market_scanner import MarketScanner
from src.portfolio import PortfolioManager
from src.position_optimizer import PositionOptimizer
from src.sentiment import SentimentAnalyzer
from src.backtester import Backtester
from src.notifier import FeishuNotifier
from src.pending_confirmation import save_pending, load_pending, clear_pending, check_confirmation
from src.strategies import (
    GridStrategy, DCAStrategy, TrendStrategy,
    RSIStrategy, BollingerStrategy, VWAPStrategy
)

import yaml
import logging
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


from src.trade_executor import execute_auto_trade, get_position_tier, count_active_positions
from src.scan_orchestrator import cmd_scan as _cmd_scan_impl, cmd_cron_scan as _cmd_cron_scan_impl

def cmd_scan(send_notification: bool = False):
    """Scan market for opportunities (delegates to scan_orchestrator)."""
    _cmd_scan_impl(send_notification=send_notification)


def cmd_cron_scan():
    """Phase 3 scan pipeline (delegates to scan_orchestrator)."""
    _cmd_cron_scan_impl()


def cmd_sentiment():
    """Analyze market sentiment"""
    logger.info("=== Sentiment Analysis ===")

    analyzer = SentimentAnalyzer()

    # Market sentiment
    market = analyzer.get_market_sentiment()
    print(f"\n🌐 Market Sentiment:")
    print(f"  Score: {market['sentiment_score']:.2f} ({market['sentiment_label']})")
    print(f"  Fear & Greed: {market['fear_greed']}/100")

    # Coin sentiment
    coins = ["BTC", "ETH", "SOL", "BNB"]
    print(f"\n🪙 Coin Sentiment:")
    for coin in coins:
        try:
            result = analyzer.analyze_coin(f"{coin}USDT")
            print(f"  {coin}: {result['sentiment_label']} ({result['sentiment_score']:.2f})")
        except Exception as e:
            print(f"  {coin}: Error - {e}")


def cmd_analyze(symbol: str = None, timeframes: str = "15m,1h,4h"):
    """Multi-timeframe technical analysis for a symbol.

    Usage: python main.py analyze <SYMBOL> [--timeframe 15m,1h,4h]
    """
    if not symbol:
        print("Usage: python main.py analyze <SYMBOL> [--timeframe 15m,1h,4h]")
        return

    # Normalize symbol
    if not symbol.upper().endswith("USDT"):
        symbol = f"{symbol.upper()}USDT"
    else:
        symbol = symbol.upper()

    logger.info(f"=== Multi-Timeframe Analysis: {symbol} ===")
    client = BinanceClient()

    from src.multi_timeframe import MultiTimeframeAnalyzer
    analyzer = MultiTimeframeAnalyzer(client)
    result = analyzer.analyze(symbol)

    if not result or result.get("trend_score") is None:
        print(f"❌ No data available for {symbol}")
        return

    # Display results
    print(f"\n📈 Multi-Timeframe Analysis: {symbol}")
    print(f"  Alignment: {result.get('trend_alignment', 'N/A')}")
    print(f"  Score: {result.get('trend_score', 0):.1f}/100")
    entry = result.get("entry_signal")
    print(f"  Entry Signal: {entry.upper() if entry else 'NONE'}")

    # Timeframe breakdown
    for tf_key in ["tf_4h", "tf_1h", "tf_15m"]:
        data = result.get(tf_key)
        if not data:
            continue
        label = tf_key.replace("tf_", "")
        trend = data.get("trend", "N/A")
        rsi = data.get("rsi", 0)
        macd_hist = data.get("macd_histogram", 0)
        price = data.get("current_price", 0)
        print(f"  {label}: trend={trend} RSI={rsi:.1f} MACD_hist={macd_hist:+.6f} price={price}")

    # ATR
    atr = result.get("atr_15m")
    if atr:
        print(f"\n📐 ATR (15m): {atr:.6f}")


def cmd_onchain(symbol: str = None):
    """On-chain / exchange data analysis for a symbol.

    Usage: python main.py onchain <SYMBOL>
    Fetches: funding rate, whale activity, taker ratio, open interest, volume trend.
    """
    if not symbol:
        print("Usage: python main.py onchain <SYMBOL>")
        return

    coin = symbol.upper().replace("USDT", "")
    logger.info(f"=== On-Chain Analysis: {coin}USDT ===")
    client = BinanceClient()

    from src.market_researcher import MarketResearcher
    researcher = MarketResearcher()
    result = researcher._research_onchain(coin, client)

    if not result:
        print(f"❌ No on-chain data available for {coin}")
        return

    print(f"\n⛓️ On-Chain / Exchange Data: {coin}USDT")
    print(f"  Whale Activity:  {result.get('whale_activity', 'N/A')}")
    print(f"  Exchange Flow:   {result.get('exchange_flow', 'N/A')}")
    print(f"  Volume Trend:    {result.get('volume_trend', 'N/A')}")

    fr = result.get("funding_rate")
    if fr is not None:
        print(f"  Funding Rate:    {fr:+.4f}%")
        if abs(fr) > 0.05:
            print(f"    ⚠️  Elevated — {'longs paying' if fr > 0 else 'shorts paying'}")
    else:
        print(f"  Funding Rate:    N/A (no futures)")

    oi = result.get("oi_change")
    if oi:
        print(f"  Open Interest:   {oi.get('openInterest', 'N/A')} {oi.get('symbol', '')}")

    top_long = result.get("top_trader_long_pct")
    top_short = result.get("top_trader_short_pct")
    if top_long is not None:
        print(f"  Top Traders:     {top_long:.1f}% long / {top_short:.1f}% short")

    taker = result.get("taker_ratio")
    if taker is not None:
        print(f"  Taker Buy/Sell:  {taker:.3f}")


def _sync_from_binance(portfolio, client):
    """Sync positions and cash from Binance API to local state.

    Delegates to the canonical PortfolioManager.sync_from_binance() method
    which has proper rollback, audit logging, and phantom-trade protection.
    """
    portfolio.sync_from_binance(client)


def cmd_status():
    """Show portfolio status"""
    logger.info("=== Portfolio Status ===")

    portfolio = PortfolioManager()
    client = BinanceClient(testnet=False)

    # Sync positions from Binance API (source of truth)
    _sync_from_binance(portfolio, client)

    # Update balances
    for symbol, pos in list(portfolio.positions.items()):
        klines = client.get_klines(symbol, "1h", limit=1)
        if klines:
            current_price = klines[0]["close"]
            portfolio.update_position_price(symbol, current_price)

    # Update cash
    portfolio.update_balance(client.get_balance("USDT"))

    # Get summary
    summary = portfolio.get_summary()

    print(f"\n💼 Portfolio Summary:")
    print(f"  Total Value: ${summary['total_value']:.2f}")
    print(f"  Cash: ${summary['cash']:.2f}")
    print(f"  Exposure: ${summary['total_exposure']:.2f}")
    print(f"  Total PnL: ${summary['total_pnl']:.2f}")
    print(f"  Positions: {summary['positions_count']}")

    if summary['positions']:
        print(f"\n📊 Positions:")
        for pos in summary['positions']:
            print(f"  {pos['symbol']}: {pos['quantity']:.4f} @ ${pos['entry_price']:.4f}")
            print(f"    Current: ${pos['current_price']:.4f} | PnL: {pos['pnl_pct']:.2f}%")


def cmd_backtest():
    """Run backtest"""
    logger.info("=== Backtest ===")

    backtester = Backtester(initial_capital=10000)

    # Compare strategies on BTC
    print("\n📊 Comparing strategies on BTCUSDT (30 days, 1h)...")
    results = backtester.compare_strategies("BTCUSDT", "1h", 30)

    print(f"\nStrategy Comparison:")
    for name, res in results["strategies"].items():
        print(f"\n  {name.upper()}:")
        print(f"    Return: {res['total_return_pct']:.2f}%")
        print(f"    Trades: {res['total_trades']}")
        print(f"    Win Rate: {res['win_rate']:.1f}%")


def cmd_trade():
    """Run trading cycle"""
    logger.info("=== Trading Cycle ===")

    client = BinanceClient(testnet=False)
    scanner = MarketScanner(client)
    portfolio = PortfolioManager()

    # Sync with Binance before trading
    _sync_from_binance(portfolio, client)

    print("Scanning market...")
    opportunities = scanner.scan_all()

    if not opportunities:
        print("No opportunities found.")
        return

    print(f"Top opportunity: {opportunities[0]['symbol']} (Score: {opportunities[0]['score']:.0f})")
    print(f"Signals: {opportunities[0]['signals'][:3]}")


def cmd_cron_report():
    """Run daily report - based on real Binance account data"""
    logger.info("=== Daily Report ===")

    notifier = FeishuNotifier()
    client = BinanceClient(testnet=False)

    try:
        acct = client.get_account()
        usdt_bal = 0
        positions = []

        for b in acct['balances']:
            free = float(b['free']) + float(b['locked'])
            if free <= 0:
                continue
            asset = b['asset']

            if asset == 'USDT':
                usdt_bal = free
                continue

            sym = asset + 'USDT'
            try:
                stats = client.get_24hr_stats(sym)
                if not stats:
                    continue
                price = float(stats.get('last_price', 0))
                change_pct = float(stats.get('price_change_pct', 0))
                value = free * price
                if value >= 1.0:  # Only show positions worth >= $1
                    positions.append({
                        'asset': asset,
                        'qty': free,
                        'price': price,
                        'change_pct': change_pct,
                        'value': value
                    })
            except Exception:
                pass

        # Open orders
        open_orders = client.get_open_orders()

        total_value = usdt_bal + sum(p['value'] for p in positions)

        # Build report
        lines = [f"📊 每日报告 {datetime.now().strftime('%Y-%m-%d')}\n"]
        lines.append(f"💰 USDT: ${usdt_bal:.2f}")
        lines.append(f"📈 總資產: ${total_value:.2f}")

        if positions:
            lines.append(f"\n📦 持倉 ({len(positions)}):")
            for p in sorted(positions, key=lambda x: -x['value']):
                arrow = "🔴" if p['change_pct'] < 0 else "🟢"
                lines.append(f"  {arrow} {p['asset']}: {p['qty']:.1f} (${p['value']:.2f}, {p['change_pct']:+.2f}%)")
        else:
            lines.append("\n📦 無持倉")

        if open_orders:
            sl_count = sum(1 for o in open_orders if 'STOP' in o['type'])
            tp_count = sum(1 for o in open_orders if 'LIMIT' in o['type'])
            lines.append(f"\n📋 掛單: {len(open_orders)} 個 (SL:{sl_count} TP:{tp_count})")

        # Rebalancing suggestions
        if total_value > 0 and positions:
            lines.append("\n⚖️ 再平衡建議:")
            max_single_pct = 40.0  # max % in single position
            for p in sorted(positions, key=lambda x: -x['value']):
                pct = (p['value'] / total_value) * 100
                if pct > max_single_pct:
                    excess = p['value'] - (total_value * max_single_pct / 100)
                    lines.append(f"  ⚠️ {p['asset']} 佔 {pct:.1f}% > {max_single_pct}% → 建議賣出 ${excess:.2f}")
                elif pct > 25:
                    lines.append(f"  ℹ️ {p['asset']} 佔 {pct:.1f}%（偏高，考慮部分獲利）")

            # Cash reserve check
            cash_pct = (usdt_bal / total_value) * 100
            if cash_pct < 20:
                lines.append(f"  ⚠️ 現金僅 {cash_pct:.1f}% < 20% → 考慮減倉補現金")
            elif cash_pct > 60:
                lines.append(f"  ℹ️ 現金 {cash_pct:.1f}% > 60% → 可考慮加倉")

            # Profit-taking opportunities (positions up > 10%)
            for p in positions:
                if p['change_pct'] > 10:
                    lines.append(f"  🎯 {p['asset']} 24h +{p['change_pct']:.1f}% → 考慮鎖利")

        notifier.send_text("\n".join(lines))
        logger.info("Daily report sent")

    except Exception as e:
        logger.error(f"Daily report failed: {e}")
        notifier.send_text(f"⚠️ 每日報告生成失敗: {e}")


def cmd_auto_dust():
    """Automatically convert dust positions (< $1 value, free balance only) to USDT or BNB.

    Strategy:
    1. Find all non-USDT assets with free balance worth < $1
    2. Skip delisted/unsupported assets (NTRN etc)
    3. Try Convert API (to USDT) first for each asset
    4. For remaining: batch dust transfer to BNB (rate limit: 1/hr)
    """
    import json as _json

    DUST_THRESHOLD_USD = 1.0
    SKIP_ASSETS = {'NTRN', 'LDBNB', 'BETH'}

    client = BinanceClient(testnet=False)

    try:
        acct = client.get_account()
    except Exception as e:
        print(_json.dumps({"action": "error", "reason": f"account_fetch_failed: {e}"}))
        return

    # Find dust (free balance worth < $1)
    dust_assets = []
    for b in acct['balances']:
        asset = b['asset']
        free = float(b['free'])
        if asset in ('USDT', 'BNB') or free <= 0 or asset in SKIP_ASSETS:
            continue

        symbol = f"{asset}USDT"
        try:
            price = client.get_ticker_price(symbol)
            if not price:
                continue
            value_usd = free * price
        except Exception:
            value_usd = 0

        if value_usd < DUST_THRESHOLD_USD:
            dust_assets.append({"asset": asset, "free": free, "value_usd": round(value_usd, 4)})

    if not dust_assets:
        print(_json.dumps({"action": "none", "reason": "no_dust"}))
        return

    results = []
    converted = set()

    # Phase 1: Convert API (asset -> USDT)
    for d in dust_assets:
        asset, free = d['asset'], d['free']
        try:
            pairs = client.list_all_convert_pairs(fromAsset=asset, toAsset='USDT')
            if not pairs:
                continue
            min_amt = float(pairs[0].get('fromAssetMinAmount', 0))
            if free < min_amt:
                continue

            quote = client.convert_get_quote(
                fromAsset=asset, toAsset='USDT', fromAmount=str(free),
            )
            qid = quote.get('quoteId')
            to_amt = quote.get('toAmount', '0')

            if qid:
                accept = client.convert_accept_quote(quoteId=qid)
                to_amt = accept.get('toAmount', to_amt)

            results.append({"asset": asset, "action": "converted", "from": free, "to_usdt": to_amt})
            converted.add(asset)
            logger.info(f"Dust auto-convert: {free} {asset} -> {to_amt} USDT")
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"Dust convert failed for {asset}: {e}")
            time.sleep(0.3)

    # Phase 2: Dust transfer to BNB for remaining
    remaining = [d['asset'] for d in dust_assets if d['asset'] not in converted]
    if remaining:
        try:
            conv_info = client.bnb_convertible_assets()
            conv_set = {d['asset'] for d in conv_info.get('details', [])}
            to_transfer = [a for a in remaining if a in conv_set]
            if to_transfer:
                client.transfer_dust(asset=to_transfer)
                results.append({"action": "dust_to_bnb", "assets": to_transfer})
                logger.info(f"Dust transfer to BNB: {to_transfer}")
        except Exception as e:
            logger.debug(f"Dust transfer failed: {e}")

    print(_json.dumps({
        "action": "dust_cleaned",
        "found": len(dust_assets),
        "converted": len(converted),
        "results": results,
    }, default=str, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "scan":
        cmd_scan(send_notification=False)
    elif cmd == "cron-scan":
        cmd_cron_scan()
    elif cmd == "cron-report":
        cmd_cron_report()
    elif cmd == "sentiment":
        cmd_sentiment()
    elif cmd == "status":
        cmd_status()
    elif cmd == "backtest":
        cmd_backtest()
    elif cmd == "trade":
        cmd_trade()
    elif cmd == "trailing-check":
        cmd_trailing_check()
    elif cmd == "dust-check":
        cmd_auto_dust()
    elif cmd == "strategy-status":
        cmd_strategy_status()
    elif cmd == "analyze":
        sym = sys.argv[2] if len(sys.argv) > 2 else None
        tf = "15m,1h,4h"
        if "--timeframe" in sys.argv:
            idx = sys.argv.index("--timeframe")
            if idx + 1 < len(sys.argv):
                tf = sys.argv[idx + 1]
        cmd_analyze(sym, tf)
    elif cmd == "onchain":
        sym = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_onchain(sym)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

def cmd_trailing_check():
    """Check open positions and update trailing stop-loss orders.

    For each held position:
    1. Get current price and ATR
    2. Run TrailingStop.update() to check activation/move
    3. If SL should move up: cancel old SL order, place new one
    4. If trailing triggered: close position immediately

    Outputs JSON result for cron agent to format.
    """
    import json as _json
    from src.indicators import Indicators
    from src.risk_manager import TrailingStop, RiskManager

    client = BinanceClient(testnet=False)
    ts = TrailingStop()
    risk_mgr = RiskManager(binance_client=client)  # single instance for entire check
    notifier = FeishuNotifier()

    # Get non-USDT positions (exclude dust < $1 value)
    acct = client.get_account()
    positions = []
    for b in acct['balances']:
        asset = b['asset']
        free = float(b['free'])
        locked = float(b['locked'])
        total = free + locked
        if asset != 'USDT' and total > 0 and asset not in ('NTRN',):
            # Skip grid-managed symbols (their orders are handled by grid_bot)
            if asset in os.environ.get('GRID_MANAGED_ASSETS', '').split(','):
                continue
            # Filter dust: skip assets with < $1 total value
            symbol = f"{asset}USDT"
            try:
                stats = client.get_24hr_stats(symbol)
                price = float(stats.get('last_price', 0))
                if total * price < 1.0:
                    continue  # dust, skip
            except Exception:
                continue  # can't price, skip
            positions.append({"asset": asset, "symbol": symbol, "free": free, "locked": locked, "total": total})

    if not positions:
        # Clean stale trailing data
        for sym in list(ts.get_all().keys()):
            ts.remove(sym)
        print(_json.dumps({"action": "none", "reason": "no_positions"}))
        return

    results = []

    for pos in positions:
        asset = pos['asset']
        symbol = pos['symbol']
        p_prec = client.get_price_precision(symbol)

        # Get current price
        try:
            stats = client.get_24hr_stats(symbol)
            if not isinstance(stats, dict):
                results.append({"asset": asset, "action": "skip", "reason": "no_price_data"})
                continue
            current_price = float(stats.get('last_price', 0))
            if current_price <= 0:
                results.append({"asset": asset, "action": "skip", "reason": "invalid_price"})
                continue
        except Exception as e:
            results.append({"asset": asset, "action": "skip", "reason": str(e)})
            continue

        # Get ATR from klines
        try:
            klines_raw = client.get_klines(symbol, interval='1h', limit=20)
            if not klines_raw or len(klines_raw) < 15:
                results.append({"asset": asset, "action": "skip", "reason": "insufficient_klines"})
                continue
            # get_klines returns list of dicts
            klines = klines_raw
            atr = Indicators.atr(klines, period=14)
            if atr <= 0:
                results.append({"asset": asset, "action": "skip", "reason": "atr_zero"})
                continue
        except Exception as e:
            results.append({"asset": asset, "action": "skip", "reason": f"atr_error: {e}"})
            continue

        # Get true entry price from trade history (for new tracking or PnL)
        true_entry = None
        if asset not in ts.get_all():
            try:
                from src.entry_price import get_avg_entry_price
                true_entry = get_avg_entry_price(client, symbol, current_qty=pos['total'])
                if true_entry:
                    logger.info(f"True entry price for {asset}: ${true_entry:.6f}")
            except Exception as e:
                logger.warning(f"Cannot get entry price for {asset}: {e}")

        # Update trailing stop state
        update = ts.update(asset, current_price, atr, entry_price=true_entry)

        # IMPORTANT: Check triggered FIRST (triggered result lacks "activated" key)
        # Case 2: Trailing triggered — close position
        if update.get("triggered"):
            logger.warning("TrailingStop TRIGGERED for %s at $%.6f", asset, current_price)
            # Close all orders and sell remaining
            client.cancel_all_orders(symbol)
            qty_to_sell = pos['free']
            sell_ok = False
            if qty_to_sell > 0:
                for sell_attempt in range(3):
                    try:
                        sell_result = client.place_order(symbol, "SELL", "MARKET", qty_to_sell)
                        sell_ok = True
                        notifier.send_text(f"🔴 追蹤止損觸發 {asset}\n賣出 {qty_to_sell} @ ${current_price:.6f}\n最高: ${update['highest_price']:.6f}")
                        results.append({
                            "asset": asset,
                            "action": "trailing_triggered",
                            "price": current_price,
                            "highest": update["highest_price"],
                            "sl_price": update["sl_price"],
                            "sell_qty": qty_to_sell,
                        })
                        break
                    except Exception as e:
                        if sell_attempt < 2:
                            logger.warning(f"Trailing sell attempt {sell_attempt+1} failed: {e}, retrying in 2s...")
                            time.sleep(2)
                        else:
                            notifier.send_text(f"🔴🔴 追蹤止損觸發但賣出失敗 {asset}！手動處理！\n錯誤: {e}")
                            results.append({"asset": asset, "action": "triggered_sell_failed", "error": str(e)})
            else:
                results.append({"asset": asset, "action": "triggered_no_free_balance"})

            # Record PnL for loss guard
            try:
                entry_price = update.get("entry_price", 0)
                if entry_price > 0 and (sell_ok or qty_to_sell == 0):
                    pnl = (current_price - entry_price) * (qty_to_sell if qty_to_sell > 0 else pos['total'])
                    risk_mgr.post_trade_update(asset, pnl)
                    logger.info(f"Post-trade update: {asset} PnL={pnl:.4f} USDT")
            except Exception as e:
                logger.error(f"Failed to record post-trade update for {asset}: {e}")

            ts.remove(asset)
            continue

        # Case 1: Not yet activated
        if not update.get("activated"):
            results.append({
                "asset": asset,
                "price": current_price,
                "atr": round(atr, 6),
                "action": "tracking",
                "activated": False,
            })
            continue

        # Case 3: Trailing active — check if SL needs to move up
        new_sl = update.get("sl_price", 0)
        if new_sl <= 0:
            results.append({"asset": asset, "action": "tracking", "sl_price": 0})
            continue

        # Find existing SL order
        open_orders = client.get_open_orders(symbol)
        sl_orders = [o for o in open_orders if 'STOP' in o.get('type', '')]

        old_sl_price = 0
        sl_moved = False

        if sl_orders:
            sl_order = sl_orders[0]
            old_sl_price = float(sl_order.get('stopPrice', 0) or sl_order.get('price', 0))

            # Only move UP
            if new_sl > old_sl_price * 1.001:  # 0.1% buffer to avoid dust moves
                sl_qty = float(sl_order['origQty'])
                # Cancel old SL
                cancel_result = client.cancel_order(symbol, sl_order['orderId'])
                if cancel_result:
                    # Place new SL
                    new_sl_rounded = round(new_sl, p_prec)
                    new_sl_order = client.place_order(
                        symbol, "SELL", "STOP_LOSS_LIMIT",
                        sl_qty, price=new_sl_rounded, stop_price=new_sl_rounded
                    )
                    if new_sl_order:
                        sl_moved = True
                        logger.info(
                            "TrailingStop SL moved %s: $%.6f → $%.6f",
                            asset, old_sl_price, new_sl_rounded
                        )
                        results.append({
                            "asset": asset,
                            "action": "sl_moved",
                            "old_sl": old_sl_price,
                            "new_sl": new_sl_rounded,
                            "highest": update["highest_price"],
                            "current_price": current_price,
                            "callback_pct": update.get("callback_pct", 0),
                        })
                    else:
                        logger.critical("TrailingStop: failed to place new SL for %s after cancel!", asset)
                        notifier.send_text(f"🔴 SL更新失敗 {asset}！舊SL已取消但新SL未掛上！手動處理！")
                        results.append({"asset": asset, "action": "sl_move_failed", "old_sl": old_sl_price})
                else:
                    logger.warning("TrailingStop: failed to cancel old SL for %s", asset)
                    results.append({"asset": asset, "action": "sl_cancel_failed"})
            else:
                # SL already at or above target
                results.append({
                    "asset": asset,
                    "action": "sl_unchanged",
                    "sl_price": old_sl_price,
                    "target_sl": new_sl,
                    "highest": update["highest_price"],
                    "current_price": current_price,
                })
        else:
            # No SL found — place one
            new_sl_rounded = round(new_sl, p_prec)
            qty_for_sl = pos['free']
            if qty_for_sl >= 1:
                sl_order = client.place_order(
                    symbol, "SELL", "STOP_LOSS_LIMIT",
                    qty_for_sl, price=new_sl_rounded, stop_price=new_sl_rounded
                )
                if sl_order:
                    sl_moved = True
                    results.append({
                        "asset": asset,
                        "action": "sl_created",
                        "new_sl": new_sl_rounded,
                        "qty": qty_for_sl,
                        "highest": update["highest_price"],
                        "current_price": current_price,
                    })
                else:
                    results.append({"asset": asset, "action": "sl_create_failed"})
            else:
                results.append({"asset": asset, "action": "no_free_balance_for_sl"})

    # Check SL coverage for all positions
    for pos in positions:
        asset = pos['asset']
        symbol = pos['symbol']
        total_qty = pos['total']
        free_qty = pos['free']

        open_orders = client.get_open_orders(symbol)
        sl_orders = [o for o in open_orders if 'STOP' in o.get('type', '')]
        tp_orders = [o for o in open_orders if 'STOP' not in o.get('type', '')]
        sl_covered = sum(float(o['origQty']) for o in sl_orders)
        tp_covered = sum(float(o['origQty']) for o in tp_orders)

        uncovered_by_sl = total_qty - sl_covered  # units with no SL protection

        # Case 1: Position fully locked in TP but no SL — cancel lowest TP to make room for SL
        if uncovered_by_sl > 0 and free_qty < uncovered_by_sl and tp_orders:
            # Estimate SL price before canceling TP
            try:
                stats = client.get_24hr_stats(symbol)
                est_price = float(stats.get('last_price', 0)) if stats else 0
            except Exception:
                est_price = 0
            est_sl_price = round(est_price * 0.95, p_prec) if est_price > 0 else 0
            est_notional = uncovered_by_sl * est_sl_price if est_sl_price > 0 else 0

            # If SL notional would be below $5, canceling TP would just waste it — skip
            if est_notional < 5.0 and est_notional > 0:
                logger.info(
                    "Skipping TP cancel for %s: SL notional $%.2f < $5 minimum. "
                    "TP preserved.",
                    asset, est_notional,
                )
            else:
                # All or most units locked in TP with no SL — cancel lowest TP
                lowest_tp = min(tp_orders, key=lambda o: float(o.get('price', 0)))
                cancel_qty = float(lowest_tp['origQty'])
                logger.warning(
                    "No SL for %s (%.4f total, SL covers %.4f, TP locks %.4f). "
                    "Canceling lowest TP (%.4f @ $%s) to place SL.",
                    asset, total_qty, sl_covered, tp_covered,
                    cancel_qty, lowest_tp.get('price'),
                )
                try:
                    cancel_result = client.cancel_order(symbol, lowest_tp['orderId'])
                    if cancel_result:
                        free_qty += cancel_qty  # freed up by cancel
                        uncovered_by_sl = total_qty - sl_covered  # recalculate
                        tp_covered -= cancel_qty
                except Exception as e:
                    logger.error(f"Failed to cancel TP for {asset}: {e}")
                    results.append({
                        "asset": asset, "action": "no_sl_cancel_tp_failed",
                        "error": str(e),
                    })

        # Case 2: Free units available with no SL — place default -5% SL
        # Use qty_to_protect = min(free_qty, uncovered_by_sl) but also check notional minimum
        qty_to_protect = min(free_qty, uncovered_by_sl)
        p_prec = client.get_price_precision(symbol)
        current_price = 0
        try:
            stats = client.get_24hr_stats(symbol)
            current_price = float(stats.get('last_price', 0))
        except Exception:
            pass

        if qty_to_protect <= 0 or current_price <= 0:
            continue

        # Check minimum notional ($5 on Binance)
        notional = qty_to_protect * current_price
        if notional < 5.0:
            results.append({
                "asset": asset, "action": "no_sl_below_notional",
                "qty": qty_to_protect, "value": round(notional, 2),
                "msg": f"價值 ${notional:.2f} < $5 最低掛單門檻",
            })
            continue

        sl_price = round(current_price * 0.95, p_prec)  # -5% default SL
        try:
            sl_result = client.place_order(
                symbol, "SELL", "STOP_LOSS_LIMIT",
                qty_to_protect, price=sl_price, stop_price=sl_price
            )
            if sl_result:
                logger.warning(
                    "SL placed for unprotected position: %s %.4f units @ $%.6f",
                    asset, qty_to_protect, sl_price,
                )
                results.append({
                    "asset": asset,
                    "action": "uncovered_sl_created",
                    "qty": qty_to_protect,
                    "sl_price": sl_price,
                    "current_price": current_price,
                })
            else:
                logger.critical("Failed to place SL for %s %.4f units", asset, qty_to_protect)
                results.append({
                    "asset": asset, "action": "uncovered_sl_failed",
                    "qty": qty_to_protect,
                })
        except Exception as e:
            logger.error("Error placing SL for %s: %s", asset, e)
            results.append({
                "asset": asset, "action": "uncovered_sl_error",
                "qty": qty_to_protect, "error": str(e),
            })

    # Detect SL/TP filled by exchange (position gone but was tracked)
    tracked = ts.get_all()
    for sym in list(tracked.keys()):
        sym_info = tracked[sym]
        # Normalize symbol for comparison (trailing stop uses USDT suffix)
        sym_normalized = sym if sym.endswith("USDT") else sym + "USDT"
        # Find if this asset is still in positions with meaningful balance
        sym_pos = next((p for p in positions if p['asset'] == sym or p['asset'] == sym_normalized or p.get('symbol') == sym_normalized), None)
        if sym_pos is None:
            # Position gone — SL/TP was filled on exchange
            entry_price = sym_info.get('entry_price', 0)
            # Determine exit reason from trailing_stop state
            activated = sym_info.get('activated', False)
            sl_price = sym_info.get('sl_price', 0)
            if activated:
                exit_reason = "trailing"
            elif sl_price > 0 and entry_price > 0:
                exit_reason = "sl"
            else:
                exit_reason = "order_fill"
            if entry_price > 0:
                try:
                    # Get the exit price from recent trades
                    exit_price = 0
                    symbol = sym_normalized
                    trades = client.get_my_trades(symbol=symbol, limit=5)
                    if trades:
                        last_trade = trades[-1]
                        exit_price = float(last_trade.get('price', 0))
                    if exit_price == 0:
                        # Fallback to current market price
                        stats = client.get_24hr_stats(symbol)
                        exit_price = float(stats.get('last_price', 0))
                    if exit_price > 0:
                        qty = sym_pos['total'] if sym_pos else sym_info.get('qty', 0)
                        if qty <= 0:
                            logger.warning(f"Cannot compute PnL for {sym}: no qty available (position gone, not tracked)")
                        else:
                            pnl = (exit_price - entry_price) * qty
                            risk_mgr.post_trade_update(sym, pnl)
                            logger.info(f"Detected SL/TP fill: {sym} entry={entry_price} exit={exit_price} qty={qty} PnL={pnl:.4f}")
                        results.append({
                            "asset": sym,
                            "action": "sltp_filled_detected",
                            "exit_reason": exit_reason,
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "qty": qty,
                            "pnl": round((exit_price - entry_price) * qty, 4) if qty > 0 else 0,
                        })
                except Exception as e:
                    err_str = str(e)
                    # Skip known non-critical errors: invalid symbol, no such order
                    if "-1121" in err_str or "Invalid symbol" in err_str:
                        logger.warning(f"SL/TP fill detection skipped for {sym}: {err_str}")
                    else:
                        logger.error(f"Failed to record SL/TP fill for {sym}: {e}")

    # Clean stale entries (positions no longer held)
    held_assets = {p['asset'] for p in positions}
    for sym in list(ts.get_all().keys()):
        sym_normalized = sym if sym.endswith("USDT") else sym + "USDT"
        if sym_normalized not in held_assets and sym not in held_assets:
            ts.remove(sym)

    print(_json.dumps({"positions": len(positions), "results": results}, default=str, ensure_ascii=False))



def cmd_strategy_status():
    """Show current strategy adaptation status."""
    from src.strategy_adaptor import StrategyAdaptor
    adaptor = StrategyAdaptor()
    cfg = adaptor.get_full_config()
    if not cfg:
        print("策略適配器尚未運行。請先執行 cron-scan 以生成適配配置。")
        return
    print(adaptor.format_report())


if __name__ == "__main__":
    main()
