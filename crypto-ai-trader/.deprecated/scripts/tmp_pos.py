import sys
sys.path.insert(0, "/home/travis/crypto-ai-trader")
from src.binance_client import BinanceClient
c = BinanceClient()
acct = c.client.account()
for b in acct.get('balances', []):
    free = float(b.get('free', 0))
    locked = float(b.get('locked', 0))
    total = free + locked
    asset = b['asset']
    if total > 0 and asset not in ('USDT', 'NTRN'):
        try:
            stats = c.get_24hr_stats(asset + 'USDT')
            price = float(stats.get('last_price', 0))
            value = total * price
            if value >= 1.0:
                print(f'{asset}: total={total}, price={price}, value=${value:.2f}, locked={locked}')
        except Exception as e:
            print(f'{asset}: total={total}, locked={locked}, err={e}')
for b in acct.get('balances', []):
    if b['asset'] == 'USDT':
        usdt_free = b['free']
        print(f'USDT: free={usdt_free}')
