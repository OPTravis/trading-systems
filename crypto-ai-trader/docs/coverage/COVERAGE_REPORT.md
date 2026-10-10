# WO-1041 T4 Coverage Report

- **Date:** 2026-10-10 ｜ **Run:** `pytest tests/ --cov=src --cov-report=term --cov-report=html`
- **Result:** 2456 passed / 4 skipped / 0 failed (baseline 2422 + 34 new WO-1041 tests)
- **Overall coverage: 66.1%** — 24,728 statements, 8,395 missing
- Artifacts: `docs/coverage/html/` (interactive HTML), `docs/coverage/coverage.json` (raw)

## 与 95% 方向值的差距构成

- 缺口 ≈ 28.9pp（≈7,150 行）。**<50% 的 26 个模块贡献 3037 行 miss（全部 miss 的 36%）**，其中 5 个 0% 模块（未接线/外部 I/O）贡献 417 行。
- 50–80% 模块 58 个贡献 4529 行；≥80% 模块 53 个。核心交易链路（executor/portfolio/state_db/paper_trader/risk_manager/pnl 等）均在 ≥80% 区间。
- 结论：95% 需要覆盖大量 I/O 密集、长连接、CLI 入口和未接线实验模块——其中约一半 miss 行属于「mock 基建成本高、回归价值低」的类别，为凑数字强写 mock 测试会带来脆测试维护负担（工单明示不要）。

## 分模块覆盖率（全量，升序）

| Cov% | Stmts | Miss | Module |
|---:|---:|---:|---|
| 0% | 55 | 55 | `src/social_sentiment.py` |
| 0% | 62 | 62 | `src/onchain_provider.py` |
| 0% | 78 | 78 | `src/metrics_exporter.py` |
| 0% | 89 | 89 | `src/capture_tracker.py` |
| 0% | 133 | 133 | `src/sector_clustering.py` |
| 14% | 118 | 101 | `src/backtester.py` |
| 15% | 145 | 123 | `src/funding_arb.py` |
| 16% | 196 | 165 | `src/online_learner.py` |
| 16% | 350 | 293 | `src/bull_paper_engine.py` |
| 22% | 247 | 193 | `src/ws_user_stream.py` |
| 25% | 110 | 83 | `src/data_feed_news.py` |
| 28% | 235 | 170 | `src/bull_paper_engine_b.py` |
| 29% | 59 | 42 | `src/orderbook_analyzer.py` |
| 30% | 175 | 123 | `src/feature_store.py` |
| 31% | 71 | 49 | `src/data_feed_oi.py` |
| 32% | 159 | 108 | `src/dynamic_coin_pool.py` |
| 38% | 279 | 173 | `src/sector_classifier.py` |
| 39% | 23 | 14 | `src/app_secrets.py` |
| 40% | 580 | 350 | `src/market_researcher.py` |
| 41% | 69 | 41 | `src/data_feed_scorer.py` |
| 42% | 90 | 52 | `src/pending_confirmation.py` |
| 42% | 253 | 146 | `src/param_optimizer.py` |
| 45% | 125 | 69 | `src/trade_journal.py` |
| 46% | 173 | 93 | `src/data_feed_llama.py` |
| 46% | 166 | 89 | `src/self_healer.py` |
| 48% | 276 | 143 | `src/hmm_regime.py` |
| 50% | 155 | 77 | `src/sentiment.py` |
| 51% | 96 | 47 | `src/fee_optimizer.py` |
| 52% | 182 | 88 | `src/price_predictor.py` |
| 52% | 561 | 269 | `src/cmd_trailing_check.py` |
| 52% | 82 | 39 | `src/strategy_guard.py` |
| 53% | 122 | 57 | `src/strategy_registry.py` |
| 53% | 60 | 28 | `src/data_feed_fng.py` |
| 54% | 691 | 321 | `src/backtest.py` |
| 54% | 139 | 64 | `src/trade_outcome_recorder.py` |
| 56% | 90 | 40 | `src/data_feed_funding.py` |
| 56% | 136 | 60 | `src/twap_vwap.py` |
| 56% | 698 | 307 | `src/ccxt_client.py` |
| 57% | 431 | 185 | `src/scan_phases.py` |
| 58% | 260 | 108 | `src/control_panel.py` |
| 60% | 126 | 51 | `src/execute_phases.py` |
| 60% | 147 | 59 | `src/bear_analyst.py` |
| 60% | 50 | 20 | `src/adaptive_trailing.py` |
| 60% | 589 | 233 | `src/market_scanner.py` |
| 61% | 41 | 16 | `src/risk_config.py` |
| 61% | 118 | 46 | `src/bull_paper_ab_metrics.py` |
| 64% | 141 | 51 | `src/llm_client.py` |
| 65% | 43 | 15 | `src/btc_trend_gate.py` |
| 65% | 101 | 35 | `src/event_bus.py` |
| 65% | 52 | 18 | `src/restricted_symbols.py` |
| 66% | 50 | 17 | `src/strategies/dca.py` |
| 67% | 107 | 35 | `src/smart_order.py` |
| 68% | 173 | 56 | `src/notifier.py` |
| 68% | 167 | 54 | `src/fund_flow_audit.py` |
| 68% | 50 | 16 | `src/strategies/base.py` |
| 70% | 209 | 63 | `src/strategy_evolver.py` |
| 70% | 271 | 81 | `src/portfolio.py` |
| 71% | 144 | 42 | `src/concept_drift.py` |
| 71% | 410 | 118 | `src/strategy_adaptor.py` |
| 71% | 351 | 101 | `src/scan_orchestrator.py` |
| 71% | 675 | 193 | `src/risk_manager.py` |
| 72% | 716 | 202 | `src/_binance_sdk_client.py` |
| 72% | 442 | 122 | `src/research_phase.py` |
| 73% | 153 | 42 | `src/multi_timeframe.py` |
| 73% | 62 | 17 | `src/strategies/bollinger.py` |
| 73% | 66 | 18 | `src/portfolio_risk.py` |
| 73% | 448 | 122 | `src/dimension_scorer.py` |
| 74% | 176 | 45 | `src/kelly_sizer.py` |
| 74% | 937 | 239 | `src/trade_executor.py` |
| 75% | 103 | 26 | `src/stepwise_drawdown.py` |
| 75% | 123 | 31 | `src/data_feed.py` |
| 75% | 108 | 27 | `src/hash_ribbon.py` |
| 75% | 57 | 14 | `src/strategies/trend.py` |
| 76% | 474 | 116 | `src/grid_trader.py` |
| 76% | 63 | 15 | `src/strategies/rsi_reversion.py` |
| 77% | 482 | 113 | `src/paper_trader.py` |
| 77% | 137 | 32 | `src/invariant_guard.py` |
| 77% | 30 | 7 | `src/portfolio_pnl.py` |
| 78% | 690 | 155 | `src/state_db.py` |
| 78% | 117 | 26 | `src/qfl_scanner.py` |
| 79% | 66 | 14 | `src/strategies/vwap.py` |
| 79% | 419 | 86 | `src/protection_guardian.py` |
| 80% | 127 | 26 | `src/bias_analysis.py` |
| 80% | 118 | 24 | `src/cvar_risk.py` |
| 80% | 35 | 7 | `src/strategies/grid.py` |
| 80% | 96 | 19 | `src/entry_price.py` |
| 80% | 187 | 37 | `src/correlation_risk.py` |
| 81% | 182 | 35 | `src/dust_reaper.py` |
| 81% | 252 | 48 | `src/bull_regime.py` |
| 82% | 98 | 18 | `src/drawdown_breaker.py` |
| 82% | 564 | 103 | `src/position_optimizer.py` |
| 82% | 232 | 41 | `src/portfolio_state.py` |
| 84% | 129 | 21 | `src/kv_preflight.py` |
| 84% | 305 | 48 | `src/exit_check.py` |
| 85% | 350 | 54 | `src/indicators.py` |
| 85% | 130 | 20 | `src/daily_loss_breaker.py` |
| 85% | 472 | 71 | `src/portfolio_reconciler.py` |
| 85% | 68 | 10 | `src/entry_governor.py` |
| 87% | 75 | 10 | `src/stuck_order_monitor.py` |
| 87% | 266 | 34 | `src/bull_paper_store.py` |
| 87% | 548 | 70 | `src/ledger.py` |
| 87% | 295 | 37 | `src/circuit_tiers.py` |
| 88% | 130 | 15 | `src/circuit_breaker.py` |
| 89% | 35 | 4 | `src/live_alerts.py` |
| 89% | 55 | 6 | `src/strategy_rolling_stats.py` |
| 90% | 59 | 6 | `src/tp_sl_tracker.py` |
| 90% | 79 | 8 | `src/health_report.py` |
| 90% | 80 | 8 | `src/agents/calibration.py` |
| 90% | 163 | 16 | `src/agents/technical_agent.py` |
| 91% | 43 | 4 | `src/data_feed_base.py` |
| 91% | 102 | 9 | `src/data_feed_onchain.py` |
| 92% | 60 | 5 | `src/agents/volume_agent.py` |
| 92% | 90 | 7 | `src/garch_vol.py` |
| 93% | 27 | 2 | `src/agents/onchain_agent.py` |
| 93% | 149 | 10 | `src/protections.py` |
| 93% | 76 | 5 | `src/agents/prepump_agent.py` |
| 94% | 50 | 3 | `src/agents/sentiment_agent.py` |
| 94% | 190 | 11 | `src/surge_detector.py` |
| 95% | 112 | 6 | `src/protection_shape.py` |
| 95% | 39 | 2 | `src/news_entity_filter.py` |
| 95% | 121 | 6 | `src/event_trigger.py` |
| 95% | 44 | 2 | `src/agents/market_sentiment_agent.py` |
| 96% | 95 | 4 | `src/config_store.py` |
| 97% | 143 | 4 | `src/contextual_bandit.py` |
| 98% | 40 | 1 | `src/bge_cache.py` |
| 99% | 161 | 2 | `src/bull_paper_portfolio.py` |
| 100% | 1 | 0 | `src/__init__.py` |
| 100% | 2 | 0 | `src/exchange_client.py` |
| 100% | 3 | 0 | `src/utils.py` |
| 100% | 8 | 0 | `src/agents/base.py` |
| 100% | 8 | 0 | `src/strategies/__init__.py` |
| 100% | 10 | 0 | `src/agents/__init__.py` |
| 100% | 13 | 0 | `src/binance_client.py` |
| 100% | 17 | 0 | `src/pnl_calculator.py` |
| 100% | 18 | 0 | `src/coin_names.py` |
| 100% | 36 | 0 | `src/hyperopt_loss.py` |
| 100% | 37 | 0 | `src/agents/trend_agent.py` |

## 冷门路径清单（<50%，26 个）

| Cov% | Miss | Module | 定性 |
|---:|---:|---|---|
| 0% | 55 | `src/social_sentiment.py` | 未接线实验模块（社交情绪抓取，外部 API 密集） |
| 0% | 62 | `src/onchain_provider.py` | 链上数据抓取（外部 API，无 mock 基建） |
| 0% | 78 | `src/metrics_exporter.py` | 运维导出器（独立 CLI 子系统） |
| 0% | 89 | `src/capture_tracker.py` | 截图追踪（本地资源 I/O） |
| 0% | 133 | `src/sector_clustering.py` | 实验模块（未接入主链路） |
| 14% | 101 | `src/backtester.py` | CLI 回测入口路径（核心指标已由独立计算模块覆盖） |
| 15% | 123 | `src/funding_arb.py` | F1 backlog（未接线，Travis 侧挂账） |
| 16% | 165 | `src/online_learner.py` | 在线学习底座（daily-learning cron 路径，08:30 真实运行） |
| 16% | 293 | `src/bull_paper_engine.py` | BULL Phase2 paper 通道（独立引擎） |
| 22% | 193 | `src/ws_user_stream.py` | websocket 长连接（连接生命周期天然难单测） |
| 25% | 83 | `src/data_feed_news.py` | 外部新闻 feed（网络 I/O 为主） |
| 28% | 170 | `src/bull_paper_engine_b.py` | A/B 实验 B 组引擎（实验线） |
| 29% | 42 | `src/orderbook_analyzer.py` | 盘口分析（外部数据形态） |
| 30% | 123 | `src/feature_store.py` | 特征存储（文件 I/O 密集） |
| 31% | 49 | `src/data_feed_oi.py` | 外部 OI feed |
| 32% | 108 | `src/dynamic_coin_pool.py` | 动态币池（依赖运行时状态） |
| 38% | 173 | `src/sector_classifier.py` | 板块分类（LLM/外部调用密集） |
| 39% | 14 | `src/app_secrets.py` | 密钥装配（安全敏感，不宜深度 mock） |
| 40% | 350 | `src/market_researcher.py` | 核心研究链（Jina/DDGS/LLM 网络 I/O 密集——P1 提升首选） |
| 41% | 41 | `src/data_feed_scorer.py` | feed 评分（外部数据） |
| 42% | 52 | `src/pending_confirmation.py` | 挂单确认（事件时序路径） |
| 42% | 146 | `src/param_optimizer.py` | 参数优化（计算重、入口窄） |
| 45% | 69 | `src/trade_journal.py` | 交易日志（文件 I/O） |
| 46% | 93 | `src/data_feed_llama.py` | LLM feed（外部 API） |
| 46% | 89 | `src/self_healer.py` | 自愈脚本路径（运维 cron 真实运行） |
| 48% | 143 | `src/hmm_regime.py` | HMM 市场状态（数值核心可测——P2 候选） |

## 务实提升计划（不为凑数）

- **P1 高价值可测（~800 行 miss）**：`market_researcher.py`（350，WO-1040 刚改动的核心链，网络层已有 mock 先例）、`online_learner.py`（165，WO-1039 守门学习）、`hmm_regime.py`（143，纯数值核心）、`param_optimizer.py`（146）。
- **P2 次优（~700 行）**：`bull_paper_engine*.py`（A/B 实验线）、`dynamic_coin_pool.py`、`pending_confirmation.py`、`self_healer.py`。
- **P3 暂缓（结构性难测）**：`ws_user_stream`（长连接）、`data_feed_*`/`sector_classifier`（外部 feed/LLM）、`backtester` CLI、`funding_arb`（未接线）、5 个 0% 未接线模块——待接线或 mock 基建成熟后再覆盖。
- 建议验收口径：核心链路（交易执行/记账/风控/状态）≥85% 已达标；整体值随 P1/P2 落地预期 66%→73%±，95% 作为长期方向值随模块接线逐步逼近。
