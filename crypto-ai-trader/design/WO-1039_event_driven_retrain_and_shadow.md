# WO-1039 设计文档：事件驱动重训 + 新参数 Shadow 验证

> 状态：**待评审，未实施**（工单第三段「只出设计不动手」）
> 作者：编程专家 · 2026-10-10 · 评审人：Leo / Travis

---

## 1. 背景：系统学习时间尺度现状（WO-1039 第一段盘点结论）

| 通道 | 当前节奏 | 触发点 | 文件:行号 |
|---|---|---|---|
| Contextual bandit | **逐笔**（平仓即时） | `record_outcome` 内部，用 entry 真实 context_json | trade_outcome_recorder.py:287 |
| Phase 2A rolling stats | **逐笔**（平仓即时） | `record_outcome` 尾部 `refresh_rolling_stats` | trade_outcome_recorder.py:322 |
| Phase 2B PF 双窗口策略开关 | **每轮 scan**（10min） | `_step_evolve_strategies` → `evaluate_pf_channel` | scan_orchestrator.py:398 |
| Factor weight learning | 周→**日**（本单已落地，带守门） | `daily_learning.py`（Mon-Sat 08:30 cron） | scripts/daily_learning.py |
| Concept drift 检测 | 周→**日**（本单已落地，report-only） | 同上 | scripts/daily_learning.py |
| Sector clustering | 周日 09:00 | weekly pipeline step 3 | scripts/learning_pipeline.py |
| Param optimization | 周日 09:00（grid+walk-forward，760s） | weekly pipeline step 4，**保持周级不动** | scripts/learning_pipeline.py |

**遗留缺口**（本设计要解决的）：
1. 学习节奏是**时间驱动**的（逐笔/10min/日/周），市场结构突变时最慢要等到次日 08:30 才被 drift 检测发现；
2. 任何学出的新参数（factor weights / param opt 结果）**直接生效于实盘**，没有离线验证缓冲带。

---

## 2. 目标与非目标

**目标**
- G1：drift 分数超阈值时**事件驱动**触发局部重训，不等下一档定时任务；
- G2：新参数先走 **shadow（paper）通道**积累对照业绩，达标后再切换实盘。

**非目标（硬边界）**
- 不改任何实盘下单逻辑（BUY/SL/TP/exit 决策与执行路径零改动）；
- 不把 param optimization（grid+walk-forward）日级化或事件化——单次 760s 计算重、样本不足时结果不稳（10/4 已见 "Too few trades: 0 < 5" 失败），保持周级；
- 不新增外部 API 调用（全部基于本地 DB/日志计算，限速零压力）。

---

## 3. Part A：事件驱动重训（drift → 局部重训）

### 3.1 触发器设计

```
daily_learning.py (日级)          每 10min cron-scan (未来可选挂点)
        │                                  │
        ▼                                  ▼
  detect_drift() ──── severity ∈ {warning, severe} ────
        │                                  (连续 2 次 ≥ threshold)
        ▼
  kv: drift_trigger = {ts, severity, kl, signals}
  outbox: learning:drift_trigger 事件（供 watcher/人审）
        │
        ▼ (de-bounced, cooldown 7d)
  局部重训 runner: retrain_drifted_components(severity, signals)
```

**关键决策：挂在日级 daily_learning 内，而不是 cron-scan 内。**
理由：drift 检测本身要算 KL 散度（30 笔滑窗直方图），每 10min 算一次纯浪费；
日级跑 + 事件触发重训，响应延迟上限 24h，对「结构漂移」这个尺度足够。
若评审认为需要更快，可降级为 cron-scan 每 6 轮（1h）做一次轻量 KL 检查——
留作开放问题 §6.1。

### 3.2 「局部重训」范围定义（不是全量重训）

按 drift 信号定向，只重训受影响组件：

| drift 信号 | 重训组件 | 动作 | 耗时预估 |
|---|---|---|---|
| corr/wr 相关性漂移 | factor weights | `compute_optimal_weights`（用近 30 笔 vs 全量 60/40 blend） | <5s |
| win_rate 断崖 | bandit 温度 | 提高 `update_from_outcome` 的探索权重（ε-greedy 温度参数） | <1s |
| severity=severe | 全部上述 + **只发告警**，不自动动 SL/TP 参数 | param opt 属于重资产，仍留周日全量 | — |

**SL/TP/仓位参数永远不被事件驱动自动重训**——这是防噪核心边界：
漂移期最危险的是同时改变风控参数，宁可慢。

### 3.3 防噪与安全（全部复用 WO-1039 已落地守门基建）

- **min-sample**：drift 分数的 KL 计算本身已有 MIN_TRADES=30；重训再加近 7d ≥5 笔（daily_learning 同款门槛）；
- **de-bounce**：连续 2 次检测确认（防单日抖动）；
- **cooldown**：同一组件 7 天内不重复事件重训（防震荡循环）；
- **ε 守门**：重训结果与现值差 < 0.5（权重点位）不应用；
- **审计**：`logs/learning_audit.jsonl` 追加 `trigger: "drift_event"` 行（与 daily 共用格式）；
- **回滚**：复用 `daily_learning.py --rollback-last`（audit 行带 old 快照，一键恢复）；
- **失败安全**：重训 runner 任何异常 → 保留旧参数 + outbox 告警，绝不半写。

---

## 4. Part B：新参数 Shadow 验证（paper 先行再切换）

### 4.1 通道复用：不新建 paper 引擎

系统已有 `bull_paper_engine.py / _b.py`（独立 paper 组合、同源行情、独立 DB 记账）。
Shadow 方案复用该基建，新增一个 **shadow 实例**：

```
                ┌─ 实盘通道：现行参数 kv['learned_factor_weights'] ── 不动 ─┐
行情/因子快照 ──┤                                                            ├→ 对照评估器
                └─ shadow 通道：候选参数 kv['shadow_factor_weights'] ────────┘
                     (bull_paper_engine 克隆实例，仅记账不下单)
```

- shadow 通道读 `shadow_factor_weights`（候选），实盘读 `learned_factor_weights`（现行）；
- 两条通道吃**同一份**每轮因子快照（复用 scan 落盘的因子分数，零额外 API）；
- shadow 交易用 paper 成交模型（现有 PaperTrader 假成交语义），只写 paper 表，**物理上不可能触实盘**（WO-1014 后 paper 表已与生产 trades 双写隔离）。

### 4.2 评估与切换准则

| 阶段 | 条件 | 动作 |
|---|---|---|
| 1. 积累 | shadow ≥ 20 笔平仓（预估 1-2 周自然产生） | 不评估，只记账 |
| 2. 对照 | 双通道同窗 PF / WR / 期望值 / MDD 四指标 | outbox 出对照报告（人可读） |
| 3. 建议切换 | shadow PF > 现行 PF × 1.1 **且** WR 差 ≥ +3pp **且** MDD 不恶化 | outbox 出 `learning:shadow_promote_candidate` 事件 |
| 4. 切换 | **人工确认**（Leo/Travis 在工单/群内回复）后才 kv 提升 shadow→learned | 审计 + 可回滚 |

**切换默认人工确认**，不做全自动 promote——参数影响实盘仓位权重，
成本（一次错误切换）远高于收益（省一次人工确认）。
若运行 4 周后对照稳定，可再议 auto-promote with cooldown（开放问题 §6.2）。

### 4.3 与 param optimization（周级）的关系

周日 param opt 产出的新参数组同样走 shadow 通道验证后才可提升——
即 shadow 是**所有**新参数进入实盘的唯一通道，与参数来源无关。
这也顺带修复了现状「param opt 结果直接写 kv 生效」的无缓冲问题。

---

## 5. 实施拆分建议（评审通过后另行立项，不在本单范围）

| 阶段 | 内容 | 预估 | 风险 |
|---|---|---|---|
| P1 | drift 事件触发 + 定向重训 runner（挂 daily_learning 内） | 0.5d | 低（纯 DB 计算+kv 写） |
| P2 | shadow 通道实例化 + 双通道记账 | 1d | 中（需 paper 实例隔离验证） |
| P3 | 对照评估器 + promote 事件 + 人工确认流程 | 0.5d | 低 |

依赖顺序 P1 → P2 → P3；P1 可独立上线（与 shadow 无耦合）。

---

## 6. 开放问题（待评审拍板）

1. **drift 检测频率**：日级（本设计默认）vs 1h 轻量档（KL 每 6 轮 scan 算一次）？后者响应快 24 倍，成本是多一次滑窗直方图计算（<50ms，可忽略）——真正顾虑是**重训触发太频繁导致参数抖动**，若 1h 档则 de-bounce 需加强为连续 3 次确认。
2. **auto-promote 时机**：4 周对照稳定后是否开放自动切换（带 7d cooldown + 回滚保险）？
3. **shadow 通道的行情源**：复用 scan 落盘因子快照（零 API，但因子快照粒度=10min）vs 独立订阅（多 API 消耗）——默认前者。

---

## 7. 附录：本设计引用的现状证据

- bandit 逐笔更新调用链：portfolio.py close_position → trade_outcome_recorder.record_outcome:287 → bandit.update_from_outcome（WO-1039 已修双写：原 close_position 直接段已删，单源单次）
- PF 通道触发：scan_orchestrator.py:398 `_step_evolve_strategies`（每 10min）
- drift 现状：KL_DIVERGENCE_THRESHOLD=0.10 / MIN_TRADES=30（concept_drift.py:23,27），最近输出 none 0/3 信号
- 周级 pipeline 耗时：weekly_learning_status.json 2026-10-04 —— weight 4.4s / drift 0.0s / sector 23.8s / param_opt 760s(failed: 0<5 trades)
