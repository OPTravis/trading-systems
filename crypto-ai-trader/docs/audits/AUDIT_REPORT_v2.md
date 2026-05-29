# Crypto-AI-Trader 系統審計報告 v2.0

**審計日期**: 2026-04-24  
**審計方法**: 本地狀態文件 + SQLite + Binance API 實際數據對比  
**審計團隊**: 數據一致性檢查員、架構師、數據流審計員、安全審計員  

---

## 執行摘要

本次審計採用「本地狀態 vs 真實數據源」對比方法，發現 **3 個致命問題、4 個警告、3 個建議**。核心問題：SQLite 缺失 `cash_balance` 存儲導致 PortfolioManager 初始化後現金歸零，以及 BNB dust 被同步進持倉表。

---

## 🔴 致命問題

### F1: SQLite `portfolio` 表缺失 `cash_balance` 欄位
**嚴重程度**: 🔴 致命  
**發現者**: 架構師 + 數據一致性檢查員  

**問題描述**:  
- `state_db.py` 的 `portfolio` 表只存儲 `symbol, quantity, entry_price, strategy, opened_at, updated_at`
- **沒有 `cash_balance` 欄位**，也沒有 kv 存儲
- `portfolio.py` 的 `_load_state_from_db()` 從 SQLite 讀取 positions 後設置 `loaded=True`，**不會 fallback 到 JSON 讀取 cash_balance**
- 結果：`PortfolioManager.cash_balance` 永遠初始化為 `0`

**影響**:  
- 所有基於 `cash_balance` 的計算（倉位大小、風險限制、日內虧損追蹤）全部錯誤
- `add_position()` 的 `position_pct` 計算：`position_value / (cash_balance + exposure)` → 分母變小 → 倉位比例被高估
- `_check_daily_reset()` 使用錯誤的 `cash_balance` 作為日內基準

**驗證數據**:  
```
portfolio_state.json cash_balance = 368.32
PortfolioManager.cash_balance   = 0
Binance USDT 餘額               = 368.32
```

**修復建議**:  
1. 在 `portfolio` 表增加 `cash_balance REAL` 欄位（需 schema migration）
2. 或在 `kv` 表存儲 `cash_balance` key
3. `_load_state_from_db()` 同時讀取 cash_balance
4. `_save_state()` 同時保存 cash_balance 到 SQLite

---

### F2: BNB Dust (5.69e-06) 被同步進持倉
**嚴重程度**: 🔴 致命  
**發現者**: 數據一致性檢查員  

**問題描述**:  
- Binance 帳戶有 BNB dust: `5.69e-06` (價值約 $0.003)
- 該 dust 被同步進 `portfolio_state.json` 和 SQLite `portfolio` 表
- `PortfolioManager.DUST_THRESHOLD_USD = 1.0` 只在 `add_position()` 時過濾，**不在同步時過濾**

**影響**:  
- 持倉數量從 5 變成 6，影響 `max_open_positions` 計數
- 監控輸出顯示無意義的 dust 持倉
- 可能觸發對 BNB 的止損/止盈檢查（浪費計算）

**驗證數據**:  
```
Binance: BNB total=5.69e-06, price=633.49, value=$0.003
本地: BNBUSDT: qty=5.69e-06, entry=633.49
```

**修復建議**:  
1. 在 `cmd_status()` 同步邏輯中加入 dust 過濾：`if value < DUST_THRESHOLD_USD: continue`
2. 或在 `portfolio_set()` / `add_position()` 統一過濾

---

### F3: 持倉同步依賴手動觸發，無自動機制
**嚴重程度**: 🔴 致命  
**發現者**: 數據流審計員  

**問題描述**:  
- `cmd_status()` 是唯一從 Binance API 同步持倉到本地的入口
- 該命令**不會被 cron 自動執行**，需要用戶手動運行 `python main.py status`
- 審計前 SQLite 和 JSON 中只有 AVAXUSDT，因為 `cmd_status()` 很久沒執行
- 運行 `python main.py status` 後，所有 6 個持倉才正確同步

**影響**:  
- 新開倉位（通過 Binance App 或其他方式）不會自動進入本地狀態
- 本地策略不知道這些持倉存在，不會執行止損/止盈/追蹤止損
- 風險計算不完整（只算到已知持倉）

**驗證數據**:  
```
同步前本地持倉: {AVAXUSDT}
Binance 實際持倉: {AVAXUSDT, NEARUSDT, SUIUSDT, SEIUSDT, BARDUSDT}
運行 status 後本地: {AVAXUSDT, NEARUSDT, SUIUSDT, SEIUSDT, BARDUSDT, BNBUSDT(dust)}
```

**修復建議**:  
1. **P0**: 在 `PortfolioManager.__init__()` 或 cron 啟動前自動調用 Binance 同步
2. **P1**: 增加定時同步機制（每小時/每次交易前）
3. **P2**: 統一使用 `BinanceClient` 進行同步，避免 `cmd_status()` 繞過緩存和重試邏輯

---

## 🟡 警告

### W1: `cmd_status()` 使用獨立 Spot API 客戶端
**嚴重程度**: 🟡 警告  
**發現者**: 數據流審計員  

**問題描述**:  
- `cmd_status()` 創建獨立的 `Spot(api_key, api_secret)` 客戶端，**繞過了 `BinanceClient` 的緩存、重試、錯誤處理邏輯**
- 如果 API 限流或網絡錯誤，沒有重試機制

**影響**:  
- 同步失敗時沒有降級處理
- 與主系統的 API 調用統計分離（難以追蹤限流）

**修復建議**:  
1. 統一使用 `BinanceClient.get_account()` 進行同步
2. 复用現有的重試和緩存邏輯

---

### W2: 同步邏輯被大 try/except 包裹
**嚴重程度**: 🟡 警告  
**發現者**: 數據流審計員  

**問題描述**:  
- `cmd_status()` 的整個同步邏輯被一個大 `try/except` 包裹
- 任何單個幣種的 API 錯誤（如價格查詢失敗）會導致**整個同步跳過**

**影響**:  
- 一個幣種的問題導致所有持倉無法同步
- 錯誤信息不夠細化，難以定位問題幣種

**修復建議**:  
1. 細化錯誤處理：每個幣種獨立 try/except
2. 記錄哪些幣種同步成功/失敗
3. 部分同步成功時保存已同步的數據

---

### W3: `portfolio_state.json` 無文件鎖
**嚴重程度**: 🟡 警告  
**發現者**: 架構師  

**問題描述**:  
- `_save_state()` 使用原子寫入（寫入臨時文件後 rename），但**無文件鎖**
- 多進程同時讀寫可能導致覆蓋或損壞

**影響**:  
- cron 任務和主程序同時運行時可能衝突
- 雖然概率低，但後果嚴重（狀態丟失）

**修復建議**:  
1. 使用 `fcntl.flock` 或 `portalocker` 加文件鎖
2. 或完全遷移到 SQLite（已經是 primary storage）

---

### W4: `drawdown_breaker.json` 的 `high_watermark` 固定為 1000
**嚴重程度**: 🟡 警告  
**發現者**: 數據一致性檢查員  

**問題描述**:  
- `drawdown_breaker.json` 中 `high_watermark` 固定為 `1000`
- `current_drawdown_pct` 為 `34.93%`
- 但實際帳戶總值約 $368 (USDT) + $59.63 (持倉) = $427.63

**影響**:  
- 回撤計算基準錯誤（應該基於實際帳戶總值）
- 可能過早或過晚觸發回撤保護

**修復建議**:  
1. `high_watermark` 應該基於實際帳戶總值動態更新
2. 或初始化時從 Binance API 讀取當前總值作為基準

---

## 🟢 建議

### S1: 安全 — API 密鑰存儲合理
**嚴重程度**: 🟢 建議  
**發現者**: 安全審計員  

**評估**:  
- ✅ API 密鑰存儲在 `.env` 和 `crypto-secrets.env`，權限 `600`
- ✅ `secrets.py` 有 `check_file_permissions()` 檢查過寬權限
- ✅ `binance_client.py` 有 `_sanitize_error()` 脫敏處理
- ✅ 使用 `binance.spot.Spot`（純現貨），無期貨/合約接口
- ✅ `validate_symbol()` 支持 allowlist

**建議**:  
1. 定期輪換 API 密鑰
2. 考慮啟用 IP 白名單

---

### S2: 建議 — 增加數據一致性健康檢查命令
**嚴重程度**: 🟢 建議  
**發現者**: 數據一致性檢查員  

**建議**:  
1. 增加 `python main.py healthcheck` 命令
2. 自動對比：本地持倉 vs Binance 持倉、本地現金 vs Binance USDT
3. 不一致時發出警告/通知

---

### S3: 建議 — 統一狀態存儲
**嚴重程度**: 🟢 建議  
**發現者**: 架構師  

**建議**:  
1. 完全遷移到 SQLite，廢棄 `portfolio_state.json`
2. 或明確分工：SQLite = 系統使用，JSON = 人類可讀備份（只寫不讀）
3. 當前雙存儲架構導致不一致風險

---

## 數據源對照表

| 數據項 | portfolio_state.json | SQLite portfolio | Binance API | 一致性 |
|--------|---------------------|------------------|-------------|--------|
| AVAX qty | 1.43 | 1.43 | 1.43 | ✅ |
| NEAR qty | 14.0 | 14.0 | 14.0 | ✅ |
| SUI qty | 5.4 | 5.4 | 5.4 | ✅ |
| SEI qty | 239.5 | 239.5 | 239.5 | ✅ |
| BARD qty | 22.0 | 22.0 | 22.0 | ✅ |
| BNB qty | 5.69e-06 | 5.69e-06 | 5.69e-06 | ⚠️ dust |
| cash_balance | 368.32 | **缺失** | 368.32 | ❌ |
| 持倉數量 | 6 | 6 | 5 (>\$1) | ⚠️ |

---

## 修復優先級

| 優先級 | 問題 | 預估工作量 |
|--------|------|-----------|
| P0 | F1: SQLite 增加 cash_balance 存儲 | 2h |
| P0 | F3: 自動同步 Binance 持倉 | 2h |
| P1 | F2: 同步時過濾 dust | 30min |
| P1 | W1: 統一使用 BinanceClient | 1h |
| P1 | W2: 細化同步錯誤處理 | 1h |
| P2 | W3: 加文件鎖 | 1h |
| P2 | W4: 修正回撤基準 | 1h |
| P2 | S2: 增加 healthcheck 命令 | 2h |

---

## 審計方法說明

本次審計與上次（v1.0）的區別：
- **v1.0**: 只看代碼邏輯，沒有對比真實數據 → 沒發現 cash_balance=0 和缺失持倉
- **v2.0**: 實際調用 Binance API，對比本地狀態 → 發現所有不一致項

**教訓**: 任何涉及「本地狀態 + 外部 API」的系統，審計必須包含實際數據對比。
