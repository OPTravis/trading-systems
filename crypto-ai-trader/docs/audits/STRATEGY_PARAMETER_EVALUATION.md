# 策略參數評估報告
## Strategy Adaptor Parameter Evaluation

**日期**: 2026-05-02 18:32 UTC+8  
**市場狀態**: FEAR regime (F&G=39) | BTC: Strong Uptrend ($78,219, +16.9% 30d) | Volatility: LOW (1.89% daily)  
**持倉數量**: 3/5 (TAOUSDT, PENDLEUSDT, AVAXUSDT)  
**持倉市值**: $51.24 (總成本 $49.68, PnL +3.13%)

---

## 一、當前參數 vs 策略適配器邏輯驗證

### Global Parameters

| 參數 | 適配器輸出 | 理論計算值 | 差異 | 來源 |
|------|-----------|-----------|------|------|
| score_threshold | 60 | 65 (FEAR base 68 - 3 bearish adj) → 60 (additional adj) | -5 | strategy_adaptor.py L462-545 |
| max_position_pct | 9% | 10% (FEAR base 12 - 2 bearish adj) → 9% (additional adj) | -1% | strategy_adaptor.py L462-545 |
| max_total_exposure_pct | 60% | 60% (FEAR base) | ✓ | strategy_adaptor.py L480 |
| cash_reserve_pct | 40% | 40% (FEAR base) | ✓ | strategy_adaptor.py L481 |

差異分析: score_threshold 和 max_position_pct 比理論值低，因為 F&G=39 且 funding rate < -0.05%（CROWDED_SHORT 信號觸發了額外調整：threshold -5, position_pct +3）。數值合理，偏保守方向。

### Per-Strategy Parameters

| Strategy | Enabled | size_multiplier | SL% | TP levels | Max Hold |
|----------|---------|----------------|-----|-----------|----------|
| DCA | ✅ | 0.70 | 4.0% | 2.5%/30, 5.0%/40, 10.0%/30 | 168h |
| RSI | ✅ | 0.70 | 4.0% | 2.5%/40, 5.0%/40, 10.0%/20 | 84h |
| Bollinger | ✅ | 0.60 | 4.0% | 2.5%/30, 5.0%/40, 10.0%/30 | 84h |
| VWAP | ✅ | 0.60 | 4.0% | 2.5%/40, 5.0%/40, 10.0%/20 | 36h |
| Grid | ❌ | 1.00 | 5.0% | — | — |
| Trend | ❌ | 1.00 | 5.0% | — | — |

---

## 二、各參數評估

### 1. score_threshold: 60 ✅ 合理
- FEAR regime base = 68
- BTC BEARISH overlay 減 3 → 65
- Funding rate CROWDED_SHORT 減 5 → 60
- 最終 60，偏保守但合理
- 評估: 在 F&G=39 + BTC Strong Uptrend + Negative Funding 的環境下，60 門檻允許中高質量機會進入，同時過濾掉低分噪音。
- **無需調整。**

### 2. max_position_pct: 9% ⚠️ 合理但未連接 PortfolioManager
- FEAR base 12% - bearish adj 2% = 10%，Funding adjustment → 9%
- 在 3 個持倉的分散投資下，9%/position 意味著最大總敞口 27%
- 加上 cash_reserve 40%，這意味著最多動用 60% 資金
- **問題: 此參數目前僅在適配器輸出中存在，未連接到 PortfolioManager 實際風控**
- PortfolioManager 使用 config/risk_limits.yaml 的 max_position_pct: 15%
- **建議: 每次 adapt() 後更新 PortfolioManager 的 config**

### 3. position_scale: 硬編碼 ⚠️ 偏保守但可用
- 當前 3 個持倉 → scale = 0.65 (trade_executor.py L113)
- 4 個持倉 → scale = 0.35
- 此硬編碼與 regime 無關，在 FEAR 和 GREED 中都一樣
- **建議: 根據 regime 動態調整**
  - FEAR regime: 更保守 (0.55, 0.40, 0.30, 0.20)
  - NEUTRAL regime: 保持當前 (0.80, 0.65, 0.50, 0.35)
  - GREED regime: 稍激進 (0.90, 0.75, 0.60, 0.45)

### 4. size_multiplier: 0.6-0.7 ❌ 未被使用 (CRITICAL BUG)
- 適配器精心計算: DCA=0.7, RSI=0.7, Bollinger=0.6, VWAP=0.6
- 這些乘數應在交易執行時進一步縮小倉位
- **BUG: scan_orchestrator.py L437 提取了 size_multiplier 但未傳遞給 execute_auto_trade()**
  - L437: `size_multiplier = strategy_cfg["size_multiplier"]`
  - L479: `execute_auto_trade(symbol, price, strategy, ...)` — 缺少 size_multiplier 參數
- execute_auto_trade() (trade_executor.py L75) 不接受 size_multiplier 參數
- invest_pct 計算 (L119): `invest_pct = base_pct * position_scale * fee_reserve` — 無 size_multiplier
- **影響: 在 FEAR regime 下，所有策略的實際倉位比預期大 30-40%**

---

## 三、當前持倉表現 (T2 Handoff)

| Symbol | 數量 | 入場價 | 現價 | PnL | SL | TP | 風報比 |
|--------|------|--------|------|-----|----|----|--------|
| TAOUSDT | 0.0864 | $263.10 | $272.60 | +3.61% | $249.94 (-5.0%) | $278.89 (+6.0%) | 1:1.20 |
| PENDLEUSDT | 9.6351 | $1.45 | $1.53 | +6.02% | $1.37 (-5.0%) | $1.53 (+6.0%) | 1:1.20 |
| AVAXUSDT | 1.4191 | $9.18 | $9.11 | -0.79% | $8.84 (-3.7%) | $9.86 (+7.4%) | 1:1.99 |

**覆蓋率:** 3/3 SL (100%), 3/3 TP (100%)  
**總體:** PnL +$1.56 (+3.13%), 回撤 0.20%

觀察:
- PENDLEUSDT 已觸及止盈目標 ($1.5330 >= TP $1.5328)
- AVAXUSDT 接近止損 (距 SL 僅 3.05%)
- TAOUSDT 接近止盈 (距 TP 僅 2.31%)
- 止損範圍 3.7-5.0% 在 LOW volatility regime 下偏寬，但 FEAR regime 下合理

---

## 四、調整建議

### 立即修復 (Critical)
1. **連接 size_multiplier 到交易執行流程**
   - 方案 A (推薦): 在 scan_orchestrator.py L479 的 execute_auto_trade() 調用中傳入 size_multiplier
   - 在 execute_auto_trade() 中添加 size_multiplier 參數，L119 改為:
     `invest_pct = base_pct * position_scale * size_multiplier * fee_reserve`
   - 方案 B: 在 scan_orchestrator.py 中直接在 invest_pct 計算前乘入 size_multiplier

### 建議調整 (Medium Priority)
2. **position_scale 改為動態**
   - FEAR regime: position_scale 更保守 (0.55, 0.40, 0.30, 0.20)
   - NEUTRAL regime: 保持當前 (0.80, 0.65, 0.50, 0.35)
   - GREED regime: 可稍激進 (0.90, 0.75, 0.60, 0.45)

3. **max_position_pct 應連接到 PortfolioManager**
   - 目前 PortfolioManager 有獨立的 max_position_pct (默認 30%, config 設 15%)
   - 適配器動態輸出的 9% 完全未生效
   - 建議在每次 adapt() 後更新 PortfolioManager 的 config

### 可選優化 (Low Priority)
4. **score_threshold 可進一步微調**
   - FEAR+BEARISH+Negative Funding 時，當前 60 可保持
   - 如果 F&G 從 39 升至 45+ (接近 NEUTRAL)，應自動升至 68+
   - 已由適配器自動處理，無需手動干預

---

## 五、結論

| 參數 | 狀態 | 行動 |
|------|------|------|
| score_threshold | ✅ 正常工作 | 無需調整 |
| max_position_pct | ⚠️ 未生效 | 需連接到 PortfolioManager |
| position_scale | ⚠️ 硬編碼 | 建議改為 regime 動態 |
| size_multiplier | ❌ 未使用 | **CRITICAL: 需修復傳遞鏈** |

**最高優先級**: 修復 size_multiplier 的傳遞，否則 FEAR regime 下倉位比預期大 30-40%。

**綜合評估**: 在當前 FEAR + BTC Strong Uptrend + Negative Funding 環境下，策略參數整體偏保守方向是合理的。score_threshold=60 允許優質機會進入，size_multiplier 的 BUG 導致實際倉位過大是最大風險。建議優先修復 size_multiplier 傳遞鏈，其次考慮 position_scale 動態化。
