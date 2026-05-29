from src.binance_client import BinanceClient
c = BinanceClient()
acc = c.client.account()
for bal in acc['balances']:
    f,l = float(bal['free']),float(bal['locked'])
    if f+l>0 and bal['asset']!='USDT':
        sym = bal['asset']+'USDT'
        val = (f+l)*float(c.get_24hr_stats(sym)['last_price'])
        if val > 1:
            orders = c.get_open_orders(sym)
            sl = [o for o in orders if 'STOP' in o['type']]
            tp = [o for o in orders if 'LIMIT' in o['type'] or 'MAKER' in o['type']]
            print(f"{bal['asset']}: {(f+l):.4f} ${val:.2f} SL={len(sl)} TP={len(tp)}")
print('USDT:', c.get_free_balance('USDT'))
