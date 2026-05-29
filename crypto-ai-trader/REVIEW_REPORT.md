# crypto-ai-trader 架構與代碼質量審查報告

**日期**: 2026-05-14  
**審查範圍**: 架構、代碼質量、安全設計、性能瓶頸、可維護性  
**代碼行數**: ~44,490 行 (src/ 27,316 + main.py 1,031 + tests/ 7,257 + scripts/ 其餘)

---

## 1. 架構評估

### 模組耦合度

| 問題 | 嚴重度 | 說明 |
|------|--------|------|
| scan_orchestrator 直接耦合 trade_executor | 🔴 高 | scan_orchestrator.py (922行) 直接 import execute_auto_trade, get_position_tier, count_active_positions，掃描邏輯與交易執行混在一起，違反單一職責 |
| main.py 仍是 God File | 🔴 高 | 1031行，import 了 10+ 個 src 模組，部分命令直接在 main.py 內實現 (cmd_sentiment, cmd_analyze, cmd_trade)，部分委託 scan_orchestrator — 職責邊界不清 |
| portfolio.py 承擔過多功能 | 🟡 中 | 31行 docstring 列出 RiskMixin, PnlMixin, StateMixin，一個類要管理倉位、風險檢查、盈虧計算、狀態持久化 |

### God Object 問題

| 檔案 | 行數 | 類名 | 問題 |
|------|------|------|------|
| src/backtest.py | 1284 | Position, ClosedTrade, BacktestEngine | 單一檔案含 3 個大類，回測引擎、倉位模型、交易記錄全混在一起 |
| src/ccxt_client.py | 1145 | BinanceClient | 與 _binance_sdk_client.py (928行) 功能高度重複，兩者都實現 SPOT API |
| src/risk_manager.py | 1093 | TrendFilter, TrailingStop, RiskManager | 4 個獨立功能模組塞在同一檔案 |
| src/scan_orchestrator.py | 922 | (函數集合) | 掃描、研究、自適應、執行全在一个 pipeline 函數中 |

### 配置管理職責劃分

| 狀態 | 說明 |
|------|------|
| ✅ 良好 | app_secrets.py 集中管理密鑰檔案路徑，有權限檢查 |
| ✅ 良好 | config.yaml 分離了 risk limits, LLM config, strategy params |
| ⚠️ 問題 | .env 含 BINANCE_BASE_URL=https://api.binance.com，但程式碼預設值是 api3.binance.com (8處)，兩者不一致 |
| ⚠️ 問題 | BINANCE_BASE_URL 在 .env 中設定為 api.binance.com，而 src/_binance_sdk_client.py 和 src/ccxt_client.py 預設值為 api3.binance.com — 如果 .env 載入失敗會用不同端點 |

---

## 2. 代碼質量

### AST 靜態異常掃描

| 指標 | 數量 | 嚴重度 |
|------|------|--------|
| `except Exception` (src/ + main.py + scripts/) | **443** | 🔴 高 |
| Bare `except:` (不含 .venv) | 1 (code_quality_guard.py) | 🟡 中 |
| `except: pass` (src/ + main.py + scripts/) | **0** | 🟢 低 |
| `except Exception` + `pass` (src/) | **0** | 🟢 低 |

**分析**: 443 個 `except Exception` 分佈極廣，平均每 62 行就有一個。雖然沒有「except: pass」這種最惡劣的模式，但 443 個寬泛異常捕獲仍然意味著：
- 大量隱藏的錯誤被吞沒
- 偵錯困難（錯誤日誌可能被覆蓋或遺漏）
- 異常被靜默處理後可能導致後續邏輯在錯誤狀態下繼續執行

### 錯誤處理一致性

| 模組 | 重試策略 | 問題 |
|------|----------|------|
| llm_client.py | 可配置 max_retries + retry_delay，含 429 處理 | ✅ 設計良好 |
| _binance_sdk_client.py | 自帶 retry 邏輯，Parse-After 回退 | ✅ 但與 llm_client 不同的重試實現 |
| trade_executor.py | 無統一重試，time.sleep(1) 散布在 4 處 | 🔴 缺乏重試機制 |
| ws_user_stream.py | 指數退避 1s→60s，2x multiplier | ✅ 設計良好 |
| param_optimizer.py | time.sleep(0.5) 硬編碼 | 🟡 硬編碼無配置 |

**問題**: 沒有統一的 retry utility，每個模組各自實現重試邏輯，行為不一致。

### 測試覆蓋率

| 指標 | 數值 |
|------|------|
| 測試檔案數 | 15 |
| 測試函數數 | 325 |
| 測試代碼行數 | 7,257 |
| src/ 代碼行數 | 27,316 |
| 測試/源碼比 | 1:3.76 |

**🔴 嚴重問題**:
1. **test_integration_recent_changes.py** 在模組頂層調用 `sys.exit()`，導致 **pytest collection 階段直接崩潰**，整個測試套件無法執行
2. 當前 **9 個測試失敗**：
   - 5 個 StrategyAdaptor regime 測試全部失敗（ImportError: cannot import name 'PortfolioState'）
   - 4 個 agent 測試失敗（異常值不匹配）
3. 測試收集需要 **28+ 秒**

---

## 3. 安全審計

### API Key 管理

| 檢查項 | 狀態 | 說明 |
|--------|------|------|
| .env 權限 | ✅ 600 | 只有 owner 可讀寫 |
| crypto-secrets.env 權限 | ✅ 600 | 只有 owner 可讀寫 |
| app_secrets.py 權限檢查 | ✅ 存在 | 拒絕 group/world readable |
| API key 洩露風險 | 🟡 中 | .env 中有 JINA_API_KEY, DEEPSEEK_API_KEY, BINANCE keys，但 .gitignore 規則正確排除了 *.env |

### SPOT ONLY 安全閘門

| 檢查項 | 狀態 | 說明 |
|--------|------|------|
| _binance_sdk_client.py | ✅ | 明確標註 "SPOT ONLY - no futures, no margin, no leverage" |
| ccxt_client.py | ✅ | 同上 |
| portfolio.py max_leverage | ✅ | 硬編碼為 1 |
| Futures API 端點使用 | 🟡 | data_feed_oi.py, data_feed_funding.py, scan_orchestrator.py, market_researcher.py 均訪問 fapi.binance.com — 但僅用於 OI/funding 數據採集（研究用途），非交易 |
| ENABLE_FUTURES 閘門 | ✅ | 三個 data_feed 模組都檢查 ENABLE_FUTURES 環境變量 |

### 外部調用安全

| 檢查項 | 狀態 | 說明 |
|--------|------|------|
| HTTP vs HTTPS | ✅ | 全部使用 https://，無明文傳輸 |
| eval/exec/pickle | ✅ | src/ 中未發現危險函數調用 |
| market_researcher IPv4 適配器 | 🟡 | 手動 monkey-patch urllib3 強制 IPv4，脆弱但非安全問題 |
| VERIFY_SSL 可配置 | 🟡 | 預設 true，但可通過環境變量關閉 — 生產環境應固定為 true |

---

## 4. 性能瓶頸

### API 調用頻率

| 位置 | 策略 | 問題 |
|------|------|------|
| trade_executor.py | time.sleep(1) × 4處 | 🔴 硬編碼延遲，無 rate limiter，無法適應不同 API 端點的限速 |
| param_optimizer.py | time.sleep(0.5) × 2處 | 🟡 硬編碼 |
| backtest.py | time.sleep(0.5) × 1處 | 🟡 硬編碼 |
| ws_user_stream.py | 指數退避 1→60s | ✅ 良好 |

**問題**: 沒有集中式 rate limiter。Binance API 對不同端點有不同的限速 (1200 req/min weight-based)，但程式碼中只有硬編碼的 sleep，沒有按端點權重計數。

### 數據庫查詢效率

| 檢查項 | 狀態 |
|--------|------|
| StateDB 索引 | ✅ trades(symbol), trades(timestamp), grid_state(symbol), dca_state(symbol), decisions(symbol/time/date/type) |
| WAL 日誌模式 | ✅ 提升並發讀寫性能 |
| 連接池管理 | ✅ 線程本地連接，5分鐘回收陳舊連接 |
| SQLite 效率 | ✅ 對當前數據量足夠，無需遷移 |

### 內存使用

| 問題 | 嚴重度 | 說明 |
|------|--------|------|
| data/cache.db 被 git 追蹤 | 🔴 高 | 114 個 data/ 下的 JSON/DB 檔案被 git 追蹤，可能包含敏感交易數據 |
| print() 語句散佈 | 🟡 中 | src/ 中有 82 個 print() 語句，部分應改為 logger |

---

## 5. 可維護性

### 文檔完整性

| 檔案 | 狀態 | 說明 |
|------|------|------|
| README.md | 🟡 簡略 | 有基本使用說明，但缺少架構圖、配置詳情、部署指南 |
| Docstring | 🟡 不一致 | 部分模組有完整 docstring（如 state_db.py），部分模組只有 1 行（如 indicators.py） |
| Inline comments | 🟡 不一致 | 關鍵邏輯有註釋，但 443 個 except Exception 中多數無說明 |

### 依賴管理

| 問題 | 嚴重度 |
|------|--------|
| requirements.txt 未固定版本 | 🔴 高 |
| requirements.lock 存在但可能過時 | 🟡 中 |
| binance-connector vs ccxt 兩套 SDK 共存 | 🟡 中 |

### 部署複雜度

| 項目 | 狀態 |
|------|------|
| 環境變量 | .env + crypto-secrets.env + config.yaml，較複雜 |
| 啟動腳本 | run_scan.sh, run_daily_report.sh 存在 |
| 測試腳手架 | ❌ 缺少 setup.py / pyproject.toml |
| CI/CD | workflows/ 有一個 crypto-audit.yaml (LLM-based audit) |

---

## 6. 歷史問題追蹤

根據先前審查記錄（t_a86608dc, t_5c782834）：

| 歷史問題 | 當前狀態 |
|----------|----------|
| FUTURES_BASE URL 錯誤 | ✅ 已修正為 https://fapi.binance.com |
| 59 個 swallowed exceptions | ✅ 已減少到 0 個 except:pass |
| test_integration sys.exit() 崩潰 | 🔴 仍然存在 — 模組頂層 sys.exit(1) 導致 pytest collection 失敗 |
| 105 個 broad except Exception | 🔴 仍為 443 個（src/ + main.py + scripts/） |

---

## 7. 優先修復建議

### 🔴 高優先級 (阻塞風險)

1. **修復 test_integration_recent_changes.py 的 sys.exit()** — 移除模組頂層的 sys.exit()，改為 `if __name__ == "__main__"` 保護
2. **修復 PortfolioState ImportError** — strategy_adaptor.py:421 嘗試 import PortfolioState 但模組中不存在
3. **requirements.txt 固定版本** — 當前未 pin 版本，生產環境可能因依賴更新而中斷
4. **git rm --cached data/cache.db data/dca_state.json** — 114 個 data/ 檔案被追蹤，可能洩露交易數據

### 🟡 中優先級

5. **建立統一 retry utility** — 替換分散在 5+ 個模組中的重試邏輯
6. **拆分 God Objects** — backtest.py (1284行), risk_manager.py (1093行), ccxt_client.py (1145行)
7. **掃描並減少 except Exception** — 443 個寬異常捕獲應縮小到具體異常類型
8. **統一 BINANCE_BASE_URL** — .env 與程式碼預設值不一致

### 🟢 低優先級

9. 改進 docstring 覆蓋率
10. 移除 src/ 中的 print() 語句，統一使用 logger
11. 建立 pyproject.toml 替代 requirements.txt

---

**結論**: 該系統具備完整的交易功能和基本的安全措施，但存在 **443 個寬異常捕獲**、**測試套件因 sys.exit() 無法完整執行**、**缺乏統一重試機制** 等代碼質量問題。安全方面，SPOT ONLY 閘門和密鑰管理設計合理，但 data/ 目錄被 git 追蹤是潛在洩露風險。建議優先修復測試崩潰和 PortfolioState ImportError。
