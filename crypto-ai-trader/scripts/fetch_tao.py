#!/usr/bin/env python3
import urllib.request, json
try:
    req = urllib.request.Request('https://api.coingecko.com/api/v3/coins/bittensor?localization=false&tickers=false&community_data=false&developer_data=false')
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        price = data['market_data']['current_price']['usd']
        change = data['market_data']['price_change_percentage_24h']
        print(f'TAO (Bittensor): ${price:.2f} ({change:+.2f}% 24h)')
except Exception as e:
    print(f'Error: {e}')
