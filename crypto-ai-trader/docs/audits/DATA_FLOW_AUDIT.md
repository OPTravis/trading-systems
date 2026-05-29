# 數據流審計報告：crypto-ai-trader

**審計日期：** 2026-04-24  
**審計範圍：** Binance API → 本地處理 → 狀態保存 → 監控輸出完整鏈路  
**重點文件：** main.py, src/portfolio.py, src/binance_client.py, src/state_db.py

---

## 一、數據流圖（文字描述）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              數據流完整鏈路                                   │
└─────────────────────────────────────────────────────────────────────────────┘

[Binance API] 
    │ 1. get_account() → balances (free + locked)
    │ 2. get_24hr_stats(symbol) → price, change_pct
    │ 3. get_klines(symbol) → OHLCV
    │ 4. get_open_orders() → SL/TP orders
    │ 5. place_order() / place_market_buy() → trade execution
    │
    ▼
[main.py]
    │
    ├── cmd_cron_scan() ───────────────────────────────────────────┐
    │   │ 1. scanner.scan_all() → opportunities (top 20)
    │   │ 2. risk_mgr.pre_trade_check() → filter
    │   │ 3. researcher.research() → score adjustment
    │   │ 4. execute_auto_trade() → place_market_buy + OCO/SL/TP
    │   │ 5. portfolio.add_position() → update in-memory + _save_state()
    │   │
    │   ▼
    │ [PortfolioManager] ──→ SQLite (state.db) PRIMARY
    │   │                    JSON (portfolio_state.json) BACKUP
    │   │
    │   ▼
    │ [Notifier] → Telegram/Feishu message
    │
    ├── cmd_status() ──────────────────────────────────────────────┤
    │   │ 1. Direct Spot API call (bypasses BinanceClient cache!)
    │   │ 2. Sync non-zero balances → portfolio.positions[symbol]
    │   │ 3. Remove ghost positions (local but not on Binance)
    │   │ 4. portfolio._save_state() → SQLite + JSON
    │   │
    │   ▼
    │ [PortfolioManager] → get_summary() → console output
    │
    ├── cmd_trailing_check() ──────────────────────────────────────┤
    │   │ 1. get_account() → positions
    │   │ 2. TrailingStop.update() → check activation/move
    │   │ 3. cancel_order() / place_order() → update SL
    │   │ 4. risk_mgr.post_trade_update() → record PnL
    │   │
    │   ▼
    │ [Notifier] → Telegram alert on SL move / trigger
    │
    ├── cmd_cron_report() ─────────────────────────────────────────┤
    │   │ 1. Direct Spot API call → account balances
    │   │ 2. Build report from live API data (NOT from PortfolioManager!)
    │   │
    │   ▼
    │ [Notifier] → Telegram daily report
    │
    └── cmd_auto_dust() ─────────────────────────────────────────┤
        │ 1. get_account() → find dust (< $1)
        │ 2. Convert API / transfer_dust() → clean up
        │
        ▼
        [Console] → JSON result

[State Storage]
    │
    ├── SQLite (state.db) — PRIMARY
    │   ├── portfolio table → positions (qty, entry_price, strategy)
    │   ├── trades table → trade history
    │   ├── trailing_stop table → TS state
    │   └── risk_guard table → loss guard
    │
    └── JSON (portfolio_state.json) — BACKUP / human-readable
        ├── positions dict → full position metadata
        ├── cash_balance
        └── _hash → integrity check
```

---

## 二、發現的斷點與問題

### 🔴 CRITICAL — NEAR/SUI/SEI/BARD 未進入本地狀態的原因

**根本原因：cmd_status() 是唯一會從 Binance API 同步持倉到本地狀態的入口，但：**

1. **PortfolioManager 初始化時只加載 SQLite 中的數據**（src/portfolio.py:70 `_load_state_from_db()`）
2. **如果 SQLite portfolio 表為空或缺少這些幣種，它們就不會出現在內存中**
3. **cmd_status() 的同步邏輯確實會把它們加入**，但這個命令需要手動運行 `python main.py status`
4. **其他命令（如 cmd_cron_scan, cmd_trailing_check）不會自動同步持倉到 PortfolioManager**

**實際驗證結果：**
- 審計前：portfolio_state.json 只有 AVAXUSDT（1個），SQLite 也只有 AVAXUSDT
- 運行 `python main.py status` 後：所有 6 個持倉（AVAX/BNB/NEAR/SUI/SEI/BARD）正確同步到 SQLite 和 JSON
- **結論：NEAR/SUI/SEI/BARD 之前沒進入本地狀態，是因為 cmd_status() 很久沒有執行，或者執行時出錯中斷**

### 🟠 HIGH — 數據不一致風險

| 問題 | 位置 | 影響 |
|------|------|------|
| **cmd_status() 使用獨立的 Spot API 客戶端** | main.py:520 | 繞過了 BinanceClient 的緩存和錯誤處理，如果 API 限流可能失敗 |
| **BNB dust (5.69e-06) 被同步進來** | main.py:528-530 | dust 過濾邏輯在同步時失效，BNBUSDT 被當作真實持倉 |
| **cash_balance 在 cmd_status() 後仍為 0（內存）** | portfolio.py:137 | `update_balance()` 只更新內存，但 portfolio_state.json 顯示 368.32，說明 JSON 寫入了但內存可能沒正確更新 |
| **PortfolioManager 不加載 Binance 持倉作為初始狀態** | portfolio.py:70 | 初始化時只從 SQLite/JSON 加載，不從 Binance API 拉取，導致重啟後狀態可能過時 |

### 🟡 MEDIUM — 緩存/緩衝機制

| 機制 | 位置 | TTL | 風險 |
|------|------|-----|------|
| `_balance_cache` | binance_client.py:91 | 30秒 | `get_balance()` 有緩存，但 `get_account()` 每次調用都走 API，沒有緩存 |
| `_exchange_info_cache` | binance_client.py:94 | 1小時 | 交易對信息緩存，正常 |
| `_save_debounce_sec` | portfolio.py:50 | 2秒 | 防止頻繁寫盤，但可能導致短時間內狀態丟失 |

**關鍵發現：**
- `cmd_status()` 直接調用 `spot.account()` 和 `spot.ticker_price()`，**完全繞過了 BinanceClient 的緩存和重試邏輯**
- 如果 Binance API 限流（429），cmd_status() 的同步邏輯會直接崩潰，不會重試

### 🟡 MEDIUM — 錯誤處理

| 場景 | 處理方式 | 問題 |
|------|----------|------|
| API 429/418 | `time.sleep(retry_after)` 重試 3 次 | binance_client.py 處理正確 |
| SSL 錯誤 | 指數退避重試 5 次 | klines 處理正確 |
| 網絡錯誤 | `time.sleep(2^attempt)` 重試 | 正確 |
| **cmd_status() 中的 API 錯誤** | `logger.warning(f"Binance sync failed: {e}")` 然後繼續 | **整個 try/except 塊包裹了所有同步邏輯，任何一步出錯都會跳過整個同步！** |

**main.py:517-596 的結構：**
```python
try:
    spot = Spot(...)          # 如果密鑰錯誤，整個同步跳過
    account = spot.account()  # 如果 429，整個同步跳過
    ...
    portfolio._save_state()   # 如果前面出錯，這裡不執行
except Exception as e:
    logger.warning(f"Binance sync failed: {e}")  # 錯誤被吞掉，狀態不更新
```

---

## 三、修復建議

### 1. 自動同步持倉（解決 NEAR/SUI/SEI/BARD 問題）

**建議 A：在 PortfolioManager 初始化時自動從 Binance 同步**
```python
# src/portfolio.py __init__
if binance_client:
    self._sync_from_binance()
```

**建議 B：在 cron-scan / trailing-check 前自動同步**
```python
# main.py 每個命令開頭
portfolio.sync_positions_from_binance(client)
```

**建議 C：定期後台同步（如每 5 分鐘）**
- 使用 cron job 每 5 分鐘運行 `python main.py status > /dev/null`
- 或者使用 WebSocket 用戶數據流實時推送餘額變化

### 2. 統一使用 BinanceClient（避免繞過緩存）

**main.py:520 應該使用已有的 `client` 實例，而不是新建 Spot：**
```python
# 當前（繞過緩存和重試）：
from binance.spot import Spot
spot = Spot(api_key=..., api_secret=...)
account = spot.account()

# 建議（使用 BinanceClient）：
account = client.get_account()  # 已有重試邏輯
```

### 3. 加強 dust 過濾

**main.py:528-530 的同步邏輯應該檢查 notional value：**
```python
# 當前：只檢查 total_qty > 0
if total_qty <= 0:
    continue

# 建議：檢查價值 >= $1
stats = client.get_24hr_stats(symbol)
price = float(stats.get('last_price', 0))
if total_qty * price < 1.0:
    continue  # skip dust
```

### 4. 細化錯誤處理

**main.py:517-596 的大 try/except 應該拆分：**
```python
# 建議：每個獨立操作有自己的錯誤處理
account = client.get_account()  # 獨立錯誤處理
if not account:
    return  # 早期退出

for bal in non_zero:
    try:
        # 同步單個幣種
    except Exception as e:
        logger.warning(f"Failed to sync {asset}: {e}")
        continue  # 跳過這個幣種，繼續其他
```

### 5. 修復 cash_balance 不一致

**portfolio.py:137 `update_balance()` 只更新內存，但 cmd_status() 後內存 cash=0 而 JSON=368：**
- 檢查 `_save_state()` 是否正確寫入 SQLite
- 檢查 `cmd_status()` 中 `portfolio.update_balance()` 的調用時機

---

## 四、驗證結果

| 測試 | 結果 |
|------|------|
| 運行 `python main.py status` | ✅ NEAR/SUI/SEI/BARD 正確同步到 SQLite 和 JSON |
| SQLite portfolio 表 | ✅ 6 個持倉全部寫入 |
| portfolio_state.json | ✅ 6 個持倉 + cash_balance=368.32 |
| Binance API 實際餘額 | ✅ 8 個非零餘額（含 USDT/NTRN/BNB dust） |
| 價格查詢 | ✅ NEAR=$1.406, SUI=$0.94, SEI=$0.062, BARD=$0.305 |

---

## 五、總結

**核心結論：**

1. **NEAR/SUI/SEI/BARD 沒有進入本地狀態，不是代碼 bug，而是 `cmd_status()` 沒有被定期執行**。系統設計上依賴手動/定時運行 `python main.py status` 來同步 Binance 持倉到本地狀態。

2. **數據流存在單點故障**：`cmd_status()` 是唯一同步入口，但它：
   - 使用獨立 API 客戶端（無緩存/重試）
   - 整個同步邏輯被一個大 try/except 包裹
   - 沒有自動定期執行機制

3. **緩存機制正常**，但 `cmd_status()` 繞過了所有緩存。

4. **修復優先級：**
   - P0：添加自動同步機制（cron 或初始化時同步）
   - P1：統一使用 BinanceClient 避免繞過緩存
   - P2：細化錯誤處理，避免單點失敗
   - P3：加強 dust 過濾

---

*審計完成時間：2026-04-24 12:16 UTC*
