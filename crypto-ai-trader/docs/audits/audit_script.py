import sys, json, os, time
from datetime import datetime
sys.path.insert(0, 'src')
from binance_client import BinanceClient

client = BinanceClient()

# Load local states
states = {}
for fname in ['portfolio_state.json', 'dca_state.json', 'loss_guard.json', 'trailing_stops.json', 'drawdown_breaker.json']:
    with open(f'data/{fname}') as f:
        states[fname] = json.load(f)

# Get real account data
account = client.get_account()
real_balances = {}
for b in account.get('balances', []):
    free = float(b['free'])
    locked = float(b['locked'])
    if free > 0 or locked > 0:
        real_balances[b['asset']] = {'free': free, 'locked': locked}

# Get current prices
symbols = ['AVAXUSDT', 'NEARUSDT', 'SUIUSDT', 'SEIUSDT', 'BARDUSDT']
prices = {}
for s in symbols:
    try:
        prices[s] = client.get_ticker_price(symbol=s)
    except Exception as e:
        prices[s] = None

now = time.time()
now_iso = datetime.now().isoformat()

print('='*60)
print('數據一致性審計報告')
print(f'審計時間: {now_iso}')
print('='*60)

# --- portfolio_state.json audit ---
print()
print('--- [1] portfolio_state.json ---')
ps = states['portfolio_state.json']
saved_at = ps.get('saved_at', 'N/A')
print(f'saved_at: {saved_at}')
cash = ps.get('cash_balance', 0)
real_cash = real_balances.get("USDT", {}).get("free", 0) + real_balances.get("USDT", {}).get("locked", 0)
print(f'本地現金: {cash}')
print(f'API 現金: {real_cash}')
if abs(cash - real_cash) < 0.01:
    print('  [OK] 現金餘額一致')
else:
    print('  [CRITICAL] 現金餘額不一致!')

local_positions = set(ps.get('positions', {}).keys())
# Map real balances to USDT pairs
real_assets = set()
for asset, bal in real_balances.items():
    if asset in ['AVAX', 'NEAR', 'SUI', 'SEI', 'BARD']:
        if bal['locked'] > 0:
            real_assets.add(asset + 'USDT')

print(f'本地持倉幣種: {local_positions}')
print(f'API 持倉幣種: {real_assets}')
missing_local = real_assets - local_positions
missing_api = local_positions - real_assets
if missing_local:
    print(f'  [CRITICAL] 本地缺失持倉: {missing_local}')
if missing_api:
    print(f'  [WARNING] API缺失持倉(可能已賣出): {missing_api}')
if not missing_local and not missing_api:
    print('  [OK] 持倉幣種一致')

# Check each position quantity
for sym in real_assets & local_positions:
    asset = sym.replace('USDT', '')
    real_qty = real_balances.get(asset, {}).get('locked', 0)
    local_qty = ps['positions'][sym]['quantity']
    print(f'  {sym}: 本地={local_qty}, API={real_qty}', end='')
    if abs(real_qty - local_qty) > 0.001:
        print(' [CRITICAL 數量不一致]')
    else:
        print(' [OK]')

# Check saved_at age
try:
    saved_dt = datetime.fromisoformat(saved_at)
    age_sec = (datetime.now() - saved_dt).total_seconds()
    print(f'數據年齡: {age_sec:.0f} 秒')
    if age_sec > 3600:
        print('  [WARNING] 數據超過1小時未更新')
except:
    pass

# --- dca_state.json audit ---
print()
print('--- [2] dca_state.json ---')
ds = states['dca_state.json']
print(f'last_dca_time: {ds.get("last_dca_time", "N/A")}')
print(f'dca_cycle: {ds.get("dca_cycle", "N/A")}')
print(f'total_invested: {ds.get("total_invested", "N/A")}')
print(f'eth_positions: {ds.get("eth_positions", [])}')
print('  [INFO] DCA狀態獨立，無直接API對照項')

# --- loss_guard.json audit ---
print()
print('--- [3] loss_guard.json ---')
lg = states['loss_guard.json']
last_loss = lg.get('last_loss_time', 0)
print(f'consecutive_losses: {lg.get("consecutive_losses", 0)}')
print(f'last_loss_time: {last_loss} ({datetime.fromtimestamp(last_loss).isoformat() if last_loss else "N/A"})')
print(f'paused_until: {lg.get("paused_until", "N/A")}')
print('  [INFO] 損失守衛為本地策略狀態，無直接API對照項')

# --- trailing_stops.json audit ---
print()
print('--- [4] trailing_stops.json ---')
ts = states['trailing_stops.json']
print(f'追蹤止損條目數: {len(ts)}')
for sym, data in ts.items():
    asset = sym.replace('USDT', '')
    has_position = asset in real_balances and real_balances[asset].get('locked', 0) > 0
    status = '有持倉' if has_position else '無持倉'
    activated = data.get('activated', 0)
    sl_price = data.get('sl_price', 0)
    print(f'  {sym}: {status}, activated={activated}, sl_price={sl_price}')
    if not has_position and activated == 0:
        print(f'    [WARNING] {sym} 無持倉但存在止損記錄（殘留數據）')
    if activated == 1 and sl_price == 0:
        print(f'    [CRITICAL] {sym} 已激活但止損價為0!')

# --- drawdown_breaker.json audit ---
print()
print('--- [5] drawdown_breaker.json ---')
db = states['drawdown_breaker.json']
print(f'high_watermark: {db.get("high_watermark", "N/A")}')
print(f'current_drawdown_pct: {db.get("current_drawdown_pct", "N/A")}%')
print(f'max_drawdown_pct: {db.get("max_drawdown_pct", "N/A")}%')
tripped_at = db.get('tripped_at')
print(f'tripped_at: {tripped_at} ({datetime.fromtimestamp(tripped_at).isoformat() if tripped_at else "N/A"})')
print(f'tripped_count: {db.get("tripped_count", 0)}')
print('  [INFO] 回撤斷路器為本地策略狀態，無直接API對照項')

# --- Summary ---
print()
print('='*60)
print('審計摘要')
print('='*60)
print(f'總持倉價值 (API):')
total_value = real_balances.get('USDT', {}).get('free', 0)
for asset, bal in real_balances.items():
    if asset in ['AVAX', 'NEAR', 'SUI', 'SEI', 'BARD']:
        qty = bal['locked']
        price = prices.get(asset + 'USDT', 0)
        value = qty * price
        total_value += value
        print(f'  {asset}: {qty} * {price} = {value:.2f} USDT')
print(f'  USDT現金: {real_balances.get("USDT", {}).get("free", 0):.2f}')
print(f'  總計: {total_value:.2f} USDT')
print()
print('不一致項統計:')
print(f'  CRITICAL: {len(missing_local)} 項 (本地缺失持倉)')
print(f'  WARNING:  trailing_stops.json 中存在無持倉幣種的殘留記錄')
print(f'  INFO: dca/loss_guard/drawdown_breaker 為本地策略狀態，無API對照項')
