#!/usr/bin/env python3
"""Sync system clock to Binance server time."""
import time, requests, subprocess, sys
from datetime import datetime

r = requests.get('https://api.binance.com/api/v3/time', timeout=5)
server_ms = r.json()['serverTime']
local_ms = int(time.time() * 1000)
offset = local_ms - server_ms
print(f'Offset: {offset}ms')

server_time = datetime.fromtimestamp(server_ms / 1000)
time_str = server_time.strftime('%Y-%m-%d %H:%M:%S')
print(f'Setting clock to: {time_str}')

result = subprocess.run(['sudo', 'date', '-s', time_str], capture_output=True, text=True)
if result.returncode == 0:
    print('Clock synced')
else:
    print(f'Sync failed: {result.stderr}')
    sys.exit(1)

# Verify
local_ms = int(time.time() * 1000)
offset = local_ms - server_ms
print(f'New offset: {offset}ms')
