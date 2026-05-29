#!/usr/bin/env python3
"""
Handle trading confirmation from user.
Reads the pending confirmation file and executes the trade if user said YES.

Usage:
    python handle_confirmation.py "YES NOMUSDT"
    python handle_confirmation.py check
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.pending_confirmation import load_pending, clear_pending, check_confirmation
from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def execute_trade(pending: dict) -> bool:
    """Execute the trade from pending confirmation."""
    symbol = pending["symbol"]
    original_price = pending["price"]
    strategy = pending["strategy"]
    stop_loss_pct = pending["stop_loss_pct"]
    tp_levels = pending["tp_levels"]
    
    client = BinanceClient(testnet=False)
    portfolio = PortfolioManager(client)
    
    # C1: Fetch LIVE price before executing (original price may be stale)
    try:
        stats = client.get_24hr_stats(symbol)
        live_price = float(stats["last_price"]) if stats else original_price
        if stats and abs(live_price - original_price) / original_price > 0.01:
            print(f"⚡ Price updated: ${original_price} -> ${live_price:.6f} ({(live_price/original_price-1)*100:+.2f}%)")
    except Exception as e:
        print(f"⚠️ Could not fetch live price, using original: {e}")
        live_price = original_price
    
    price = live_price
    stop_price = round(price * (1 - stop_loss_pct / 100), 8)
    
    # Get available USDT balance
    usdt_bal = client.get_free_balance('USDT')
    if usdt_bal < 10:
        print(f"❌ Insufficient USDT balance: ${usdt_bal:.2f}")
        return False
    
    # Calculate position size (use 50% of available balance per trade)
    invest_amount = min(usdt_bal * 0.5, 50)  # Max $50 per trade
    if invest_amount < 5:
        print(f"⚠️ Balance ${usdt_bal:.2f} too low for meaningful trade")
        invest_amount = usdt_bal * 0.3
    
    qty = int(invest_amount / price)
    if qty < 100:
        print(f"❌ Quantity too small: {qty} {symbol} (min 100)")
        return False
    
    print(f"=== Executing {symbol} Trade ===")
    print(f"Price: ${price} | Quantity: {qty} | Amount: ${invest_amount:.2f}")
    print(f"Strategy: {strategy.upper()}")
    print(f"Stop Loss: -{stop_loss_pct}% @ ${stop_price}")
    print(f"TP Levels: {tp_levels}")
    
    # Place market buy
    print(f"\n📝 Placing MARKET BUY order...")
    try:
        buy_order = client.place_order(symbol, "BUY", "MARKET", qty)
        print(f"✅ Buy order placed: {buy_order}")
    except Exception as e:
        print(f"❌ Buy order failed: {e}")
        return False
    
    # Set stop loss
    print(f"\n📝 Setting STOP LOSS...")
    try:
        sl_order = client.place_order(symbol, "SELL", "STOP_LOSS_LIMIT",
                                      qty, price=stop_price, stop_price=stop_price)
        print(f"✅ Stop loss set: {sl_order}")
    except Exception as e:
        print(f"⚠️ Stop loss order failed: {e}")
    
    # Set take profit orders
    remaining_qty = qty
    for i, tp in enumerate(tp_levels):
        tp_qty = int(qty * tp["size_pct"] / 100)
        if tp_qty < 100:
            continue
        tp_price = price * (1 + tp["pct"] / 100)
        
        # Round to tick size
        tick_size = 0.00001
        tp_price = round(tp_price / tick_size) * tick_size
        
        try:
            tp_order = client.place_order(symbol, "SELL", "LIMIT",
                                         tp_qty, price=tp_price)
            print(f"✅ TP{i+1} (+{tp['pct']}%): {tp_qty} @ ${tp_price:.6f} -> order placed")
            remaining_qty -= tp_qty
        except Exception as e:
            print(f"⚠️ TP{i+1} failed: {e}")
    
    # Remaining qty -> market sell when TP levels hit
    if remaining_qty >= 100:
        print(f"📝 Remaining {remaining_qty} will be sold at market when TP levels hit")
    
    print(f"\n✅ Trade execution complete for {symbol}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python handle_confirmation.py <YES SYMBOL|check>")
        sys.exit(1)
    
    user_input = sys.argv[1]
    
    if user_input.lower() == "check":
        # Just check pending, don't execute
        pending = load_pending()
        if pending:
            print(f"📋 Pending confirmation: {pending['symbol']}")
            print(f"   Price: ${pending['price']}")
            print(f"   Strategy: {pending['strategy']}")
            print(f"   Signals: {pending.get('reason', '')}")
            print(f"   Saved: {pending['saved_at']}")
            print(f"   TTL: {pending['ttl_hours']}h")
        else:
            print("✅ No pending confirmation")
        return
    
    # Check if user input is a confirmation
    is_conf, pending, symbol = check_confirmation(user_input)
    
    if not pending:
        print(f"❌ No pending confirmation found. User said: {user_input}")
        sys.exit(0)
    
    if not is_conf:
        pending_symbol = pending.get('symbol', 'UNKNOWN')
        print(f"❌ Symbol mismatch. Pending: {pending_symbol} | User said: {symbol or '(no symbol)'}")
        sys.exit(0)
    
    # Execute the trade
    print(f"✅ Confirmation matched: YES {pending['symbol']}")
    success = execute_trade(pending)
    
    if success:
        clear_pending(pending['symbol'])
        print(f"🗑 Pending confirmation cleared")


if __name__ == "__main__":
    main()
