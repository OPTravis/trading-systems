#!/usr/bin/env python3
"""
AI4Trade Integration - Combines signal publishing and copy trading
Usage:
    python ai4trade_integration.py --publish-only    # Only publish trades
    python ai4trade_integration.py --copy-only      # Only copy trades
    python ai4trade_integration.py --both           # Both (default)
    python ai4trade_integration.py --status         # Check status
    python ai4trade_integration.py --top-traders    # Show top traders
    python ai4trade_integration.py --follow <id>    # Follow a trader
"""

import argparse
import logging
import os
import sys

# Setup path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.ai4trade_client import AI4TradeClient, load_client, get_top_traders
from src.ai4trade_publisher import AI4TradePublisher, publish_buy, publish_sell
from src.ai4trade_subscriber import AI4TradeSubscriber, start_copy_trading, get_subscriber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


def check_status():
    """Check AI4Trade status"""
    print("\n" + "=" * 50)
    print("🤖 AI4Trade Status Check")
    print("=" * 50)
    
    client = load_client()
    if not client:
        print("❌ No AI4Trade client available")
        return
    
    # Get agent info
    me = client.get_me()
    print(f"\n📛 Agent: {me.get('name')} (ID: {me.get('id')})")
    print(f"💰 Cash: ${me.get('cash', 0):,.2f}")
    print(f"⭐ Points: {me.get('points', 0)}")
    print(f"📊 Reputation: {me.get('reputation_score', 0)}")
    
    # Get positions
    positions = client.get_positions()
    print(f"\n📦 Positions: {len(positions)}")
    for pos in positions[:5]:
        print(f"   {pos.get('symbol')}: {pos.get('quantity')} @ ${pos.get('current_price')}")
    
    # Get following
    following = client.get_following()
    print(f"\n👥 Following: {len(following)} traders")
    for f in following[:5]:
        print(f"   {f.get('leader_name')} (ID: {f.get('leader_id')})")
    
    # Get copied trades
    subscriber = get_subscriber()
    if subscriber:
        copied = subscriber.get_copied_trades()
        print(f"\n📥 Copied trades: {len(copied)}")
    
    print()


def show_top_traders(limit: int = 10):
    """Show top traders on the platform"""
    print("\n" + "=" * 50)
    print(f"🏆 Top {limit} Traders on AI4Trade")
    print("=" * 50)
    
    traders = get_top_traders(limit=limit)
    for i, t in enumerate(traders, 1):
        print(f"\n{i}. {t.get('agent_name')}")
        print(f"   ID: {t.get('agent_id')}")
        print(f"   Signals: {t.get('signal_count')}")
        print(f"   Total PnL: {t.get('total_pnl', 0)}")
        print(f"   Last active: {t.get('last_signal_at', 'N/A')[:10]}")
    
    print()


def follow_trader(leader_id: int):
    """Follow a specific trader"""
    print(f"\n👥 Following leader ID: {leader_id}")
    
    client = load_client()
    if not client:
        print("❌ No client available")
        return
    
    result = client.follow(leader_id)
    if result and result.get("success"):
        print(f"✅ Successfully followed leader {leader_id}")
    else:
        print(f"❌ Failed to follow: {result}")


def start_publisher():
    """Start the signal publisher"""
    print("\n📡 Starting AI4Trade Publisher...")
    publisher = AI4TradePublisher()
    
    # Test publish
    print("Testing publish capability...")
    test = publisher.publish_sell(
        symbol="BTCUSDT",
        price=65000,
        quantity=0.001,
        strategy="TEST",
        reason="Integration test",
        pnl=5.0,
        pnl_pct=0.5,
        exit_type="manual"
    )
    
    if test:
        print("✅ Publisher test successful!")
    else:
        print("❌ Publisher test failed (this is normal if token expired)")
    
    return publisher


def start_copy_trader_with_callback():
    """Start copy trading with Binance execution"""
    print("\n📥 Starting AI4Trade Copy Trading...")
    
    # Import Binance client
    try:
        from src.binance_client import BinanceClient
        from src.portfolio import PortfolioManager

        # Initialize Binance client
        binance = BinanceClient(testnet=False)
        
        # Create trade callback
        def on_trade_signal(symbol: str, side: str, price: float, quantity: float):
            """Execute a copied trade on Binance"""
            logger.info(f"Executing copied trade: {side.upper()} {quantity} {symbol}")
            
            if side.lower() == "buy":
                result = binance.place_market_buy(symbol, quantity)
            elif side.lower() == "sell":
                result = binance.place_market_sell(symbol, quantity)
            else:
                logger.warning(f"Unknown side: {side}")
                return
            
            if result:
                logger.info(f"✅ Copied trade executed: {result}")
            else:
                logger.error(f"❌ Copied trade failed")
        
        # Start subscriber
        subscriber = start_copy_trading(trade_callback=on_trade_signal)
        print("✅ Copy trading started!")
        print("   Polling for signals from followed traders...")
        
        return subscriber
        
    except ImportError as e:
        logger.error(f"Could not import Binance client: {e}")
        print("❌ Binance client not available, starting without execution")
        return start_copy_trading()


def main():
    parser = argparse.ArgumentParser(description="AI4Trade Integration")
    parser.add_argument("--status", action="store_true", help="Check status")
    parser.add_argument("--top-traders", action="store_true", help="Show top traders")
    parser.add_argument("--follow", type=int, metavar="ID", help="Follow a trader by ID")
    parser.add_argument("--publish-only", action="store_true", help="Only start publisher")
    parser.add_argument("--copy-only", action="store_true", help="Only start copy trading")
    parser.add_argument("--both", action="store_true", help="Start both (default)")
    
    args = parser.parse_args()
    
    if args.status:
        check_status()
    elif args.top_traders:
        show_top_traders()
    elif args.follow:
        follow_trader(args.follow)
    elif args.publish_only:
        start_publisher()
    elif args.copy_only:
        start_copy_trader_with_callback()
    else:
        # Default: show status and explain options
        check_status()
        print("\nUsage:")
        print("  python ai4trade_integration.py --status        # Check status")
        print("  python ai4trade_integration.py --top-traders   # Show top traders")
        print("  python ai4trade_integration.py --follow <id>   # Follow a trader")
        print("  python ai4trade_integration.py --publish-only  # Start publisher only")
        print("  python ai4trade_integration.py --copy-only     # Start copy trading only")
        print()


if __name__ == "__main__":
    main()
