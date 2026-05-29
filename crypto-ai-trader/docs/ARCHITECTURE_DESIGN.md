# Crypto-AI-Trader 系統架構設計文檔

## 1. 架構評估結論

**不需要完全重構。** 現有系統採用分層架構，核心設計合理。建議採用「漸進式重構」策略：
- 保留現有業務邏輯和數據流
- 重點解耦緊密耦合模塊（main.py 1,652行、portfolio.py 773行）
- 引入接口抽象層（ExchangeClient Protocol）
- 統一狀態管理（StateDB 已部分實現，需擴展）
- 清理技術債務（截斷代碼、硬編碼、異常處理）

---

## 2. 系統架構總覽

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              交互層 (CLI / Cron)                              │
│  main.py (1,652行)  │  grid_bot.py (142行)  │  各類 scripts/                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                              業務編排層 (Orchestration)                        │
│  StrategyAdaptor │ MarketScanner │ PortfolioManager │ RiskManager │ GridTrader │
├─────────────────────────────────────────────────────────────────────────────┤
│                              策略層 (Strategies)                              │
│  Grid │ DCA │ Trend │ RSI │ Bollinger │ VWAP │ KellySizer │ FeeOptimizer      │
├─────────────────────────────────────────────────────────────────────────────┤
│                              基礎設施層 (Infrastructure)                       │
│  BinanceClient │ DataFeed │ Indicators │ Sentiment │ MultiTimeframe           │
├─────────────────────────────────────────────────────────────────────────────┤
│                              狀態與通知層 (State & Notify)                     │
│  StateDB (SQLite) │ JSON Backup │ Notifier (Telegram/Feishu)                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 現有模塊劃分（31個 Python 文件，13,291行）

### 3.1 核心層（Core Infrastructure）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/binance_client.py` | 709 | Binance SPOT API 封裝、密鑰管理、重試邏輯 | 裸 except 過多，OCO 重試邏輯有重複下單風險 |
| `src/state_db.py` | 421 | SQLite 持久化（portfolio/trades/risk/audit） | threading.local() 高併發競態，需連接池 |
| `src/secrets.py` | 42 | 密鑰文件加載 | **源碼截斷**，路徑解析不完整 |
| `src/utils.py` | 13 | get_project_root() | 過於簡單，多處重複實現類似邏輯 |

### 3.2 市場數據層（Market Data）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/data_feed.py` | 1,012 | K線獲取、FundingRate、新聞情緒 | 直接調用 fapi（期貨）API |
| `src/indicators.py` | 315 | 技術指標計算（MA/RSI/布林/VWAP/ATR） | **零異常處理**，除零風險 |
| `src/multi_timeframe.py` | 305 | 多時間框架分析 | 依賴 indicators，無異常保護 |
| `src/market_researcher.py` | 587 | 幣種基本面研究 | 與 scanner 職責有重疊 |
| `src/dynamic_coin_pool.py` | 340 | 動態幣池篩選（市值/成交量/波動率） | **源碼截斷** |
| `src/sentiment.py` | 184 | 恐慌貪婪指數、新聞情緒 | 外部 API 依賴 |

### 3.3 策略層（Strategies）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/strategy_adaptor.py` | 522 | 根據市場情緒動態調整策略參數 | 與 risk_limits.yaml 配置分離 |
| `src/grid_trader.py` | 834 | 網格交易核心（獨立運行） | 直接調用底層 client，繞過包裝層 |
| `src/smart_order.py` | 373 | 訂單智能拆分、精度處理 | 被 main.py 和 portfolio 依賴 |
| `src/entry_price.py` | 106 | 入場價格計算 | 職責單一，可合併 |
| `src/kelly_sizer.py` | 196 | Kelly 公式倉位計算 | 未與主流程集成 |
| `src/fee_optimizer.py` | 290 | 手續費優化分析 | 含期貨常量（未使用） |

### 3.4 風險管理層（Risk Management）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/risk_manager.py` | 844 | 趨勢過濾、回撤斷路、連敗保護、相關性風險 | 13處裸 except，狀態不一致風險 |
| `src/drawdown_breaker.py` | 215 | 最大回撤觸發暫停 | 依賴 risk_manager 狀態 |
| `src/correlation_risk.py` | 198 | 持倉相關性監控 | 計算邏輯簡單 |

### 3.5 投資組合層（Portfolio）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/portfolio.py` | 773 | 持倉管理、PnL 計算、風險檢查 | **過大**，導入錯誤（`from state_db` 缺 `src.`） |
| `src/backtest.py` | 1,247 | 回測引擎（多策略並行） | except 過寬，可能隱藏邏輯錯誤 |
| `src/backtester.py` | 256 | 回測器封裝 | **零異常處理** |

### 3.6 通知與輔助層（Notification & Utils）

| 模塊 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `src/notifier.py` | 301 | Telegram/Feishu 通知 | 硬編碼 chat_id |
| `src/ws_user_stream.py` | 239 | WebSocket 用戶數據流 | 可能洩露持倉信息到 debug 日誌 |
| `src/consistency_monitor.py` | 155 | 數據一致性監控 | `__main__` 塊導入錯誤 |

### 3.7 入口文件（Entry Points）

| 文件 | 行數 | 職責 | 問題 |
|------|------|------|------|
| `main.py` | 1,652 | 主 CLI、自動交易執行、掃描、回測 | **過大**，業務邏輯與 CLI 混合 |
| `grid_bot.py` | 142 | 網格交易 CLI | 獨立入口，與 main.py 並行 |
| `handle_confirmation.py` | 159 | 人工確認處理 | 職責單一 |

---

## 4. 數據存儲結構

### 4.1 SQLite 數據庫（StateDB）— `data/state.db`

已實現 7 張表：

| 表名 | 用途 | 關鍵字段 |
|------|------|----------|
| `portfolio` | 持倉主數據 | symbol, quantity, entry_price, strategy, stop_loss, take_profit |
| `trades` | 交易歷史 | symbol, side, qty, price, pnl, timestamp |
| `trailing_stop` | 移動止損狀態 | symbol, entry_price, highest_price, sl_price, activated |
| `drawdown` | 回撤記錄（單行） | high_watermark, current_drawdown_pct, max_drawdown_pct |
| `risk_guard` | 風險保護（單行） | daily_pnl, streak, last_reset |
| `kv` | 通用鍵值存儲 | key, value（JSON） |
| `audit_log` | 審計日誌 | timestamp, action, details, old_value, new_value, source |

### 4.2 JSON 狀態文件（Legacy / Backup）

| 文件 | 用途 | 問題 |
|------|------|------|
| `data/portfolio_state.json` | 持倉備份、人類可讀 | 與 SQLite 雙寫，可能不一致 |
| `data/grid_state.json` | 網格交易狀態 | 獨立於 StateDB |
| `data/dca_state.json` | DCA 策略狀態 | 未確認是否使用 |
| `data/strategy_state.json` | 策略適配器狀態 | 與 StateDB.kv 重疊 |
| `data/loss_guard.json` | 連敗保護狀態 | 與 risk_guard 表重疊 |
| `data/drawdown_breaker.json` | 回撤斷路狀態 | 與 drawdown 表重疊 |
| `data/trailing_stops.json` | 移動止損狀態 | 與 trailing_stop 表重疊 |

### 4.3 YAML 配置文件

| 文件 | 用途 |
|------|------|
| `config/strategies.yaml` | 策略開關與參數 |
| `config/risk_limits.yaml` | 風險限額與倉位規則 |

---

## 5. 核心問題診斷

### 5.1 架構層面

| 問題 | 嚴重性 | 說明 |
|------|--------|------|
| main.py 過大（1,652行） | 🔴 High | 業務編排、CLI、交易執行全部混在一起 |
| portfolio.py 過大（773行） | 🟡 Medium | 持倉管理、PnL、風險檢查、狀態持久化耦合 |
| 無 ExchangeClient 接口抽象 | 🟡 Medium | 所有模塊直接依賴 BinanceClient，難以測試和替換 |
| 狀態雙寫（SQLite + JSON） | 🟡 Medium | 存在不一致風險，需統一以 SQLite 為主 |
| 策略類無異常保護 | 🟡 Medium | klines 格式異常或數據缺失時直接崩潰 |

### 5.2 安全層面

| 問題 | 嚴重性 | 說明 |
|------|--------|------|
| secrets.py 源碼截斷 | 🔴 Critical | 路徑解析不完整，可能導致密鑰加載失敗 |
| portfolio.py 導入錯誤 | 🔴 Critical | `from state_db import` 缺少 `src.` 前綴 |
| OCO 重試邏輯重複下單 | 🔴 Critical | 業務錯誤也重試 3 次 |
| 期貨 API 調用（fapi） | 🟠 High | funding_arb.py、data_feed.py 仍調用期貨公開 API |
| 硬編碼 Telegram chat_id | 🟠 High | 信息洩露風險 |

### 5.3 數據一致性

| 問題 | 嚴重性 | 說明 |
|------|--------|------|
| portfolio_state.json 與 Binance API 不同步 | 🔴 Critical | 本地緩存不能作為真實數據源 |
| StateDB 線程安全 | 🟠 High | threading.local() + check_same_thread=False 高併發競態 |
| JSON 狀態文件散落 | 🟡 Medium | 7個 JSON 文件與 SQLite 表重疊 |

---

## 6. 漸進式重構路線圖

### Phase 1: 緊急修復（本週）
1. 修復 `secrets.py`、`dynamic_coin_pool.py` 源碼截斷
2. 修復 `portfolio.py` 導入錯誤
3. 修復 `binance_client.py` OCO 重試邏輯（區分業務錯誤與網絡錯誤）
4. 添加期貨 API 環境變量閘門（默認阻止）

### Phase 2: 接口抽象（2週）
1. 定義 `ExchangeClient` Protocol（接口規範見下節）
2. `BinanceClient` 實現該 Protocol
3. 各業務模塊改為依賴 Protocol 而非具體類

### Phase 3: 狀態統一（2週）
1. 所有 JSON 狀態文件遷移至 StateDB
2. `portfolio_state.json` 降級為只讀備份（定時導出）
3. StateDB 改用連接池（sqlalchemy 或自研）

### Phase 4: 模塊拆分（持續）
1. `main.py` 拆分為：`cli.py`、`trade_executor.py`、`scan_orchestrator.py`
2. `portfolio.py` 拆分為：`position_tracker.py`、`pnl_calculator.py`、`risk_checker.py`
3. 策略類添加異常保護層

---

## 7. 接口規範（ExchangeClient Protocol）

```python
from typing import Protocol, Dict, List, Optional

class ExchangeClient(Protocol):
    """交易所客戶端接口抽象。"""
    
    # ---------- 賬戶 ----------
    def get_account(self) -> Dict: ...
    def get_free_balance(self, asset: str) -> float: ...
    
    # ---------- 市場數據 ----------
    def get_klines(self, symbol: str, interval: str, limit: int) -> List[List]: ...
    def get_24hr_stats(self, symbol: str) -> Dict: ...
    def get_ticker_price(self, symbol: str) -> float: ...
    def get_exchange_info(self) -> Dict: ...
    
    # ---------- 訂單 ----------
    def place_market_buy(self, symbol: str, quantity: float) -> Optional[Dict]: ...
    def place_market_sell(self, symbol: str, quantity: float) -> Optional[Dict]: ...
    def place_limit_buy(self, symbol: str, quantity: float, price: float) -> Optional[Dict]: ...
    def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Optional[Dict]: ...
    def place_order(self, symbol: str, side: str, order_type: str, 
                    quantity: float, price: float = None, 
                    stop_price: float = None) -> Optional[Dict]: ...
    def place_oco(self, symbol: str, quantity: float, 
                  tp_price: float, sl_price: float) -> Optional[Dict]: ...
    def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    def cancel_all_orders(self, symbol: str) -> bool: ...
    def get_open_orders(self, symbol: str = None) -> List[Dict]: ...
    def get_order(self, symbol: str, order_id: str) -> Optional[Dict]: ...
    
    # ---------- 精度 ----------
    def get_price_precision(self, symbol: str) -> int: ...
    def get_quantity_precision(self, symbol: str) -> int: ...
    def get_symbol_filters(self, symbol: str) -> Dict: ...
    
    # ---------- 工具 ----------
    def format_price(self, symbol: str, price: float) -> float: ...
    def format_quantity(self, symbol: str, qty: float) -> float: ...
```

---

## 8. 數據表結構規範（SQLite）

### 8.1 統一後的表結構

建議將所有狀態統一到 `state.db`，廢棄 JSON 文件雙寫：

```sql
-- 持倉表（擴展現有 portfolio）
CREATE TABLE portfolio (
    symbol TEXT PRIMARY KEY,
    quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    strategy TEXT NOT NULL,
    opened_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    trailing_stop_pct REAL DEFAULT 1.5,
    highest_price REAL,
    status TEXT DEFAULT 'open'  -- open | closed | liquidated
);

-- 交易歷史（現有 trades，無需修改）
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,  -- BUY | SELL
    qty REAL NOT NULL,
    price REAL NOT NULL,
    pnl REAL DEFAULT 0,
    timestamp REAL NOT NULL,
    order_id TEXT,
    strategy TEXT
);
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_time ON trades(timestamp);

-- 網格交易狀態（替代 grid_state.json）
CREATE TABLE grid_state (
    symbol TEXT PRIMARY KEY,
    status TEXT NOT NULL,  -- running | paused | stopped
    config_json TEXT NOT NULL,
    levels_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- DCA 狀態（替代 dca_state.json）
CREATE TABLE dca_state (
    symbol TEXT PRIMARY KEY,
    rounds_done INTEGER DEFAULT 0,
    total_invested REAL DEFAULT 0,
    avg_price REAL DEFAULT 0,
    next_buy_at REAL,
    status TEXT DEFAULT 'active',
    updated_at REAL NOT NULL
);

-- 移動止損（現有 trailing_stop，無需修改）
CREATE TABLE trailing_stop (
    symbol TEXT PRIMARY KEY,
    entry_price REAL NOT NULL,
    highest_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    activated INTEGER DEFAULT 0,
    updated_at REAL NOT NULL
);

-- 回撤斷路（替代 drawdown_breaker.json）
CREATE TABLE drawdown (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_watermark REAL DEFAULT 0,
    current_drawdown_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    tripped_count INTEGER DEFAULT 0,
    tripped_at REAL,
    reset_at REAL,
    history TEXT,  -- JSON array
    updated_at REAL NOT NULL
);

-- 風險保護（替代 loss_guard.json）
CREATE TABLE risk_guard (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_pnl REAL DEFAULT 0,
    streak INTEGER DEFAULT 0,
    last_reset REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- 策略適配器狀態（替代 strategy_state.json）
CREATE TABLE strategy_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,  -- JSON
    updated_at REAL NOT NULL
);

-- 通用 KV（現有 kv，無需修改）
CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 審計日誌（現有 audit_log，無需修改）
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    details TEXT,
    old_value TEXT,
    new_value TEXT,
    source TEXT DEFAULT 'system'
);
CREATE INDEX idx_audit_time ON audit_log(timestamp);
```

### 8.2 遷移計劃

| 源（JSON） | 目標（SQLite） | 優先級 |
|-----------|---------------|--------|
| `grid_state.json` | `grid_state` 表 | High |
| `dca_state.json` | `dca_state` 表 | Medium |
| `strategy_state.json` | `strategy_state` 表 | Medium |
| `loss_guard.json` | `risk_guard` 表 | Low（已有表，只需停寫 JSON） |
| `drawdown_breaker.json` | `drawdown` 表 | Low（已有表，只需停寫 JSON） |
| `trailing_stops.json` | `trailing_stop` 表 | Low（已有表，只需停寫 JSON） |
| `portfolio_state.json` | `portfolio` 表 + 定時導出 | Low（已有表，改為只讀備份） |

---

## 9. 模塊依賴圖

```
main.py
├── src/binance_client.py ─────┬── src/secrets.py
│                              └── (外部) binance.spot
├── src/market_scanner.py ─────┬── src/binance_client.py
│                              ├── src/multi_timeframe.py ─── src/indicators.py
│                              ├── src/dynamic_coin_pool.py
│                              └── src/data_feed.py ───────── (外部) requests
├── src/portfolio.py ──────────┬── src/state_db.py
│                              └── (外部) yaml
├── src/risk_manager.py ───────┬── src/indicators.py
│                              ├── src/drawdown_breaker.py
│                              └── src/correlation_risk.py
├── src/strategy_adaptor.py ─── src/indicators.py
├── src/notifier.py ─────────── (外部) requests
├── src/backtester.py ───────── src/binance_client.py
├── src/backtest.py ─────────── src/binance_client.py
└── src/grid_trader.py ────────┬── src/binance_client.py
                               └── src/indicators.py
```

**無循環依賴** ✅

---

## 10. 總結

| 維度 | 評估 | 建議 |
|------|------|------|
| 架構設計 | 🟡 可用，需改進 | 漸進式重構，無需推倒重來 |
| 模塊劃分 | 🟡 基本合理，顆粒度不均 | main.py / portfolio.py 需拆分 |
| 數據存儲 | 🟡 SQLite 為主，JSON 冗餘 | 統一到 SQLite，JSON 降級為備份 |
| 接口規範 | 🔴 無抽象接口 | 引入 ExchangeClient Protocol |
| 安全閘 | 🟡 SPOT ONLY 已實現 | 修復期貨 API 調用、密鑰管理 |
| 異常處理 | 🔴 覆蓋率不均 | 策略類和 backtester 需補充 |

**下一步行動：**
1. 執行 Phase 1 緊急修復（4項）
2. 定義 `ExchangeClient` Protocol 並讓 `BinanceClient` 實現
3. 將 `grid_state.json` 遷移到 `grid_state` 表
4. 拆分 `main.py` 的 CLI 邏輯到獨立模塊
