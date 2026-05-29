# Crypto-AI-Trader 架構與安全審計報告

## 審計摘要

對 `/home/travis/crypto-ai-trader/src/` 下所有 Python 源碼進行了架構、耦合、導入、異常處理、密鑰管理、SPOT ONLY 安全閘、日誌敏感信息等方面的全面審計。

---

## 具體問題列表

### 🔴 嚴重 (Critical)

| # | 文件 | 行號 | 問題 | 嚴重性 | 修復建議 |
|---|------|------|------|--------|----------|
| 1 | `src/dynamic_coin_pool.py` | 20, 23 | 源碼被截斷/損壞：`WRAPPED_TOKENS=*** "WETHUSDT"}` 和 `_LEVERAGE_TOKEN_RE=re.com...T$")` 語法不完整 | **Critical** | 立即修復截斷行，恢復完整語法 |
| 2 | `src/secrets.py` | 10-12 | `os.path.join` 被截斷為 `os.pat...er`，`SECRETS_DIR` 定義不完整 | **Critical** | 修復截斷，確保路徑正確構建 |
| 3 | `src/portfolio.py` | 58 | `from state_db import get_state_db` 缺少 `src.` 前綴，當作為模塊導入時會 `ModuleNotFoundError` | **Critical** | 改為 `from src.state_db import get_state_db` |
| 4 | `src/binance_client.py` | 564 | `place_oco()` 中 `except Exception as e` 未對 `ClientError` 做區分處理，所有錯誤（包括業務錯誤）都重試 3 次，可能導致重複下單 | **Critical** | 區分 `ClientError`（業務錯誤不重試）與網絡錯誤 |
| 5 | `src/grid_trader.py` | 486 | `_verify_fill()` 直接調用底層 `self.client.client.get_order()`，繞過了 `BinanceClient` 的異常處理與重試邏輯 | **Critical** | 統一通過 `BinanceClient` 包裝方法調用，或添加獨立重試 |

### 🟠 高 (High)

| # | 文件 | 行號 | 問題 | 嚴重性 | 修復建議 |
|---|------|------|------|--------|----------|
| 6 | `src/funding_arb.py` | 106, 476-500 | 使用 `fapi.binance.com`（期貨 API）進行數據讀取。雖然文檔標註「SPOT ONLY / DISABLED」，但 `scan_opportunities()` 仍會實際調用期貨公開 API | **High** | 添加運行時環境變量閘門（如 `ENABLE_FUTURES=true`），默認阻止任何期貨 API 調用 |
| 7 | `src/data_feed.py` | 456-500 | `FundingRate` 類直接調用 `https://fapi.binance.com/fapi/v1/fundingRate`，存在期貨 API 誤用風險 | **High** | 同 #6，添加環境變量閘門；或將該類移至獨立可選模塊 |
| 8 | `src/binance_client.py` | 102-124 | `_load_keys()` 從多個來源（環境變量、.env、secrets 文件）加載密鑰，但沒有統一的密鑰來源優先級文檔，且 `.env` 文件路徑推導依賴 `Path(__file__).parent.parent`，在打包/安裝後可能失效 | **High** | 統一優先級文檔；添加密鑰來源日誌（僅記錄「來自 env/.env/secrets」，不記錄值）；支持 `PROJECT_ROOT` 環境變量覆蓋 |
| 9 | `src/notifier.py` | 30-31, 41 | Telegram Bot Token 和 Chat ID 從環境變量或 secrets 文件加載，但 `default_chat_id` 硬編碼了具體聊天室 ID（`-1003886015969`），存在信息洩露風險 | **High** | 移除硬編碼默認值，改為空字符串；啟動時若未配置則拋出異常 |
| 10 | `src/grid_trader.py` | 797-799 | `_get_symbol_precision()` 直接調用 `self.client.client.exchange_info()`，繞過緩存與異常處理 | **High** | 使用 `self.client._get_exchange_info()` 替代 |
| 11 | `src/portfolio.py` | 620-622 | `sync_from_binance()` 中直接調用 `binance_client.client.ticker_price()`，繞過包裝層 | **High** | 使用 `binance_client.get_24hr_stats()` 或添加包裝方法 |

### 🟡 中 (Medium)

| # | 文件 | 行號 | 問題 | 嚴重性 | 修復建議 |
|---|------|------|------|--------|----------|
| 12 | `src/market_scanner.py` | 12-15, 76 | 混合使用相對導入（`from .binance_client`）和絕對導入（`from src.state_db`），風格不一致 | **Medium** | 統一為相對導入 `from .state_db import get_state_db` |
| 13 | `src/binance_client.py` | 137, 150, 273, 286, 335, 350, 371, 386 | 多處使用裸 `except Exception`，捕獲過於寬泛，可能隱藏真正的程序錯誤（如 `KeyError`、`TypeError`） | **Medium** | 細分異常類型：網絡異常、API 異常、數據異常分別處理 |
| 14 | `src/risk_manager.py` | 142, 182, 199, 320, 386, 401, 725, 740, 759, 771, 788, 822, 828 | `except Exception` 過多（13 處），且部分僅記錄日誌後繼續執行，可能導致狀態不一致 | **Medium** | 對關鍵路徑（如 `_save()`）的異常進行分類處理；非關鍵路徑允許降級但需明確記錄 |
| 15 | `src/state_db.py` | 33, 39-40 | `check_same_thread=False` 配合 `threading.local()` 使用，雖然意圖是線程安全，但 `sqlite3` 連接跨線程使用仍可能在高併發下出現競態條件 | **Medium** | 使用連接池（如 `sqlalchemy` 或自研連接池）替代單例 `threading.local()` |
| 16 | `src/ws_user_stream.py` | 49, 70 | 在方法內部 `import requests`，而非模塊頂部導入，雖然是延遲加載模式，但降低了代碼可讀性與類型檢查能力 | **Medium** | 移至頂部導入；若需延遲加載，使用 `TYPE_CHECKING` 註解 |
| 17 | `src/backtest.py` | 328, 393, 1202 | `except Exception` 捕獲過寬，可能隱藏回測邏輯錯誤 | **Medium** | 細分異常類型，對數據缺失、計算錯誤分別處理 |
| 18 | `src/backtester.py` | - | 無 try/except 塊（try_blocks=0, except_blocks=0），對外部 API 調用和文件 I/O 完全無異常處理 | **Medium** | 添加基礎異常處理，特別是 `BinanceClient` 調用和 YAML/JSON 解析 |
| 19 | `src/indicators.py` | - | 無 try/except 塊，數學計算（如除零、空列表）可能拋出未捕獲異常 | **Medium** | 在公共 API 入口添加輸入驗證與異常捕獲 |
| 20 | `src/strategies/*.py` | - | 所有策略類均無 try/except，當 `klines` 格式異常或數據缺失時會直接崩潰 | **Medium** | 在 `BaseStrategy.analyze()` 調用鏈中添加保護層 |

### 🟢 低 (Low)

| # | 文件 | 行號 | 問題 | 嚴重性 | 修復建議 |
|---|------|------|------|--------|----------|
| 21 | `src/binance_client.py` | 320 | `get_balance()` 方法內部 `import time as _time`，與模塊頂部的 `import time` 冗餘 | **Low** | 移除內部冗餘導入 |
| 22 | `src/binance_client.py` | 430, 542 | 方法內部 `import math` / `import math as _math`，應移至頂部 | **Low** | 統一在模塊頂部導入 |
| 23 | `src/binance_client.py` | 660, 665 | `format_price()` / `format_quantity()` 內部 `from decimal import Decimal`，應移至頂部 | **Low** | 統一在模塊頂部導入 |
| 24 | `src/fee_optimizer.py` | 38-40 | 定義了 `FUTURES_TAKER` / `FUTURES_MAKER` 常量，雖然僅作註釋/參考，但在 SPOT ONLY 系統中不應存在期貨相關常量 | **Low** | 移除或註釋為「僅文檔用途，系統不支持期貨交易」 |
| 25 | `src/notifier.py` | 301 | `FeishuNotifier = TelegramNotifier` 向後兼容別名，無實際用途，增加混淆 | **Low** | 移除別名或添加棄用警告 |
| 26 | `src/grid_trader.py` | 19-21 | Python 3.11.15 兼容性補丁 `random.randbits = random.getrandbits`，註釋說明是 uv build 問題，但可能與標準庫行為衝突 | **Low** | 確認是否仍需要；若需要，移至 `src/utils.py` 統一處理 |
| 27 | `src/utils.py` | - | 過於簡單，僅提供 `get_project_root()`，但多個模塊各自實現了類似邏輯 | **Low** | 統一使用 `get_project_root()`，清理重複實現 |
| 28 | `src/consistency_monitor.py` | 146-149 | `if __name__ == "__main__"` 塊中使用 `from binance_client import BinanceClient`（無 `src.` 前綴），執行時可能失敗 | **Low** | 改為 `from src.binance_client import BinanceClient` |

---

## 安全閘與密鑰管理評估

### SPOT ONLY 安全閘

| 組件 | 狀態 | 說明 |
|------|------|------|
| `binance_client.py` | ✅ 安全 | 僅使用 `binance.spot.Spot`，無期貨客戶端實例化 |
| `funding_arb.py` | ⚠️ 風險 | 雖然交易方法被阻擋，但 `scan_opportunities()` 仍調用 `fapi.binance.com` |
| `data_feed.py` | ⚠️ 風險 | `FundingRate` 類直接調用期貨 API |
| `fee_optimizer.py` | ⚠️ 風險 | 定義期貨費率常量，雖未使用但存在 |

### 密鑰管理

| 組件 | 狀態 | 說明 |
|------|------|------|
| `secrets.py` | ⚠️ 風險 | 源碼截斷導致路徑解析可能異常；權限檢查僅發出 `warnings.warn` 而非拋出異常 |
| `binance_client.py` | ✅ 較好 | 支持多來源加載，有密鑰缺失驗證；錯誤消息經 `_sanitize_error` 脫敏處理 |
| `notifier.py` | ⚠️ 風險 | Telegram Token 從多來源加載，但無統一優先級；硬編碼了 `chat_id` |
| `data_feed.py` | ✅ 較好 | `CRYPTOCOMPARE_API_KEY` 通過 `secrets.py` 統一加載 |

### 日誌敏感信息

| 組件 | 狀態 | 說明 |
|------|------|------|
| `binance_client.py` | ✅ 較好 | 使用 `_sanitize_error()` 在記錄前脫敏 API 密鑰 |
| `ws_user_stream.py` | ⚠️ 風險 | WebSocket 消息處理中可能包含餘額信息，雖然僅記錄 `debug` 級別，但生產環境若啟用 debug 會洩露持倉 |
| `portfolio.py` | ⚠️ 風險 | `get_summary()` 和 `check_risk_limits()` 輸出中包含持倉明細，若通過日誌記錄可能洩露資產分佈 |

---

## 架構與耦合評估

### 模塊耦合圖（簡化）

```
binance_client.py (核心，被廣泛依賴)
    ├── secrets.py (密鑰加載)
    ├── portfolio.py → state_db.py
    ├── grid_trader.py → state_db.py, indicators.py
    ├── risk_manager.py → indicators.py, drawdown_breaker.py, correlation_risk.py
    ├── smart_order.py → indicators.py
    ├── market_scanner.py → multi_timeframe.py, dynamic_coin_pool.py, data_feed.py
    ├── ws_user_stream.py (獨立，僅依賴 websocket)
    └── data_feed.py → secrets.py
```

### 耦合問題

1. **無循環依賴**：審計確認無循環導入 ✅
2. **`state_db.py` 被多處延遲導入**：`portfolio.py`、`grid_trader.py`、`risk_manager.py`、`drawdown_breaker.py` 均在方法內部 `from src.state_db import get_state_db`，這是為了避免啟動時循環依賴，但降低了代碼清晰度。
3. **`BinanceClient` 過度集中**：幾乎所有業務模塊都直接依賴 `BinanceClient`，建議未來引入接口抽象（如 `ExchangeClient` 協議）。

---

## 異常處理覆蓋率

| 文件 | try 塊數 | except 塊數 | 覆蓋率評估 |
|------|----------|-------------|------------|
| `binance_client.py` | 22 | 37 | 高，但裸 `except Exception` 過多 |
| `portfolio.py` | 17 | 16 | 高 |
| `risk_manager.py` | 15 | 15 | 高，但裸 `except Exception` 過多 |
| `grid_trader.py` | 9 | 9 | 中等 |
| `data_feed.py` | 26 | 24 | 高 |
| `backtest.py` | 6 | 3 | 中等 |
| `backtester.py` | 0 | 0 | **無異常處理** |
| `indicators.py` | 0 | 0 | **無異常處理** |
| `strategies/*.py` | 0 | 0 | **無異常處理** |

---

## 修復優先級建議

1. **立即修復（Critical）**：修復 `dynamic_coin_pool.py` 和 `secrets.py` 的截斷源碼；修復 `portfolio.py` 的導入錯誤；修復 `grid_trader.py` 和 `binance_client.py` 的 OCO 重試邏輯。
2. **本週內修復（High）**：添加期貨 API 環境閘門；統一密鑰加載優先級；移除硬編碼 Telegram chat_id。
3. **下個迭代（Medium）**：統一導入風格；細分異常類型；為策略類添加異常保護層；改進 `StateDB` 線程安全。
4. **技術債務（Low）**：清理冗餘內部導入；移除未使用的兼容別名；統一項目根路徑獲取邏輯。
