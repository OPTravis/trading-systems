
from src.binance_client import BinanceClient
from src.portfolio import PortfolioManager
from src.risk_manager import RiskManager

client = BinanceClient(testnet=False)
pm = PortfolioManager(binance_client=client)

# 強制從 Binance 同步持倉
synced = pm.sync_from_binance(client)
positions = pm.get_all_positions()

if not positions:
    print('NO_POSITIONS')
else:
    total_value = sum(p.get('position_value', 0) for p in positions)
    print(f'Positions: {len(positions)} | Total Value: ${total_value:.2f}')
    for p in positions:
        pnl_pct = p.get('unrealized_pct', 0)
        emoji = 'UP' if pnl_pct >= 0 else 'DOWN'
        print(f'  {emoji} {p["symbol"]}: {p["quantity"]:.4f} @ ${p["entry_price"]:.4f} -> ${p["current_price"]:.4f} | PnL: {pnl_pct:.2f}%')
    
    # Trailing stop check
    triggers = []
    rm = RiskManager(client)
    for pos in positions:
        symbol = pos['symbol']
        price = pos['current_price']
        try:
            klines = client.get_klines(symbol, '1h', 50)
            from src.indicators import Indicators
            atr = Indicators.atr(klines, 14) if len(klines) >= 15 else price * 0.02
        except:
            atr = price * 0.02
        result = rm.trailing_stop.update(symbol, price, atr, pos['entry_price'])
        if result.get('triggered'):
            triggers.append(result)
    
    if triggers:
        print('TRAILING_STOP_TRIGGERED:')
        for t in triggers:
            print(f"  {t['symbol']}: SL=${t['sl_price']:.4f} Price=${t['current_price']:.4f}")
    else:
        print('NO_TRAILING_STOP')
