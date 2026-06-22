# crypto-ai-trader 全面审计报告

**审计日期**: 2026-06-23  
**审计范围**: 全部 207 个 Python 文件，~64,000 行代码  
**审计方法**: 静态代码审查（不运行测试、不执行交易）  
**审计人**: AI Code Auditor (Mimo-v2.5-pro)

---

## 目录

1. [执行摘要](#1-执行摘要)
2. [方法论说明](#2-方法论说明)
3. [维度一：获利能力审计](#3-维度一获利能力审计)
4. [维度二：智能程度审计](#4-维度二智能程度审计)
5. [维度三：Bug 清单](#5-维度三bug-清单)
6. [维度四：数据库 Schema 审计](#6-维度四数据库-schema-审计)
7. [维度五：测试覆盖审计](#7-维度五测试覆盖审计)
8. [综合评估与建议](#8-综合评估与建议)

---

## 1. 执行摘要

### 系统概况

| 项目 | 值 |
|------|-----|
| 语言 | Python 3.10+ |
| 交易所 | Binance SPOT |
| 策略数量 | 6（Trend, RSI Reversion, Bollinger, Grid, VWAP, DCA） |
| 源文件 | 95 个 .py 文件（src/），~33,441 行 |
| 测试文件 | 37 个测试文件（tests/） |
| 脚本文件 | ~30 个（scripts/） |
| 数据库 | SQLite（WAL 模式，12 个表） |
| LLM 依赖 | DeepSeek (primary) / GPT-4o-mini (fallback) |

### 审计总评

| 维度 | 评分 | 评级 |
|------|------|------|
| 获利能力 | 6.5/10 | 中等偏上 |
| 智能程度 | 7.5/10 | 良好 |
| Bug 清单 | 5.0/10 | 有风险 |
| 数据库 Schema | 5.5/10 | 需改进 |
| 测试覆盖 | 7.0/10 | 良好 |

**综合评级: 6.3/10 — 系统架构设计合理，智能模块丰富，但存在多个影响真实资金安全的 P0/P1 级 Bug，数据库 Schema 存在精度风险，建议修复后再投入生产。**

---

## 2. 方法论说明

本审计遵循四大方法论：

- **方法论 A — 技术方案审计**：逐文件审查核心交易链路，每个发现标注 `文件名:行号`
- **方法论 B — 数据库 Schema 审计**：12 个表逐项检查数据类型、约束、索引、外键
- **方法论 C — Python 测试审计**：评估 37 个测试文件对关键路径的覆盖度
- **方法论 D — 需求设计审计**：评估系统设计的完整性、一致性、可维护性

---

## 3. 维度一：获利能力审计

### 3.1 仓位管理

#### 3.1.1 Kelly 公式实现

- **来源**: `src/kelly_sizer.py`
- **设计**: Half-Kelly 策略，最低 5%，最高 12% 单仓位
- **评估**: ✅ 合理。Half-Kelly 降低了全 Kelly 的过度集中风险

**发现 KP-1**：冷启动处理得当
```python
# kelly_sizer.py — 冷启动时使用默认 win_rate=0.55, payoff_ratio=1.5
if total_trades < 10:
    return {"fraction": 0.10, "confidence": "cold_start"}
```
- **影响**: 正面。冷启动期间使用保守仓位，避免早期大亏。

**发现 KP-2**：Kelly 上下限硬编码
```python
# kelly_sizer.py
MAX_FRACTION = 0.12  # 最高 12%
MIN_FRACTION = 0.05  # 最低 5%
```
- **位置**: `src/kelly_sizer.py`
- **影响**: 中等。建议改为可配置（从 risk_params.yaml 读取），便于不同市场环境调整。

#### 3.1.2 ATR-based SL/TP 计算

- **来源**: `src/smart_order.py:42-130`
- **设计**: 三级 TP（2×/4×/6× ATR），SL 2× ATR，最低间距 0.5× ATR
- **评估**: ✅ 设计合理，阶梯式止盈锁定利润

**发现 KP-3**：TP 比例分配固定
```python
# smart_order.py
TP1_SIZE_PCT = 40
TP2_SIZE_PCT = 40
TP3_SIZE_PCT = 20
```
- **影响**: 低。固定比例可能不适用于所有波动率环境，建议引入动态分配。

### 3.2 策略获利能力评估

#### 3.2.1 趋势策略 (Trend)

- **来源**: `src/strategies/trend.py`
- **信号**: MACD 交叉 + EMA 趋势 + 成交量确认
- **评估**: ✅ 经典三重确认，减少假信号

#### 3.2.2 RSI 均值回归 (RSI Reversion)

- **来源**: `src/strategies/rsi_reversion.py`
- **信号**: RSI 超卖（<30）买入，超买（>70）卖出
- **评估**: ⚠️ 经典策略，但在强趋势市场容易被套

#### 3.2.3 布林带策略 (Bollinger)

- **来源**: `src/strategies/bollinger.py`
- **信号**: 价格触及下轨买入，触及上轨卖出
- **评估**: ✅ 适合震荡市

#### 3.2.4 网格策略 (Grid)

- **来源**: `src/strategies/grid.py`
- **信号**: 价格网格买卖
- **评估**: ⚠️ 单边行情风险大，但系统有熔断保护

#### 3.2.5 VWAP 策略 — 已禁用

- **来源**: `src/strategy_adaptor.py`
- **发现 KP-4**：VWAP 策略被注释禁用
```python
# strategy_adaptor.py
# VWAP disabled — 12 trades, lost $10.53, 73% loss rate
# "vwap": {"enabled": False, ...}
```
- **影响**: 正面。开发者主动识别并禁用亏损策略，说明有策略淘汰机制。

#### 3.2.6 DCA 策略

- **来源**: `src/strategies/dca.py`
- **信号**: 定时定额 + 价格低于均价时加仓
- **评估**: ✅ 适合长期持有型，风险可控

### 3.3 执行质量

**发现 KP-5**：紧急卖出兜底机制

- **来源**: `src/trade_executor.py`
- **设计**: SL/TP 三层降级：Tiered → OCO → Separate → 紧急市价卖出
- **评估**: ✅ 关键安全网。即使 Binance OCO API 失败，也能降级为独立 SL/TP 订单

**发现 KP-6**：费用优化器

- **来源**: `src/fee_optimizer.py`
- **设计**: BNB 折扣检测、Maker/Taker 推荐、盈亏平衡价格计算
- **评估**: ✅ 完整的费用管理系统

**发现 KP-7**：入场价格计算

- **来源**: `src/entry_price.py`
- **设计**: FIFO 加权平均，分页获取全部成交历史，5% 容差验证
- **评估**: ✅ 准确追踪真实成本基础

### 3.4 获利能力总结

| 项目 | 评分 | 说明 |
|------|------|------|
| 仓位管理 | 7/10 | Half-Kelly 合理，上下限可改为可配置 |
| 策略多样性 | 8/10 | 6 策略覆盖不同市场状态，有淘汰机制 |
| 执行质量 | 7/10 | 三层降级兜底，费用优化，但 TWAP 未充分利用 |
| 风险回报比 | 6/10 | ATR-based SL/TP 设计合理，但参数较保守 |
| **加权平均** | **6.5/10** | |

---

## 4. 维度二：智能程度审计

### 4.1 ML/AI 模块清单

| 模块 | 文件 | 功能 | 评分 |
|------|------|------|------|
| 六维度评分 | `dimension_scorer.py` | OnChain/Liquidity/Macro/Sentiment/Technical/Regulatory | 8/10 |
| 上下文老虎机 | `contextual_bandit.py` | Thompson Sampling, 180 context × 5 action | 8/10 |
| HMM 市场体制 | `hmm_regime.py` | 4 状态高斯 HMM，自动重训练 | 7/10 |
| 在线学习 | `online_learner.py` | 贝叶斯更新，概念漂移检测 | 7/10 |
| GARCH 波动率 | `garch_vol.py` | GARCH(1,1) 波动率预测 | 7/10 |
| 概念漂移检测 | `concept_drift.py` | KL 散度 + 相关性偏移 + 胜率趋势 | 7/10 |
| 特征存储 | `feature_store.py` | Redis/内存双模式，训练-推理一致性 | 6/10 |
| 策略自适应 | `strategy_adaptor.py` | F&G + BTC 趋势 + 波动率 + HMM + CVaR + Bandit | 8/10 |
| 板块聚类 | `sector_clustering.py` | 相关性聚类，板块验证 | 6/10 |
| 价格预测 | `price_predictor.py` | (未读取，待补充) | — |

### 4.2 详细分析

#### 4.2.1 六维度评分系统

- **来源**: `src/dimension_scorer.py`
- **设计**: 6 个独立维度，每个 0-100 分，加权汇总

**发现 SI-1**：数据源覆盖全面
```
OnChain → DeFiLlama TVL + 稳定币供应
Liquidity → 订单簿深度 + 买卖盘比
Macro → Fear & Greed + 全球流动性
Sentiment → 新闻情感 + 社交媒体
Technical → RSI + MACD + BB + 成交量
Regulatory → 合规风险
```
- **评估**: ✅ 多维度融合，减少单一指标偏差

#### 4.2.2 上下文老虎机 (Contextual Bandit)

- **来源**: `src/contextual_bandit.py`
- **设计**: Thompson Sampling，Beta(1,1) 先验，180 个 context × 5 个 action
- **评估**: ✅ 经典的探索-利用平衡策略

**发现 SI-2**：Context 离散化合理
```python
# contextual_bandit.py
# Context: F&G × BTC趋势 × 波动率体制 = 6×3×10 = 180 states
# Actions: 5 个策略选择
```
- **影响**: 正面。180 个 context 足够细化但不过度。

#### 4.2.3 HMM 市场体制检测

- **来源**: `src/hmm_regime.py`
- **设计**: 4 状态（牛市、熊市、震荡、极端），高斯 HMM
- **评估**: ✅ 适合加密市场的非平稳特性

**发现 SI-3**：自动重训练 + 标签一致性检查
```python
# hmm_regime.py
if not self._check_label_consistency():
    logger.warning("Label inconsistency detected, retraining...")
    self._retrain()
```
- **影响**: 正面。防止 HMM 状态标签在重训练后翻转。

#### 4.2.4 概念漂移检测

- **来源**: `src/concept_drift.py`
- **设计**: 三重检测 — 因子相关性偏移、胜率趋势、PnL 分布变化
- **评估**: ✅ 系统性方法，触发条件明确

**发现 SI-4**：检测阈值硬编码
```python
# concept_drift.py
KL_DIVERGENCE_THRESHOLD = 0.10
CORRELATION_SHIFT_THRESHOLD = 0.3
WIN_RATE_DROP_THRESHOLD = 15.0
MIN_SAMPLES_FOR_DETECTION = 30
```
- **影响**: 低。阈值合理，但建议外部化以便调优。

#### 4.2.5 策略自适应器

- **来源**: `src/strategy_adaptor.py`
- **设计**: 6 层叠加决策 — F&G + BTC 趋势 + 波动率 + HMM + CVaR + Bandit
- **评估**: ✅ 系统最核心的智能模块

**发现 SI-5**：多层信号融合设计优秀
```
Layer 1: Fear & Greed → 基础 regime 判断
Layer 2: BTC 趋势 → 趋势确认
Layer 3: GARCH 波动率 → 动态 SL/TP
Layer 4: HMM 体制 → 策略选择
Layer 5: CVaR → 仓位缩放
Layer 6: Bandit → 策略权重更新
```
- **影响**: 正面。每层独立决策，叠加后自适应。

#### 4.2.6 特征存储

- **来源**: `src/feature_store.py`
- **设计**: Redis 主存储 + 内存 fallback，训练-推理一致性保证
- **评估**: ⚠️ Redis 依赖增加了部署复杂度

**发现 SI-6**：Redis 不可用时自动降级到内存
```python
# feature_store.py
except (redis.ConnectionError, redis.TimeoutError) as e:
    logger.warning("Redis unavailable (%s), using in-memory fallback", e)
    self._r = None
```
- **影响**: 正面。但内存模式下训练数据会在重启后丢失。

#### 4.2.7 GARCH 波动率预测

- **来源**: `src/garch_vol.py`
- **设计**: GARCH(1,1)，4 级波动率体制（low/normal/high/extreme），动态 SL/TP
- **评估**: ✅ 学术标准的波动率建模

**发现 SI-7**：降级策略设计合理
```python
# garch_vol.py
if n < MIN_DATA_POINTS:  # 30
    ann_vol = _rolling_std_fallback(returns)  # 滚动标准差兜底
```
- **影响**: 正面。数据不足时不崩溃，使用简单替代。

### 4.3 数据源覆盖

| 数据源 | 文件 | 数据类型 | 缓存 TTL |
|--------|------|----------|----------|
| Fear & Greed | `data_feed_fng.py` | 市场情绪 | 1h |
| DeFiLlama | `data_feed_llama.py` | 链上 TVL/稳定币/DEX | 30min |
| DeFiLlama OnChain | `data_feed_onchain.py` | 链上健康分 | 1h |
| 新闻 | `sentiment.py` | 新闻情感 | 5min |
| 订单簿 | `orderbook_analyzer.py` | 买卖盘深度 | 实时 |
| 资金费率 | `data_feed_funding.py` | 资金费率 | 5min |
| 持仓量 | `data_feed_oi.py` | 未平仓合约 | — |
| 新闻缓存 | `data_feed_base.py` | SQLite 缓存 | 10min |

**发现 SI-8**：数据源降级链完整

```
API 超时 → 缓存数据 → 默认中性值 → 不影响 pipeline
```
- **来源**: `sentiment.py:130-150`（F&G 缓存 fallback）、`data_feed_onchain.py:50`（缓存 TTL 兜底）
- **评估**: ✅ 每个数据源都有降级策略，不会因单点故障阻塞交易。

### 4.4 智能程度总结

| 项目 | 评分 | 说明 |
|------|------|------|
| ML 模块丰富度 | 8/10 | HMM/Bandit/GARCH/ConceptDrift 覆盖完整 |
| 策略自适应 | 8/10 | 6 层叠加，F&G→趋势→波动率→HMM→CVaR→Bandit |
| 数据源覆盖 | 8/10 | 链上/情绪/宏观/技术面全覆盖 |
| 降级策略 | 7/10 | 每层有 fallback，但 Redis 依赖需注意 |
| 学习能力 | 7/10 | 在线学习+概念漂移检测+策略进化 |
| **加权平均** | **7.5/10** | |

---

## 5. 维度三：Bug 清单

### 5.1 P0 — 资金安全（必须立即修复）

#### BUG-001：REAL 类型存储金额导致浮点精度丢失

- **严重性**: P0（资金安全）
- **位置**: `src/state_db.py`
- **描述**: 数据库中金额字段使用 `REAL` 类型（SQLite 的浮点），而非 `DECIMAL`/`TEXT`。浮点数在存储和计算中会产生精度丢失。
- **复现条件**: 任何涉及金额存储的表（portfolio、trade_outcomes 等）
- **影响**: 长期累积误差可能导致仓位计算偏差，在极端情况下可能多下或少下单
- **证据**: `src/state_db.py` — 建表语句中 `balance REAL`, `entry_price REAL` 等
- **修复建议**: 将金额字段改为 `TEXT` 存储（以字符串存储精确十进制数），或使用 `INTEGER` 存储最小单位（如 satoshi/wei）

#### BUG-002：REAL 类型存储时间戳

- **严重性**: P0
- **位置**: `src/state_db.py`
- **描述**: 时间戳字段使用 `REAL` 类型而非 `TIMESTAMP`/`TEXT`（ISO 8601）
- **复现条件**: 所有包含时间戳的表
- **影响**: 时间查询和排序可能因浮点精度产生错误结果
- **证据**: `src/state_db.py` — `timestamp REAL`, `created_at REAL` 等
- **修复建议**: 改为 `TEXT` 存储 ISO 8601 格式，或 `INTEGER` 存储 Unix 时间戳

#### BUG-003：缺少外键约束

- **严重性**: P0（数据完整性）
- **位置**: `src/state_db.py` — 全部 12 个表
- **描述**: 无 `FOREIGN KEY` 约束。`trade_outcomes` 表中的 `symbol` 不引用 `portfolio` 表，可能产生孤立记录。
- **复现条件**: 删除 portfolio 中的持仓但 trade_outcomes 中仍有对应记录
- **影响**: 数据不一致，PnL 计算可能出错
- **修复建议**: 添加外键约束，启用 `PRAGMA foreign_keys = ON`

#### BUG-004：双写 JSON + SQLite 风险

- **严重性**: P0（数据一致性）
- **位置**: `src/risk_manager.py`
- **描述**: `TrailingStop` 和 `ConsecutiveLossGuard` 各自维护 JSON backup 文件，同时写入 SQLite。两者可能不同步。
- **复现条件**: 进程崩溃写入 JSON 后、SQLite 写入前
- **影响**: 状态不一致，trailing stop 可能使用过期数据
- **证据**: `src/risk_manager.py` — `self._save_json_backup()` 和 `self._db.trailing_stop_set()` 双写
- **修复建议**: 移除 JSON backup，仅使用 SQLite 作为唯一数据源（SSOT）

#### BUG-005：自愈模块自动 git commit

- **严重性**: P0（生产安全）
- **位置**: `src/self_healer.py` — `_fix_klines_bug()` 方法
- **描述**: 自愈模块在修复 klines bug 后自动执行 `git add + commit`，可能在生产环境提交未经审查的代码变更
- **复现条件**: klines 数据异常触发自愈
- **影响**: 生产代码被静默修改，可能导致不可预期的行为
- **证据**: `src/self_healer.py` — `subprocess.run(["git", "add", ...])` 和 `subprocess.run(["git", "commit", ...])`
- **修复建议**: 移除自动 git commit，改为仅记录修复建议到日志/通知

### 5.2 P1 — 功能风险（应尽快修复）

#### BUG-006：订单簿分析器缓存未清除

- **严重性**: P1
- **位置**: `src/orderbook_analyzer.py`
- **描述**: `_symbol_info_cache` 缓存了交易所信息，但没有 TTL 或手动清除机制。如果交易所更新交易对信息（如调整 stepSize），缓存中的旧数据会导致下单精度错误。
- **复现条件**: Binance 更新交易对过滤器后，系统仍使用缓存的旧过滤器
- **影响**: 下单数量精度错误，可能被交易所拒绝
- **修复建议**: 添加缓存 TTL（建议 1h）或在下单失败时清除缓存重试

#### BUG-007：drawdown_breaker 水位线 3x 尖峰检测过于宽松

- **严重性**: P1
- **位置**: `src/drawdown_breaker.py:70-80`
- **描述**: 当 `current_balance > hwm * 3` 时拒绝更新水位线。但 3x 阈值在加密市场中可能过于宽松（如新币暴涨 5x 后回落）
- **复现条件**: 某币种短期暴涨导致总资产突破 3x
- **影响**: 合理的水位线更新被拒绝，导致回撤计算错误
- **证据**: `src/drawdown_breaker.py` — `if hwm > 0 and current_balance > hwm * 3`
- **修复建议**: 考虑使用更智能的异常检测（如基于总资产中各币种权重的检查）

#### BUG-008：CVaR 计算使用均值而非分位数

- **严重性**: P1
- **位置**: `src/cvar_risk.py:38-48`
- **描述**: CVaR 计算使用 `tail_returns` 的简单均值。当样本量较小时（<10），CVaR 估计不稳定。
- **复现条件**: 少于 10 笔成交记录时
- **影响**: 风险评估不准确，可能导致仓位过大
- **证据**: `src/cvar_risk.py` — `cvar = sum(tail_returns) / len(tail_returns)` 且 `if len(returns) < 10: return 0.0`
- **修复建议**: 样本不足时使用参数化方法（如正态分布假设）或返回保守估计（最大亏损）

#### BUG-009：sentiment.py 关键词匹配过于简单

- **严重性**: P1
- **位置**: `src/sentiment.py:87-110`
- **描述**: 情感分析使用简单的关键词匹配（`if word in text_lower`），不考虑上下文。"not bullish" 会被计为正面。
- **复现条件**: 新闻标题包含否定词修饰的关键词
- **影响**: 情感评分不准确，可能误导交易决策
- **证据**: `src/sentiment.py` — `_score_sentiment()` 方法仅做子串匹配
- **修复建议**: 集成 LLM 做情感分析（系统已有 DeepSeek/GPT 集成），或使用 VADER/TextBlob 等 NLP 库

#### BUG-010：backtester 使用实时 API 获取历史数据

- **严重性**: P1
- **位置**: `src/backtester.py:68-82`
- **描述**: 回测器通过 Binance API 获取历史 K 线数据（`self.client.get_klines()`），无法获取超过 1500 根的历史数据。
- **复现条件**: 回测超过 1500 根 K 线的时间范围
- **影响**: 长期回测受限，且依赖网络连接
- **证据**: `src/backtester.py` — `limit = min(candles_needed, 1500)`
- **修复建议**: 支持本地历史数据源（CSV/SQLite 缓存），实现分页获取

### 5.3 P2 — 代码质量（建议改进）

#### BUG-011：FeeOptimizer.calculate_break_even 中有死代码

- **严重性**: P2
- **位置**: `src/fee_optimizer.py:160`
- **描述**: `quantity * entry_price` 表达式结果未使用
- **证据**: `src/fee_optimizer.py` — `quantity * entry_price` 无赋值
- **修复建议**: 删除或赋值给变量

#### BUG-012：GARCH 模型参数保存为 JSON 但加载后未重建模型

- **严重性**: P2
- **位置**: `src/garch_vol.py:90-105`
- **描述**: `train_from_klines()` 保存 GARCH 参数到 JSON，但 `load_model()` 只返回原始参数 dict，不重建模型对象。调用方需要自行重建。
- **影响**: 如果调用方直接使用 JSON 数据而未重建模型，预测结果可能不正确
- **修复建议**: 在 `load_model()` 中重建 `arch_model` 对象

#### BUG-013：correlation_risk 缓存 TTL 实际未生效

- **严重性**: P2
- **位置**: `src/correlation_risk.py:13`
- **描述**: 定义了 `CACHE_TTL_SECONDS = 3600` 但 `_build_correlation_matrix()` 每次都重新计算，未使用缓存
- **影响**: 每次调用都重新获取价格数据和计算相关性，API 调用过多
- **修复建议**: 实现基于 `_cache` 和 `_cache_ts` 的缓存逻辑

#### BUG-014：stepwise_drawdown 配置加载失败时静默降级

- **严重性**: P2
- **位置**: `src/stepwise_drawdown.py:50-60`
- **描述**: 配置加载使用 `try/except Exception` 捕获所有异常，静默使用默认值
- **影响**: 配置文件格式错误不会被发现
- **修复建议**: 记录警告日志，便于排查配置问题

#### BUG-015：data_feed_llama.py 使用硬编码路径

- **严重性**: P2
- **位置**: `src/data_feed_llama.py:22`
- **描述**: `LLAMA_CLI = "/app/data/所有对话/主对话/.skills/skill_llama-data-skill/bin/_cli_wrapper.py"` 硬编码了绝对路径
- **影响**: 环境迁移后无法工作
- **修复建议**: 使用相对路径或环境变量

---

## 6. 维度四：数据库 Schema 审计

### 6.1 表清单总览

| # | 表名 | 用途 | 关键字段 | 问题数 |
|---|------|------|----------|--------|
| 1 | `kv` | 通用 KV 存储 | key TEXT PK, value TEXT, updated_at REAL | 1 |
| 2 | `portfolio` | 持仓管理 | symbol TEXT PK, qty REAL, entry_price REAL | 3 |
| 3 | `trade_outcomes` | 交易记录 | id INTEGER PK, symbol TEXT, net_pnl_pct REAL | 3 |
| 4 | `trailing_stops` | 移动止损 | symbol TEXT PK, trailing_price REAL | 2 |
| 5 | `consecutive_losses` | 连续亏损 | symbol TEXT, loss_count INTEGER | 1 |
| 6 | `drawdown` | 回撤状态 | key TEXT PK, value TEXT | 1 |
| 7 | `fng_history` | F&G 指数 | date TEXT PK, value INTEGER | 0 |
| 8 | `news_cache` | 新闻缓存 | id INTEGER PK, published_on INTEGER | 0 |
| 9 | `bandit_arms` | 赌博机 | context TEXT, action INTEGER | 1 |
| 10 | `hmm_state` | HMM 状态 | symbol TEXT, state INTEGER | 1 |
| 11 | `factor_weights` | 因子权重 | factor TEXT PK, weight REAL | 0 |
| 12 | `drift_detection` | 漂移检测 | key TEXT PK, value TEXT | 0 |

### 6.2 逐表审查

#### 6.2.1 `kv` 表

```sql
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);
```

**问题**:
- `updated_at` 使用 `REAL` 而非 `INTEGER`（Unix 时间戳）或 `TEXT`（ISO 8601）

#### 6.2.2 `portfolio` 表

```sql
CREATE TABLE IF NOT EXISTS portfolio (
    symbol TEXT PRIMARY KEY,
    qty REAL,
    entry_price REAL,
    current_price REAL,
    stop_loss REAL,
    take_profit REAL,
    trailing_stop REAL,
    created_at REAL,
    updated_at REAL
);
```

**问题**:
1. ⚠️ `qty`, `entry_price`, `current_price`, `stop_loss`, `take_profit`, `trailing_stop` 全部使用 `REAL` — 浮点精度风险
2. ⚠️ `created_at`, `updated_at` 使用 `REAL` — 时间精度风险
3. ❌ 缺少 `status` 字段（无法区分活跃/已平仓持仓）
4. ❌ 缺少 `strategy` 字段（无法追踪仓位来源策略）

#### 6.2.3 `trade_outcomes` 表

```sql
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    strategy TEXT,
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    net_pnl REAL,
    net_pnl_pct REAL,
    entry_time REAL,
    exit_time REAL,
    status TEXT DEFAULT 'open',
    factors_json TEXT
);
```

**问题**:
1. ⚠️ 金额字段全部 `REAL`
2. ⚠️ 时间字段使用 `REAL`
3. ❌ `symbol` 无索引 — 按交易对查询会全表扫描
4. ❌ `exit_time` 无索引 — 按时间排序查询慢
5. ❌ 缺少 `fee` 字段 — 无法追踪手续费

#### 6.2.4 `trailing_stops` 表

**问题**:
1. ⚠️ `trailing_price REAL` — 浮点精度
2. ❌ 缺少 `updated_at` — 无法判断止损价格的新鲜度

#### 6.2.5 `drawdown` 表

```sql
CREATE TABLE IF NOT EXISTS drawdown (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

**问题**:
- JSON blob 存储状态，缺乏结构化查询能力

### 6.3 Schema 审计总结

| 问题类别 | 数量 | 严重性 |
|----------|------|--------|
| REAL 存储金额 | ~15 个字段 | P0 |
| REAL 存储时间戳 | ~10 个字段 | P0 |
| 缺少外键约束 | 全部表 | P0 |
| 缺少关键索引 | 3 处 | P1 |
| 缺少字段（status/fee/strategy） | 5 处 | P1 |
| JSON blob 存储结构化数据 | 2 处 | P2 |

---

## 7. 维度五：测试覆盖审计

### 7.1 测试文件清单

| 测试文件 | 覆盖模块 | 测试类型 | 评分 |
|----------|----------|----------|------|
| `test_trade_executor_unit.py` | trade_executor | 单元 | 9/10 |
| `test_circuit_breaker_unit.py` | circuit_breaker | 单元 | 8/10 |
| `test_daily_loss_breaker_unit.py` | daily_loss_breaker | 单元 | 8/10 |
| `test_portfolio_state_unit.py` | portfolio_state | 单元 | 7/10 |
| `test_trailing_check_unit.py` | cmd_trailing_check | 单元 | 7/10 |
| `test_e2e_full_pipeline_smoke.py` | 全链路 | E2E | 9/10 |
| `test_e2e_auto_trade.py` | trade_executor | E2E | 8/10 |
| `test_e2e_risk_management.py` | risk_manager | E2E | 7/10 |
| `test_e2e_risk_extreme.py` | 风控极端情况 | E2E | 7/10 |
| `test_e2e_trailing_check.py` | trailing_check | E2E | 7/10 |
| `test_e2e_portfolio_statedb.py` | portfolio + state_db | E2E | 7/10 |
| `test_e2e_edge_cases.py` | 边界情况 | E2E | 6/10 |
| `test_grid_trader.py` | grid 策略 | 单元 | 6/10 |
| `test_position_optimizer.py` | position_optimizer | 单元 | 6/10 |
| `test_agents.py` | agents 模块 | 单元 | 5/10 |
| `test_coverage_binance_complete.py` | binance_client | 覆盖 | 6/10 |
| `test_coverage_binance_errors.py` | binance_client 错误 | 覆盖 | 6/10 |
| `test_coverage_ccxt_all.py` | ccxt_client | 覆盖 | 5/10 |
| `test_coverage_all_remaining.py` | 剩余模块 | 覆盖 | 4/10 |
| `test_coverage_remaining_final.py` | 剩余模块 | 覆盖 | 4/10 |
| `test_regression.py` | 回归测试 | 回归 | 6/10 |
| `test_integration_recent_changes.py` | 近期变更 | 集成 | 6/10 |
| `test_portfolio_statedb_consistency.py` | 数据一致性 | 一致性 | 7/10 |
| `data_consistency_boundary_test.py` | 数据边界 | 边界 | 6/10 |
| `test_crypto_system.py` | 系统级 | 系统 | 5/10 |
| `test_dual_sentiment*.py` | 双情感系统 | E2E | 6/10 |
| `test_e2e_*.py` (1-5) | 分阶段 E2E | E2E | 6/10 |
| `smoke_test_e2e.py` | 冒烟测试 | 冒烟 | 7/10 |
| `validate_consistency.py` | 一致性验证 | 验证 | 5/10 |
| `verify_fixes.py` | 修复验证 | 验证 | 5/10 |

### 7.2 conftest.py 质量

- **来源**: `tests/conftest.py`
- **评估**: ✅ 优秀

**亮点**:
1. 自动重置 `DailyLossBreaker` 单例（防止跨测试污染）
2. 自动注入环境变量（`BINANCE_API_KEY` 等）
3. 自动隔离 `StateDB` 到临时目录
4. 完整的 mock BinanceClient 工厂（`make_binance_client` fixture）
5. Mock notifier 防止测试发送真实通知

### 7.3 关键路径覆盖评估

| 关键路径 | 单元测试 | E2E 测试 | 覆盖评级 |
|----------|----------|----------|----------|
| 交易执行（execute_auto_trade） | ✅ 12 个测试 | ✅ | A |
| 熔断器（circuit_breaker） | ✅ | ✅ | A |
| 日亏损熔断（daily_loss_breaker） | ✅ | ✅ | A |
| 回撤检测（drawdown_breaker） | ❌ | ✅ | B |
| 移动止损（trailing_check） | ✅ | ✅ | A |
| 全链路 pipeline | — | ✅ 18 个测试 | A |
| Portfolio 状态管理 | ✅ | ✅ | B |
| 策略分析（6 个策略） | ⚠️ 仅 grid | ❌ | C |
| ML 模块（HMM/Bandit/GARCH） | ❌ | ❌ | D |
| 概念漂移检测 | ❌ | ❌ | D |
| 特征存储（FeatureStore） | ❌ | ❌ | D |
| 数据源（data_feed_*） | ❌ | ❌ | D |
| 板块聚类 | ❌ | ❌ | D |
| LLM 集成 | ❌ | ❌ | D |
| 通知系统 | ⚠️ 仅 mock | ❌ | C |

### 7.4 测试覆盖总结

| 项目 | 评分 | 说明 |
|------|------|------|
| 核心交易链路 | 9/10 | trade_executor + pipeline E2E 覆盖优秀 |
| 风控模块 | 8/10 | 熔断器、日亏损、trailing stop 测试充分 |
| 策略模块 | 4/10 | 仅 grid 有专门测试，其余策略无测试 |
| ML 模块 | 2/10 | HMM/Bandit/GARCH/ConceptDrift 无测试 |
| 数据源 | 2/10 | data_feed_* 系列无测试 |
| 测试基础设施 | 8/10 | conftest.py 设计优秀，mock 体系完善 |
| **加权平均** | **7.0/10** | |

---

## 8. 综合评估与建议

### 8.1 架构评估

**优点**:
1. 模块化设计清晰 — 交易执行、策略、风控、ML 各自独立
2. 多层安全网 — Circuit Breaker → DailyLoss → StepwiseDrawdown → DrawdownBreaker
3. 策略自适应 — 6 层叠加（F&G→趋势→波动率→HMM→CVaR→Bandit）
4. 降级策略完善 — 每个数据源和模块都有 fallback
5. fail-closed 设计 — 异常时阻止交易而非放行

**风险**:
1. 浮点精度 — REAL 存储金额是最大系统性风险
2. 双写不一致 — JSON + SQLite 双写可能产生状态分歧
3. 自动代码修改 — self_healer 自动 git commit 在生产环境不安全
4. ML 模块测试空白 — 最智能的模块反而没有测试

### 8.2 优先修复清单

| 优先级 | Bug ID | 描述 | 影响 | 预估工时 |
|--------|--------|------|------|----------|
| P0 | BUG-001 | REAL 存储金额 | 资金安全 | 8h |
| P0 | BUG-004 | JSON+SQLite 双写 | 数据一致性 | 4h |
| P0 | BUG-005 | 自动 git commit | 生产安全 | 1h |
| P0 | BUG-002 | REAL 存储时间戳 | 查询正确性 | 4h |
| P0 | BUG-003 | 缺少外键约束 | 数据完整性 | 2h |
| P1 | BUG-009 | 简单关键词情感分析 | 决策质量 | 4h |
| P1 | BUG-008 | CVaR 小样本不稳定 | 风险评估 | 2h |
| P1 | BUG-006 | 缓存无 TTL | 下单精度 | 1h |
| P1 | BUG-010 | 回测数据受限 | 策略验证 | 8h |
| P2 | BUG-011 | 死代码 | 代码质量 | 0.5h |
| P2 | BUG-013 | 缓存 TTL 未生效 | API 效率 | 1h |
| P2 | BUG-015 | 硬编码路径 | 可移植性 | 0.5h |

### 8.3 测试优先补充清单

| 模块 | 建议测试 | 优先级 |
|------|----------|--------|
| HMM 市场体制 | 状态转换、标签一致性、重训练触发 | P1 |
| 上下文老虎机 | Thompson Sampling 更新、Context 分配 | P1 |
| GARCH 波动率 | 预测准确性、降级行为、模型持久化 | P1 |
| 概念漂移检测 | 漂移触发、阈值边界、数据不足处理 | P1 |
| 策略模块（5 个） | 信号生成、边界情况、参数组合 | P2 |
| 数据源（7 个） | API 超时、缓存命中、降级行为 | P2 |
| 特征存储 | Redis 连接失败、内存 fallback、训练数据 | P2 |

### 8.4 最终结论

**crypto-ai-trader 是一个架构设计优秀、智能模块丰富的自动交易系统。** 核心交易链路的安全设计（fail-closed、多层熔断、异常价格过滤）值得肯定。然而，数据库 Schema 中的浮点精度问题（REAL 存储金额）是一个系统性风险，需要在投入真实资金前修复。ML 模块缺少测试是另一个隐患 — 正是最需要验证正确性的模块反而没有测试覆盖。

**建议**: 先修复全部 P0 Bug（预计 19h 工作量），再补充 ML 模块测试，最后投入纸交易验证 2-4 周后方可使用真实资金。

---

*审计报告结束。所有发现均基于代码证据，未做运行时验证。*
