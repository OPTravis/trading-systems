# Crypto-AI-Trader 數據一致性審計報告

**審計日期**: 2026-04-24  
**審計範圍**: portfolio.py、state_db.py、consistency_monitor.py、binance_client.py、entry_price.py、main.py  
**審計重點**: 數據流、緩存機制、API 同步邏輯、狀態漂移、entry_price 計算、PnL 計算準確性  

---

## 🔴 CRITICAL — 致命問題

### C1: `portfolio` 表缺失 `cash_balance` 欄位，導致初始化後現金歸零
- **文件**: `src/state_db.py:58-65` (schema), `src/portfolio.py:540-579` (_load_state_from_db)
- **嚴重性**: 🔴 CRITICAL
- **問題**: `state_db.py` 的 `portfolio` 表只存 `symbol, quantity, entry_price, strategy, opened_at, updated_at`，**沒有 `cash_balance` 欄位**。`portfolio.py` 的 `_load_state_from_db()` 從 SQLite 讀取 positions 後設置 `loaded=True`，**不會 fallback 到 JSON 讀取 cash_balance**。結果：`PortfolioManager.cash_balance` 永遠初始化為 `0`。
- **影響**: 
  - `add_position()` 的 `position_pct` 計算分母變小 → 倉位比例被高估
  - `_check_daily_reset()` 使用錯誤的 `cash_balance` 作為日內基準
  - 風險限制檢查全部錯誤
- **修復建議**: 
  1. 在 `portfolio` 表增加 `cash_balance REAL` 欄位（需 schema migration）
  2. 或在 `kv` 表存儲 `cash_balance` key（已有 `portfolio_set_cash_balance` / `portfolio_get_cash_balance`，但 `_load_state_from_db` 在 `loaded=True` 時不讀取）
  3. 修改 `_load_state_from_db()`：即使從 SQLite 讀到 positions，也必須同時讀取 `kv` 中的 `cash_balance`

---

### C2: BNB Dust (5.69e-06) 被同步進持倉
- **文件**: `main.py:528-530` (cmd_status 同步邏輯), `src/portfolio.py:581-670` (sync_from_binance)
- **嚴重性**: 🔴 CRITICAL
- **問題**: `PortfolioManager.DUST_THRESHOLD_USD = 1.0` 只在 `add_position()` 時過濾，**不在 `sync_from_binance()` 和 `cmd_status()` 同步時過濾**。Binance 帳戶的 BNB dust (`5.69e-06`，價值 $0.003) 被同步進 `portfolio_state.json` 和 SQLite `portfolio` 表。
- **影響**: 
  - 持倉數量從 5 變成 6，影響 `max_open_positions` 計數
  - 可能觸發對 BNB 的止損/止盈/追蹤止損檢查（浪費計算）
  - 監控輸出顯示無意義的 dust 持倉
- **修復建議**: 
  1. 在 `sync_from_binance()` 和 `cmd_status()` 同步邏輯中加入 dust 過濾：`if value < DUST_THRESHOLD_USD: continue`
  2. 或在 `portfolio_set()` / `add_position()` 統一過濾（但 sync 邏輯直接操作 `portfolio.positions` 字典，繞過了 `add_position`）

---

### C3: 持倉同步依賴手動觸發，無自動機制
- **文件**: `main.py:509-607` (_sync_from_binance), `src/portfolio.py:70` (__init__)
- **嚴重性**: 🔴 CRITICAL
- **問題**: `cmd_status()` / `_sync_from_binance()` 是唯一從 Binance API 同步持倉到本地的入口。`PortfolioManager.__init__()` 初始化時**只從 SQLite/JSON 加載，不從 Binance API 拉取**。審計前 SQLite 和 JSON 中只有 AVAXUSDT，因為 `cmd_status()` 很久沒執行。新開倉位（通過 Binance App 或其他方式）不會自動進入本地狀態。
- **影響**: 
  - 本地策略不知道新持倉存在，不會執行止損/止盈/追蹤止損
  - 風險計算不完整（只算到已知持倉）
  - 重啟後狀態可能過時
- **修復建議**: 
  1. **P0**: 在 `PortfolioManager.__init__()` 中，如果提供了 `binance_client`，自動調用 `sync_from_binance()`
  2. **P1**: 在 `cmd_cron_scan`、`cmd_trade` 等每個交易命令開頭自動同步
  3. **P2**: 增加定時同步機制（每小時/每次交易前）

---

### C4: `cmd_status()` 使用獨立 Spot API 客戶端，繞過所有緩存和重試邏輯
- **文件**: `main.py:509-607` (_sync_from_binance)
- **嚴重性**: 🔴 CRITICAL
- **問題**: `_sync_from_binance()` 在 `cmd_status()` 中被調用時，雖然傳入的是 `BinanceClient`，但內部直接調用 `client.client.ticker_price()` 和 `client.get_account()`。更嚴重的是，舊版 `cmd_status()` 曾創建獨立的 `Spot(api_key, api_secret)` 客戶端，**完全繞過了 `BinanceClient` 的緩存、重試、錯誤處理邏輯**。如果 API 限流（429）或網絡錯誤，同步邏輯會直接崩潰，不會重試。
- **影響**: 
  - 同步失敗時沒有降級處理
  - 與主系統的 API 調用統計分離（難以追蹤限流）
  - 整個同步邏輯被一個大 `try/except` 包裹，任何單個幣種的錯誤會導致**整個同步跳過**
- **修復建議**: 
  1. 統一使用 `BinanceClient.get_account()` 和 `BinanceClient.get_24hr_stats()` 進行同步
  2. 細化錯誤處理：每個幣種獨立 try/except，一個幣種失敗不影響其他幣種
  3. 部分同步成功時保存已同步的數據

---

### C5: `sync_from_binance()` 使用**當前市價**作為 entry_price，導致 PnL 計算完全錯誤
- **文件**: `src/portfolio.py:620-637` (sync_from_binance), `src/entry_price.py:1-89` (get_avg_entry_price)
- **嚴重性**: 🔴 CRITICAL
- **問題**: `sync_from_binance()` 在重建持倉時，調用 `binance_client.client.ticker_price(f"{asset}USDT")` 獲取當前價格，然後將其作為 `entry_price` 傳入 `add_position()`。這意味著**所有從 Binance 同步的持倉，其 entry_price 都是同步時的市價，而非真實的加權平均買入價**。雖然 `main.py:_sync_from_binance()` 嘗試使用 `get_avg_entry_price()` 獲取真實 entry price，但 `portfolio.py:sync_from_binance()` 沒有這個邏輯，且 `main.py` 的實現也有問題（`current_qty=total_qty` 但 `get_avg_entry_price` 參數名是 `current_qty`）。
- **影響**: 
  - 所有同步持倉的 PnL 計算完全錯誤（顯示為 0% 或接近 0%）
  - 止損/止盈價格基於錯誤的 entry_price，可能過早或過晚觸發
  - 追蹤止損的 `highest_price` 和 `sl_price` 基於錯誤的 entry_price
  - 日內虧損追蹤 (`_check_daily_reset`) 基於錯誤的持倉價值
- **修復建議**: 
  1. **P0**: 在 `sync_from_binance()` 中優先使用 `get_avg_entry_price()` 獲取真實加權平均價
  2. **P1**: 如果 `get_avg_entry_price()` 返回 None（歷史不完整），使用當前價格但標記為 `entry_price_estimated=True`
  3. **P2**: 在持倉元數據中記錄 `entry_price_source`（`trade_history` / `sync_estimate` / `manual`）

---

## 🟠 HIGH — 高嚴重性問題

### H1: `portfolio_state.json` 作為緩存的設計缺陷 — 無文件鎖 + 哈希校驗失敗即清空狀態
- **文件**: `src/portfolio.py:487-538` (_save_state), `src/portfolio.py:693-719` (_load_state_from_json)
- **嚴重性**: 🟠 HIGH
- **問題**: 
  1. `_save_state()` 使用原子寫入（寫入臨時文件後 `os.replace`），但**無文件鎖**。多進程（cron 任務和主程序）同時讀寫可能導致覆蓋或損壞。
  2. `_load_state_from_json()` 中，如果 hash 校驗失敗，方法**直接 return（清空狀態）**。這意味著單個損壞的字節會導致**所有持倉追蹤被抹除**。持倉仍在 Binance 上，但對風險管理器不可見。
- **影響**: 
  - cron 任務和主程序同時運行時可能衝突
  - 狀態文件輕微損壞導致所有持倉丟失，風險管理失效
- **修復建議**: 
  1. 使用 `fcntl.flock` 或 `portalocker` 加文件鎖
  2. hash 校驗失敗時，備份損壞文件，記錄錯誤，並 fallback 到讀取 Binance API 真實持倉
  3. 考慮完全遷移到 SQLite（已經是 primary storage），JSON 僅作為人類可讀備份

---

### H2: `get_avg_entry_price()` 的 FIFO 邏輯存在邊界錯誤，可能導致 entry_price 計算不準確
- **文件**: `src/entry_price.py:40-61` (FIFO lot reduction)
- **嚴重性**: 🟠 HIGH
- **問題**: `get_avg_entry_price()` 使用 FIFO 方法減少 lot，但當 `lot_qty == remaining` 時，`lots.pop(0)` 後 `remaining -= lot_qty` 會使 `remaining` 變為 0，循環結束。這是正確的。但問題在於：
  1. `trades` 只取 `limit=100`，如果交易歷史超過 100 筆，**早期的買入 lot 會被截斷**，導致計算的 entry price 只基於近期交易
  2. `current_qty` 驗證使用 5% 容差，但對於高頻交易或 dust 持倉，5% 可能過大或過小
  3. 沒有處理 `commission`（手續貼）對實際成本的影響
- **影響**: 
  - 交易歷史超過 100 筆的幣種，entry_price 計算不準確
  - 手續貼未計入，實際成本被低估
  - PnL 計算因此有偏差
- **修復建議**: 
  1. 增加 `limit` 參數或分頁獲取所有交易歷史
  2. 考慮手續貼：`total_cost = sum(q * p + commission for ...)`
  3. 對於無法確定 entry_price 的持倉，標記為 `entry_price_estimated`

---

### H3: `_save_state()` 的 debounce 機制可能導致短時間內狀態丟失
- **文件**: `src/portfolio.py:487-492` (_save_state debounce)
- **嚴重性**: 🟠 HIGH
- **問題**: `_save_debounce_sec = 2` 秒，如果在 2 秒內多次調用 `_save_state()`（例如快速開倉/平倉），只有第一次會實際寫入。後續的狀態變化會被丟棄。如果程序在這 2 秒內崩潰，最新的狀態不會被保存。
- **影響**: 
  - 高頻交易場景下，狀態可能不同步
  - 程序崩潰時可能丟失最近 2 秒內的持倉變化
- **修復建議**: 
  1. 使用「標記髒數據 + 定時刷新」模式，而不是簡單的 debounce
  2. 或在程序退出時強制刷新所有 pending saves
  3. 對於關鍵操作（如平倉），強制繞過 debounce 立即保存

---

### H4: `consistency_monitor.py` 的 drift 計算邏輯有缺陷
- **文件**: `src/consistency_monitor.py:13-142` (validate_consistency)
- **嚴重性**: 🟠 HIGH
- **問題**: 
  1. `api_total` 的計算包含 USDT 餘額，但 `local_total` 也包含 `cash_balance`，兩者應該可比。但 `api_positions` 的價值使用 `total * price`，而 `local_positions` 的價值使用 `pos['quantity'] * pos.get('current_price', pos['entry_price'])`。如果 `current_price` 是舊的緩存值，drift 會被高估。
  2. `quantity_mismatch` 使用固定閾值 `0.0001`，對於不同價格的幣種不合理（BTC 的 0.0001 是 $10，SHIB 的 0.0001 幾乎為 0）。
  3. 如果 `binance_client.get_account()` 失敗，返回 `{'consistent': False, 'alerts': [...]}`，但**沒有觸發任何自動修復動作**（如自動同步）。
- **影響**: 
  - 誤報或漏報數據不一致
  - 檢測到不一致後沒有自動修復，需要人工介入
- **修復建議**: 
  1. 統一價格來源：使用同一個價格計算 api_value 和 local_value
  2. 使用相對閾值（如 0.1%）而不是絕對閾值
  3. 檢測到不一致後，自動觸發 `sync_from_binance()` 修復

---

### H5: `calculate_pnl()` 未考慮手續貼和資金費，PnL 計算不完整
- **文件**: `src/portfolio.py:333-353` (calculate_pnl)
- **嚴重性**: 🟠 HIGH
- **問題**: `calculate_pnl()` 只計算 `(current - entry) * qty`，**完全沒有考慮**：
  1. 交易手續貼（taker/maker fee）
  2. 資金貼（funding rate，對於合約）
  3. 滑點（slippage）
  4. 部分平倉後的加權平均成本變化
- **影響**: 
  - 顯示的 PnL 總是高於實際 PnL（因為沒有扣除費用）
  - 策略可能基於虛高的 PnL 做出錯誤決策
  - 日內虧損追蹤不準確
- **修復建議**: 
  1. 在 `state_db.py` 的 `trades` 表中記錄每筆交易的 fee
  2. `calculate_pnl()` 增加 `fees_deducted` 參數，或提供 `realized_pnl` 和 `unrealized_pnl` 兩個版本
  3. 從 Binance API 獲取實際的 `commission` 數據

---

## 🟡 MEDIUM — 中嚴重性問題

### M1: `state_db.py` 的 `threading.local()` 連接池在單進程多線程場景下可能洩漏連接
- **文件**: `src/state_db.py:30-43` (_get_conn)
- **嚴重性**: 🟡 MEDIUM
- **問題**: `StateDB` 使用 `threading.local()` 為每個線程創建獨立的 SQLite 連接，但**從不關閉這些連接**。如果程序使用 ThreadPoolExecutor（如 `market_scanner.py`），每個線程會創建一個新連接，長期運行後可能達到系統文件描述符限制。
- **影響**: 
  - 長期運行後連接洩漏
  - 可能達到 SQLite 或系統的文件描述符限制
- **修復建議**: 
  1. 使用連接池（如 `sqlite3` 的 `check_same_thread=False` 配合顯式連接管理）
  2. 或定期清理長時間未使用的線程本地連接
  3. 在 `StateDB` 中添加 `close()` 方法並在程序退出時調用

---

### M2: `portfolio.py:551-563` 從 DB 加載時，stop_loss / take_profit 基於 entry_price 重新計算，可能覆蓋用戶自定義值
- **文件**: `src/portfolio.py:551-563` (_load_state_from_db)
- **嚴重性**: 🟡 MEDIUM
- **問題**: 從 SQLite 加載持倉時，`stop_loss` 和 `take_profit` 使用 `data["entry_price"] * (1 - default_pct / 100)` 重新計算。如果持倉之前被手動修改過 stop_loss（如通過風險管理器動態調整），這個自定義值會被覆蓋。
- **影響**: 
  - 用戶或策略自定義的 stop_loss 被重置為默認值
  - 動態風險調整失效
- **修復建議**: 
  1. 在 `state_db.py` 的 `portfolio` 表中增加 `stop_loss` 和 `take_profit` 欄位
  2. 加載時優先使用 DB 中的值，只有在缺失時才使用默認值計算

---

### M3: `binance_client.py` 的 `get_price_precision()` 每次調用都獲取 exchange_info，無緩存
- **文件**: `src/binance_client.py:377-388` (get_price_precision)
- **嚴重性**: 🟡 MEDIUM
- **問題**: `get_price_precision()` 每次調用都獲取 `exchange_info()`，這是一個 1-2MB 的 JSON。雖然 `_get_exchange_info()` 有 1 小時緩存，但 `get_price_precision()` 在 `place_order()` 中被調用，**每次下單都增加 200-500ms 延遲**。
- **影響**: 
  - 下單延遲增加，可能錯過最佳執行價格
  - 不必要的 API 調用（雖然有緩存，但仍需序列化/反序列化大 JSON）
- **修復建議**: 
  1. 在 `BinanceClient` 初始化時預加載並緩存所有交易對的精度信息
  2. `place_order()` 直接從緩存讀取精度，不再調用 `get_price_precision()`

---

### M4: `portfolio.py:226-227` `deduct_cash=True` 時，cash_balance 可能變為負數
- **文件**: `src/portfolio.py:226-227` (add_position deduct_cash)
- **嚴重性**: 🟡 MEDIUM
- **問題**: `add_position()` 在 `deduct_cash=True` 時直接執行 `self.cash_balance -= quantity * entry_price`，**沒有檢查 `cash_balance` 是否足夠**。如果 cash_balance 為 0（由於 C1 的問題）或不足，結果會是負數。
- **影響**: 
  - `cash_balance` 變為負數，導致後續計算全部錯誤
  - `check_risk_limits()` 中的 `total_value` 計算不準確
- **修復建議**: 
  1. 在 `deduct_cash` 前檢查 `self.cash_balance >= quantity * entry_price`
  2. 如果不足，記錄警告並拒絕操作（或標記為 `cash_deficit`）

---

### M5: `sync_from_binance()` 調用 `add_position()`，但 `add_position()` 會再次觸發 `_save_state()`，導致冗餘寫入
- **文件**: `src/portfolio.py:631-637` (sync_from_binance 調用 add_position), `src/portfolio.py:229` (add_position 調用 _save_state)
- **嚴重性**: 🟡 MEDIUM
- **問題**: `sync_from_binance()` 在循環中為每個持倉調用 `add_position()`，而 `add_position()` 每次都会調用 `_save_state()`。對於 N 個持倉，這會觸發 N 次 `_save_state()`，每次都要寫入 SQLite + JSON。雖然有 2 秒 debounce，但在同步多個持倉時仍然低效。
- **影響**: 
  - 同步時不必要的 I/O 開銷
  - 可能觸發 debounce 導致部分狀態未保存
- **修復建議**: 
  1. 在 `sync_from_binance()` 中批量構建 `self.positions`，同步完成後只調用一次 `_save_state()`
  2. 或為 `add_position()` 增加 `_skip_save` 參數，供批量操作使用

---

## 🟢 LOW — 低嚴重性問題

### L1: `portfolio.py:202-203` stop_loss / take_profit 計算使用 `self.config["stop_loss"]["default_pct"]`，但 `stop_loss` 鍵名有潛在錯誤
- **文件**: `src/portfolio.py:202-203` (add_position)
- **嚴重性**: 🟢 LOW
- **問題**: `self.config["stop_loss"]["default_pct"]` 是正確的，但 `_validate_config()` 中 `pct_key = f"{key.replace('_', '_')}_pct"` 這行代碼邏輯無意義（`replace('_', '_')` 等於原字符串）。雖然不影響功能，但說明配置驗證邏輯有瑕疵。
- **修復建議**: 清理 `_validate_config()` 中的無效代碼

---

### L2: `consistency_monitor.py` 缺少定期運行機制
- **文件**: `src/consistency_monitor.py:1-156`
- **嚴重性**: 🟢 LOW
- **問題**: `consistency_monitor.py` 提供了 `validate_consistency()` 函數，但系統中沒有任何地方定期調用它。它只是一個獨立腳本，需要手動運行 `python src/consistency_monitor.py`。
- **修復建議**: 在 `cmd_cron_scan()` 或 `cmd_status()` 中集成一致性檢查，並設置閾值自動觸發修復

---

### L3: `portfolio.py:260-261` close_position 的 PnL 計算未扣除手續貼
- **文件**: `src/portfolio.py:260-261` (close_position)
- **嚴重性**: 🟢 LOW
- **問題**: `pnl = (price - pos["entry_price"]) * pos["quantity"]` 未扣除交易手續貼。這是 `calculate_pnl()` 的已知問題的延伸。
- **修復建議**: 從 `trade_add()` 中獲取實際手續貼並扣除

---

## 總結

| 嚴重性 | 數量 | 核心問題 |
|--------|------|---------|
| 🔴 CRITICAL | 5 | cash_balance 缺失、Dust 同步、無自動同步、API 繞過、entry_price 錯誤 |
| 🟠 HIGH | 5 | JSON 無鎖、FIFO 邊界錯誤、debounce 丟失、drift 計算缺陷、PnL 未扣費 |
| 🟡 MEDIUM | 5 | 連接洩漏、SL/TP 覆蓋、精度無緩存、cash 負數、冗餘寫入 |
| 🟢 LOW | 3 | 配置驗證瑕疵、一致性檢查未集成、手續貼未扣除 |

**最緊急修復（P0）**:
1. 修復 `cash_balance` 存儲和加載（C1）
2. 在同步時過濾 dust（C2）
3. 在 `PortfolioManager.__init__()` 中自動同步 Binance 持倉（C3）
4. 統一使用 `BinanceClient` 並細化錯誤處理（C4）
5. 使用真實 `get_avg_entry_price()` 而不是市價作為 entry_price（C5）
