#!/usr/bin/env python3
import urllib.request, json, sys

# Fetch Fear & Greed Index
try:
    req = urllib.request.Request('https://api.alternative.me/fng/?limit=7')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        for item in data['data']:
            print(f"F&G: {item['value']} ({item['value_classification']}) - {item['timestamp']}")
except Exception as e:
    print(f'F&G Error: {e}')

# Fetch BTC price from CoinGecko
try:
    req = urllib.request.Request('https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        btc = data['bitcoin']
        print(f"BTC: ${btc['usd']:,.2f} ({btc['usd_24h_change']:+.2f}% 24h)")
except Exception as e:
    print(f'BTC Error: {e}')

# Fetch prices for portfolio symbols
symbols = ['avalanche-2', 'pendle', 'tao']
names = ['AVAX', 'PENDLE', 'TAO']
try:
    ids_str = ','.join(symbols)
    req = urllib.request.Request(f'https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd&include_24hr_change=true')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        for sym, cg_id in zip(names, symbols):
            if cg_id in data:
                price = data[cg_id]['usd']
                change = data[cg_id]['usd_24h_change']
                print(f"{sym}: ${price:.4f} ({change:+.2f}% 24h)")
except Exception as e:
    print(f'Altcoins Error: {e}')
