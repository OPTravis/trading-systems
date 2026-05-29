#!/usr/bin/env python3
"""
Intercept user messages and check if they're trading confirmations.
Run this before processing any message in the main session.

Usage:
    python check_msg.py "YES NOMUSDT"
    python check_msg.py "NOMUSDT"
    python check_msg.py "yes"
    python check_msg.py "check"
    
Exit codes:
    0 = not a confirmation (or no pending)
    1 = is a confirmation -> execute trade
    2 = symbol mismatch (pending is different symbol)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.pending_confirmation import check_confirmation, load_pending, clear_pending
from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def execute_trade(pending: dict) -> dict:
    """Execute the trade from pending confirmation. Returns result dict."""
    symbol = pending["symbol"]
    price = pending["price"]
    strategy = pending["strategy"]
    stop_loss_pct = pending["stop_loss_pct"]
    tp_levels = pending["tp_levels"]
    stop_price = pending["stop_price"]
    max_hold_hours = pending.get("max_hold_hours", 24)
    
    client = BinanceClient(testnet=False)
    
    # Get available USDT balance
    usdt_bal = client.get_free_balance('USDT')
    if usdt_bal < 10:
        return {"success": False, "error": f"Insufficient USDT balance: ${usdt_bal:.2f}"}
    
    # Use 50% of balance, max $50
    invest_amount = min(usdt_bal * 0.5, 50)
    if invest_amount < 5:
        invest_amount = usdt_bal * 0.3
    
    qty = int(invest_amount / price)
    if qty < 100:
        return {"success": False, "error": f"Quantity too small: {qty} (min 100)"}
    
    results = []
    
    # Market buy
    try:
        buy_order = client.place_order(symbol, "BUY", "MARKET", qty)
        results.append(f"BUY: {qty} @ market")
    except Exception as e:
        return {"success": False, "error": f"BUY failed: {e}"}
    
    # Stop loss
    try:
        sl = client.place_order(symbol, "SELL", "STOP_LOSS_LIMIT",
                               qty, price=stop_price, stop_price=stop_price)
        results.append(f"SL: {qty} @ ${stop_price:.6f}")
    except Exception as e:
        results.append(f"SL: FAILED ({e})")
    
    # TP orders
    remaining = qty
    for i, tp in enumerate(tp_levels):
        tp_qty = int(qty * tp["size_pct"] / 100)
        if tp_qty < 100:
            continue
        tp_price = round(price * (1 + tp["pct"] / 100) / 0.00001) * 0.00001
        
        try:
            tpo = client.place_order(symbol, "SELL", "LIMIT", tp_qty, price=tp_price)
            results.append(f"TP{i+1}(+{tp['pct']}%): {tp_qty} @ ${tp_price:.6f}")
            remaining -= tp_qty
        except Exception as e:
            results.append(f"TP{i+1}: FAILED ({e})")
    
    if remaining >= 100:
        results.append(f"Remainder: {remaining} (market sell at TP)")
    
    return {
        "success": True,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "strategy": strategy,
        "orders": results,
        "pending_cleared": clear_pending(symbol)
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: check_msg.py <message>")
        sys.exit(0)
    
    user_input = sys.argv[1]
    
    # Special check command
    if user_input.lower() == "check":
        pending = load_pending()
        if pending:
            print(f"📋 Pending: {pending['symbol']} @ ${pending['price']} [{pending['strategy'].upper()}]")
            print(f"   Signals: {pending.get('reason', '')}")
            print(f"   Saved: {pending['saved_at']} (expires in {pending['ttl_hours']}h)")
        else:
            print("✅ No pending confirmation")
        sys.exit(0)
    
    is_conf, pending, symbol = check_confirmation(user_input)
    
    if not is_conf:
        # Not a confirmation or no pending
        if symbol:
            print(f"❓ Symbol '{symbol}' - no matching pending confirmation")
        sys.exit(0)
    
    if not pending:
        print("❓ No pending confirmation found")
        sys.exit(0)
    
    print(f"🎯 Confirmation matched: {user_input}")
    print(f"📋 Pending: {pending['symbol']} @ ${pending['price']} [{pending['strategy'].upper()}]")
    print(f"   Signals: {pending.get('reason', '')}")
    print(f"⏳ Executing trade...")
    
    result = execute_trade(pending)
    
    if result["success"]:
        print(f"\n✅ Trade executed successfully!")
        for line in result["orders"]:
            print(f"   {line}")
        if result.get("pending_cleared"):
            print(f"🗑 Pending cleared")
        sys.exit(1)  # Exit 1 = was a confirmation, trade executed
    else:
        print(f"❌ Trade failed: {result['error']}")
        sys.exit(2)  # Exit 2 = error


if __name__ == "__main__":
    main()
