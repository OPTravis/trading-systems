#!/usr/bin/env python3
"""
AI4Trade Copy Trading - Daemon Script
Monitors followed traders and automatically copies their trades to Binance.

Usage:
    python run_copy_trading.py              # Start copy trading
    python run_copy_trading.py --stop       # Stop copy trading
    python run_copy_trading.py --status     # Check status
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Dict

# Setup path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.ai4trade_client import AI4TradeClient, load_client
from src.ai4trade_subscriber import AI4TradeSubscriber, get_subscriber
from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Global flag for graceful shutdown
running = True


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global running
    logger.info("Received shutdown signal, stopping...")
    running = False


# Default allowlist — only these symbols can be traded via copy signals
DEFAULT_COPY_ALLOWLIST = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "DOTUSDT", "MATICUSDT", "AVAXUSDT",
    "LINKUSDT", "ATOMUSDT", "UNIUSDT", "LTCUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "SEIUSDT",
}


def create_trade_callback(binance_client: BinanceClient, portfolio: PortfolioManager):
    """Create a trade execution callback"""

    # Track actual bought quantities per symbol
    bought_quantities: Dict[str, float] = {}
    # C2: Thread-safe set to prevent duplicate buys for same symbol
    _pending_buys: set = set()
    # Thread safety lock — protects bought_quantities AND serialises order+portfolio updates
    portfolio_lock = threading.Lock()

    def on_trade_signal(symbol: str, side: str, price: float, quantity: float):
        """
        Execute a copied trade on Binance.

        This is called when AI-Trader subscriber receives a signal from a followed trader.
        """
        global running

        if not running:
            return

        # Normalize symbol (add USDT if needed)
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        # Symbol allowlist — reject unknown/scam tokens
        if symbol not in DEFAULT_COPY_ALLOWLIST:
            logger.warning(f"  ⚠️  Copy signal rejected: {symbol} not in allowlist")
            return

        logger.info(f"📥 Copy signal received: {side.upper()} {quantity} {symbol}")

        try:
            if side.lower() == "buy":
                # ---- Pre-flight risk check (inside lock to prevent TOCTOU) ----
                with portfolio_lock:
                    base = symbol.replace("USDT", "")
                    if symbol in _pending_buys:
                        logger.info(f"  ℹ️  Buy already pending for {symbol}, skipping (TOCTOU guard)")
                        return
                    if base in portfolio.positions:
                        logger.info(f"  ℹ️  Already have position in {base}, skipping")
                        return
                    _pending_buys.add(symbol)

                    # Use free balance only for sizing
                    usdt_free = binance_client.get_free_balance("USDT")
                    if usdt_free <= 0:
                        logger.error(f"  ❌ No free USDT balance")
                        return

                    copy_pct = 0.20  # 20% of free wallet per trade
                    max_amount = usdt_free * copy_pct
                    scaled_qty = max_amount / price if price > 0 else 0

                    if scaled_qty <= 0:
                        logger.error(f"  ❌ Insufficient balance (${usdt_free:.2f})")
                        return

                    # Pre-flight: validate against portfolio risk limits
                    try:
                        portfolio.add_position(
                            symbol=base,
                            quantity=scaled_qty,
                            entry_price=price,
                            strategy="AICopy",
                            deduct_cash=False,
                            _dry_run=True,
                        )
                    except ValueError as e:
                        logger.warning(f"  ⚠️  Pre-flight check failed: {e}")
                        return

                # Execute market buy (outside lock — network call)
                try:
                    result = binance_client.place_market_buy(symbol, scaled_qty)
                    if result:
                        logger.info(f"  ✅ Buy order placed: {result.get('orderId')}")
                        actual_qty = scaled_qty
                        fills = result.get("fills", [])
                        if fills:
                            actual_qty = sum(float(f.get("qty", 0)) for f in fills)
                        with portfolio_lock:
                            bought_quantities[symbol] = actual_qty
                            portfolio.add_position(
                                symbol=base,
                                quantity=actual_qty,
                                entry_price=price,
                                strategy="AICopy",
                                deduct_cash=False,  # Binance already debited USDT
                            )
                    else:
                        logger.error(f"  ❌ Buy order failed")
                finally:
                    with portfolio_lock:
                        _pending_buys.discard(symbol)

            elif side.lower() == "sell":
                base_symbol = symbol.replace("USDT", "")
                with portfolio_lock:
                    if base_symbol not in portfolio.positions:
                        logger.info(f"  ℹ️  No position to sell for {symbol}")
                        return
                    sell_qty = bought_quantities.get(symbol, quantity)

                logger.info(f"  💤 Selling {sell_qty} {symbol} (stored actual qty, leader said {quantity})")
                result = binance_client.place_market_sell(symbol, sell_qty)
                if result:
                    logger.info(f"  ✅ Sell order placed: {result.get('orderId')}")
                    with portfolio_lock:
                        bought_quantities.pop(symbol, None)
                        portfolio.close_position(base_symbol)
                else:
                    logger.error(f"  ❌ Sell order failed")
            else:
                logger.warning(f"  ⚠️  Unknown side: {side}")

        except Exception as e:
            logger.error(f"  ❌ Error executing copy trade: {e}")

    return on_trade_signal


def check_and_follow_top_traders(client: AI4TradeClient):
    """Check following list and follow top traders if not already following"""
    following = client.get_following()
    following_ids = {f.get("leader_id") for f in following}
    
    # Get own agent ID dynamically instead of hardcoding (Fix #3)
    my_id = None
    try:
        me = client.get_me()
        my_id = me.get("id")
        logger.info(f"My agent ID: {my_id}")
    except Exception as e:
        logger.warning(f"Could not fetch own agent ID: {e}")
    
    logger.info(f"Currently following {len(following_ids)} traders: {following_ids}")
    
    # Get top traders
    from src.ai4trade_client import get_top_traders
    top_traders = get_top_traders(limit=10)
    
    # Follow top traders we aren't already following (skip ourselves)
    followed_count = 0
    for trader in top_traders:
        leader_id = trader.get("agent_id")
        name = trader.get("agent_name")
        
        # Skip ourselves (Fix #3: use dynamic ID)
        if my_id and leader_id == my_id:
            continue
        
        # Skip if already following
        if leader_id in following_ids:
            continue
        
        # Follow this trader
        result = client.follow(leader_id)
        if result and result.get("success"):
            logger.info(f"  ✅ Followed {name} (ID: {leader_id})")
            followed_count += 1
        else:
            logger.warning(f"  ⚠️  Could not follow {name}: {result}")
    
    if followed_count > 0:
        logger.info(f"New traders followed: {followed_count}")


def main():
    parser = argparse.ArgumentParser(description="AI4Trade Copy Trading")
    parser.add_argument("--stop", action="store_true", help="Stop copy trading")
    parser.add_argument("--status", action="store_true", help="Check status")
    args = parser.parse_args()
    
    if args.stop:
        logger.info("Stopping copy trading...")
        from src.ai4trade_subscriber import stop_copy_trading
        stop_copy_trading()
        logger.info("Copy trading stopped")
        return
    
    if args.status:
        # Check status
        client = load_client()
        if not client:
            logger.error("No AI4Trade client available")
            return
        
        me = client.get_me()
        print(f"\n🤖 Agent: {me.get('name')} (ID: {me.get('id')})")
        print(f"💰 Cash: ${me.get('cash', 0):,.2f}")
        print(f"⭐ Points: {me.get('points', 0)}")
        
        following = client.get_following()
        print(f"\n👥 Following: {len(following)} traders")
        for f in following:
            print(f"   - {f.get('leader_name')} (ID: {f.get('leader_id')})")
        
        # Check subscriber
        sub = get_subscriber()
        if sub:
            copied = sub.get_copied_trades()
            print(f"\n📥 Copied trades: {len(copied)}")
            for t in copied[-5:]:
                print(f"   {t.side.upper()} {t.quantity} {t.symbol} @ {t.price} - {t.status}")
        return
    
    # Start copy trading
    global running
    running = True
    
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("="*60)
    logger.info("🚀 Starting AI4Trade Copy Trading")
    logger.info("="*60)
    
    # Initialize clients
    client = load_client()
    if not client:
        logger.error("Failed to load AI4Trade client")
        return
    
    # Check and follow top traders if not already following
    check_and_follow_top_traders(client)
    
    # Initialize Binance
    binance_client = BinanceClient(testnet=False)
    logger.info("✅ Binance client initialized")
    
    # Initialize portfolio
    portfolio = PortfolioManager()
    logger.info("✅ Portfolio manager initialized")
    
    # Create trade callback
    trade_callback = create_trade_callback(binance_client, portfolio)
    
    # Initialize subscriber
    subscriber = AI4TradeSubscriber(
        client=client,
        settings={
            "enabled": True,
            "max_position_pct": 10,      # Max 10% per trade
            "max_total_copy_pct": 30,   # Max 30% in copied trades
            "copy_sell_first": True,    # When leader sells, we sell
        },
        trade_callback=trade_callback
    )
    
    logger.info("✅ Copy trading subscriber initialized")
    
    # Start subscriber
    subscriber.start()
    logger.info("✅ Copy trading started!")
    logger.info("📡 Polling for signals from followed traders...")
    logger.info("Press Ctrl+C to stop")
    
    # Keep running
    poll_count = 0
    while running:
        time.sleep(1)
        poll_count += 1
        
        # Log heartbeat every 60 seconds
        if poll_count % 60 == 0:
            logger.info(f"💓 Heartbeat - Copy trading active")
            poll_count = 0
    
    # Cleanup
    subscriber.stop()
    logger.info("Copy trading stopped")


if __name__ == "__main__":
    main()
