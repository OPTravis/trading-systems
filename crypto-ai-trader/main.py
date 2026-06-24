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
    python main.py sync-outcomes     # Sync trade outcomes with portfolio state
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
            sl_count = sum(1 for o in open_orders if _is_stop_order(o))
            tp_count = sum(1 for o in open_orders if 'LIMIT' in o.get('type', '').upper())
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

        # Fund Flow Audit summary (from Binance API FIFO PnL)
        try:
            from src.fund_flow_audit import fetch_all_trades, get_current_prices, compute_fifo_pnl
            all_trades = fetch_all_trades()
            if all_trades:
                prices = get_current_prices()
                audit = compute_fifo_pnl(all_trades, prices)
                lines.append(f"\n📋 資金審計 (FIFO, {audit['num_trades']}筆交易):")
                lines.append(f"  已實現PnL: ${audit['total_realized_pnl']:.2f}")
                lines.append(f"  手續費: -${audit['total_commission']:.2f}")
                lines.append(f"  淨已實現: ${audit['net_realized']:.2f}")
                if audit["unrealized_positions"]:
                    lines.append(f"  塵倉浮虧: ${audit['total_unrealized']:.2f} ({len(audit['unrealized_positions'])}個)")
        except Exception as audit_err:
            logger.warning(f"Fund flow audit in daily report failed: {audit_err}")

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


def cmd_sync_outcomes():
    """Sync trade outcomes with actual portfolio state.
    
    Detects positions that were closed by SL/TP order fills on Binance
    and records them in the trade_outcomes table.
    """
    logger.info("=== Sync Trade Outcomes ===")
    
    try:
        from scripts.sync_trade_outcomes import sync_outcomes
        result = sync_outcomes()
        if result:
            print(f"同步完成: {result.get('synced', 0)} 筆交易已同步")
        else:
            print("同步完成: 無需同步的交易")
    except Exception as e:
        logger.error(f"同步失敗: {e}")
        print(f"❌ 同步失敗: {e}")


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
    elif cmd == "sync-outcomes":
        cmd_sync_outcomes()
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

def _order_qty(o):
    """Get order quantity from either Binance SDK ('origQty') or ccxt ('amount')."""
    return float(o.get('origQty') or o.get('amount') or 0)

def _order_id(o):
    """Get order ID from either Binance SDK ('orderId') or ccxt ('id')."""
    return o.get('orderId') or o.get('id')

def _is_stop_order(o):
    """Check if order is a stop/stop-loss order (case-insensitive for ccxt compat)."""
    t = o.get('type', '')
    return 'STOP' in t.upper() or 'stop' in t.lower()

def cmd_trailing_check():
    """Check open positions and update trailing stop-loss orders.

    Delegates to src.cmd_trailing_check for the actual implementation.
    """
    from src.cmd_trailing_check import cmd_trailing_check as _impl
    _impl()



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
