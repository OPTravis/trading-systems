# Strategy Review Log

## 2026-08-20 11:21 — Weekly Review #2 (post 8/14 architecture)

### Data collected
- no_signal_tracker: showed 35d no-signal (STALE — contradicted by 16 live trades 8/16-8/20)
- trade_outcomes (last 20): EDEN x3 (+7.2/+4.1/-1.8), ETH +16.4 (tp_breach), MUBARAK -2.1 (sl),
  SOL +1.9, WLD grid x3 (+1.3/+0.3/+0.2), ALICE -8.4 (sl), NIL x2 (-5.4/-6.0), PORTAL -5.0, XPL -1.2
- Market (10:30 scan): F&G 62 GREED, BTC $69,500 vs 200SMA +0.7% CONFIRMED_BULL (1.0x),
  20 opportunities, top TRX 79 / PLUME 77 / ZEC 76
- Equity $397.07 | realized PnL -$226.67 (272 trades) | overall WR 44.7% (17/38)
- Since 8/16: 16 trades, 6W, net +$1.49 (ETH carried)
- Backtest 8/17: 90d portfolio -17.3%, PF 0.58 (degradation alert ongoing, improved from -18.7)
- weekly_learning 8/16: param_optimization OOS 0 trades validation failed (not applied — safe)

### Findings
1. **bug#7 (FIXED)**: time-decay frozen at -10 (threshold pinned to floor 65) despite daily trading.
   Root cause: scan_phases write-back of stale in-memory tracker overwrote the executor's
   mid-scan reset (race); executor reset also sat inside event-bus try (silent skip on bus failure).
   Effect: entry bar 10 pts looser than design → low-score losers (NIL 72.5, XPL 74.2) admitted.
2. **Kelly deadlock**: HIGH-confidence Kelly=0 (WR 44.7% stale window). Escape hatch 5/5 used,
   blocked new entries at 10:30 (TRX 79 rejected). Escape entries to date: 4W/1L, net positive.
3. **Regime turn confirmed**: F&G 34→62 (FEAR→GREED), BTC reclaimed 200SMA (+0.7%).
   Regime mapping working as designed: trend x1.3 active, dca/rsi disabled, threshold 75.
4. Grid trading: FEAR-window entries (WLD x3) netted positive; grid gate correct (ADX/vol checked).
   GREED enables grid x1.2 (legacy design, unchanged).

### Changes (commit 7424ce6, pushed)
- scan_phases.py: race guard — re-read disk tracker before write-back, preserve executor reset
- trade_executor.py: tracker reset moved out of event-bus try; failures now warning-level
- kelly_sizer.py: EXPLORATION_CAP_30D 5 → 8 (worst-case added exposure ~$50)
- data/no_signal_tracker.json: corrected to ground truth (last_trade_date=2026-08-20, days=0)
- Tests: test_e2e_auto_trade + test_e2e_edge_cases 60/60 green

### Expected effect
- Threshold returns to regime value (GREED 75): TRX/PLUME/ZEC (77+) still pass;
  NIL/XPL-class (72-74) filtered out → higher per-trade quality
- 3 more escape entries available during CONFIRMED_BULL window; each 1-2% ($4-8),
  refreshes the stale win-rate window that gates full Kelly sizing

### Deferred (watch next week)
- walk-forward OOS 0 trades: expect natural recovery as regime warms; if still 0 next review,
  widen param_optimizer search space
- GREED grid x1.2 aggressiveness: review after observing post-fix trades
- RSI 1h→4h migration: still queued behind walk-forward validation


## 2026-09-17 11:47 — Weekly Review #6 (resident-host era; #3-#5 unlogged — ticket recurrence drops)

### Data collected
- no_signal_tracker: consecutive=0, last_trade=2026-09-17 (healthy, no decay active)
- trade_outcomes (rebuilt ledger): 6 OPEN rows / 5 symbols — ARB switch 67.9@0.1521 (9/16) +
  5 entries this morning: NEAR 18.68@2.558 (78.9), HEI 303.3@0.1388 (73.3), UNI 6.11@6.494 (69.4),
  DASH 0.47@56.36 (70.3), ARB dca 125.6@0.1693 (75.5); all 5 symbols carry TP1/TP2/SL OCO (kv verified)
- Exposure: $187.82 notional ≈ 44-47% of ~$425 equity (cash $234.63) — under 50% cap;
  max_open_positions=5 symbols → AT limit (new symbols blocked until something closes)
- Market: F&G 50 NEUTRAL (REAL — cross-checked alternative.me: 9/17=50, 9/16=51, 9/15=69;
  post-FOMC cooling), BTC $76,382 vs 200SMA $70,372 = +8.5% → trend gate CONFIRMED_BULL OPEN;
  threshold NEUTRAL 65 + SILENCE surge +2 = 67; all 5 entries scored 69.4-78.9 → passed 67
- Grid: OFF — CONFIRMED_BULL force-off per P0-A1 (correct); correlation guard fired at 11:00:
  ZECUSDT pre-trade BLOCK (0.74 corr vs DASH/NEAR, limit 0.7) — risk stack working
- weekly_backtest / weekly_learning status files: last success 8/17 / 8/16 — ORPHANED since
  9/15 resident migration (no ticket, no crontab entry; main.py has no weekly-* subcommand)

### Findings
1. **bug#41 (FIXED — hard-constraint violation)**: strategy_adaptor NEUTRAL/GREED/base entries
   hardcoded max_position_pct=15 / max_total_exposure_pct=70 / cash_reserve_pct=30 while
   risk_limits.yaml was tightened to 10/50/50 on 2026-08-03 ("Lowered from 15 → 10") — code
   drift silently raised the per-trade cap 50% above the SPOT hard constraint. BULLISH overlay
   could even push sizing to 18-20%. Evidence: 09-17 03:10:56 "Position cap: $118.48 → $59.84
   (max_position_pct=15%)"; NEAR filled $47.79 = 11.3% of equity (10% intent bypassed).
   Fix: caps now load from config/risk_limits.yaml (single source of truth, fallback 10/50/50)
   + unconditional end-clamp of position/exposure in _compute_regime_settings so no regime or
   overlay can exceed hard caps. Verified: NEUTRAL/BULL 12→10, GREED/BULL 13→10, FEAR/BEAR
   tightens normally (7), NEUTRAL/BEAR 5. Tests 60/60 green.
2. **bug#41b (FIXED — silent sensor death)**: DynamicGate logged "F&G unknown, defaulting to 50"
   every cycle since 9/15 — scan_gate.py + scan_phases.py read relative "data/cache.db", which in
   the /root clone is a 0-byte stale file; the real cache resolves to /root/trading-state/cache.db.
   Masked this week only because real F&G (50) equaled the fallback (50): a GREED/FEAR move would
   have run the gate on a false value and fng_prev surge-delta was dead. Both now import CACHE_DB
   from data_feed_base; verified live: "DynamicGate: F&G = 50 (from cache)".
3. **Weekly pipelines orphaned (RESTORED)**: after 9/15 常驻化 the crontab had only
   keepalive×2 + reside_scan; weekly-learning/weekly-backtest were ticket-era jobs that no longer
   fired (also explains review #3-#5 never reaching this log). Wired run_cron.sh case routing
   (weekly-learning → scripts/learning_pipeline.py, weekly-backtest → scripts/weekly_backtest.py)
   + crontab `0 9 * * 0` chain (flock /tmp/weekly_jobs.lock). First-run exposed two more fossils:
   learning_pipeline hardcoded .venv/bin/python (absent in clone) → sys.executable fallback;
   weekly_backtest bootstrapped sys.path from removed ~/crypto-ai-trader → Path(__file__) parent.
   Re-run launched 11:52; results append below when done.
4. Regime check (Step 3 rules): no 14d+ no-signal (0d — threshold floor cut N/A); grid gate
   correct; F&G 50 sits in NEUTRAL band → mapping correct, no regime-map change; BTC +8.5% from
   200SMA (>5% proximity rule → no filter-removal prep needed); walk-forward OOS 0-trades rule →
   pipeline was dead, so "4 consecutive weeks" never accrued — search-space widening DEFERRED
   until one fresh pipeline cycle is on record.

### Changes (commit 7100fa0, pushed; OPTravis identity restored in repo config)
- src/strategy_adaptor.py: _load_risk_caps() from risk_limits.yaml + hard-cap clamp at end of
  _compute_regime_settings; NEUTRAL/GREED/base now YAML-driven (10/50/50)
- scripts/scan_gate.py + src/scan_phases.py: read CACHE_DB (resolved) instead of stale
  relative data/cache.db
- run_cron.sh: weekly-learning / weekly-backtest routed to standalone scripts (proxy+env
  preamble reused); all other commands byte-identical behavior
- scripts/learning_pipeline.py: PYTHON falls back to sys.executable when .venv absent
- scripts/weekly_backtest.py: sys.path bootstrap from script location (layout migration fix)
- crontab (root): + `0 9 * * 0 flock -n /tmp/weekly_jobs.lock bash -c "run_cron.sh
  weekly-learning; run_cron.sh weekly-backtest"` (Sun 09:00 HKT)
- Tests: tests/test_e2e_auto_trade.py + test_e2e_edge_cases.py 60/60 green (4m20s)

### Expected effect
- Per-trade size cap drops 15% → 10% of available USDT in NEUTRAL/GREED (hard constraint
  enforced through every regime/overlay path); worst-case single position shrinks ~1/3
- DynamicGate regains true F&G awareness: correct scan cadence (GREED 1h / EXTREME_GREED 30m /
  FEAR 2h) and surge fng_prev delta detection restored
- Weekly learning/backtest resume: weight learning + drift + param_optimization run Sundays
  09:00; next review gets fresh walk-forward OOS data to judge the search-space question

### Deferred / watch (next review 9/24)
- walk-forward OOS trades: re-evaluate with 9/21 pipeline output; widen param_optimizer search
  space only if still 0 on fresh data
- NEAR position (11.3%, opened pre-fix): leave running (OCO set: SL 2.369 / TP 2.814/2.967);
  do NOT force-trim mid-flight
- Exposure 47% + 5/5 symbols: system will naturally pause new entries; watch regime flip to
  GREED (would re-enable trend ×1.3 with tighter 10% cap)
- EXTREME_GREED gate cadence 0.5h vs Leo's 1h ruling: left as coded (only accelerates in
  extreme greed); ask Leo if strict 1h desired
- 0-byte data/cache.db in repo: inert after fix, left in place (tracked file)
- HEI portfolio qty 200.1 vs trade_outcomes 303.3: tp/sl reservation accounting artifact;
  validator reconcile should settle it on next scan — verify next review


### 2026-09-17 12:16 — Pipeline first-run results (appended)

**weekly-learning** (12:02→12:15, 803s, Exit 0, all_ok=true):
- weight_learning / concept_drift / sector_clustering ✓（板块重分类：BTC/AVAX/BNB/CRV→MEME 簇等）
- param_optimization：管线 OK 但**验证拒绝**——OOS Sharpe -0.04<0.5、robustness 11%<33%、trades 0<5；候选参数（RSI 25/75、score_threshold 40）未应用。设计内保守拒绝，与 Deferred-watch#1 一致：OOS 数据不足，不动搜索空间，继续积累

**weekly-backtest** (Exit 0)：walk-forward 组合 PF 1.17（1175 trades, WR 76%, +4.08% / +$2,041.69）> 1.0 ✓
- SOL +7.79% PF1.32 / ETH +9.65% PF1.44 / AVAX +3.35% PF1.12 / LINK +2.79% PF1.11 / BNB -3.17% PF0.84（唯一负 PF，留意）
- 结论：组合级 OOS PF>1.0 达标，趋势过滤无需解除；BNB 单标的负 PF 记入下轮观察

**结论**：本周两项变更（10% 帽钳制 + 周管线复活）均已验证；参数自动优化被 OOS 守门正确拒绝（0 笔交易不足以支撑变更）——系统按 walk-forward 约束运行正常。

### 2026-09-24 11:25 — Weekly strategy review #4 (NO param/code change)

**Data**: no_signal_tracker(0d no-signal, filled today), trade_outcomes last 20, cron-scan live 10:51/11:10, weekly_learning+weekly_backtest(9/20), portfolio/kv, BTC 200SMA computed live.

**Step 2/3 rule evaluations**:
1. Time-decay: consecutive_no_signal_days=0 (fresh 9/24 fills) -> floor cut 65->60 NOT triggered; floor stays 65.
2. Grid: OFF (disabled by HMM BULL_TREND), grid_state empty, zero grid loss -> nothing to tighten; FEAR+ADX<25+LOW/NORMAL spec unchanged.
3. F&G 50->71 (GREED) + CONFIRMED_BULL + HMM BULL_TREND p=0.994 -> regime-mapping re-check per rule 3c: GREED threshold 65 (eff 67), trend ON x1.56 (drawdown 0.7 -> eff 0.56), rsi_reversion OFF in GREED, bollinger ON, DCA OFF — all per design; mapping correct, no change.
4. BTC 84,256 vs 200SMA 70,886 = +18.9% (>>5% proximity) -> no trend-filter-removal prep (rule 3d).

**Findings**:
- [DATA-INTEGRITY -> dispatched to coding agent as reconciler-batch addendum] trade_outcomes has ~21 stale open rows vs 1 actual holding (ZAMA 223). Sells exist in trades table but reconcile never closes outcome rows (evidence: LTC #49 0.178 = SELL@64.4 10:51; NEAR #46 4.9 = SELL@4.666 04:11; UNI #43 partial 0.77/2.34; MARSCOIN #48 sold 10:01). Pollutes Kelly (win_rate 35% MEDIUM), strategy_rolling_stats, weekly weight_learning (all 10 factor correlations negative on n=14 — likely artifact). Fix owned by coding agent; backfill from trades-table SELL fills.
- [WATCH - boundary] walk-forward alpha (trend OFF) portfolio PF 1.17 -> 1.00 in one week (SOL 1.01 / ETH 1.28 / AVAX 0.94 / BNB 0.74 / LINK 1.02) — exactly AT the OOS PF>1.0 hard line; BNB negative 2nd week (0.84->0.74). No new-strategy adoption while at boundary. Re-judge 10/1.
- [DEFERRED x2, rationale updated] param_optimizer rejected again 9/20 (trades 0<5). Mechanical trigger "still 0 -> widen search space" FIRED, but OVERRIDDEN: alpha edge decayed to 1.00 same week — loosening optimizer while edge sits at boundary is pro-cyclical. Re-evaluate only after (a) outcomes data cleaned, (b) portfolio alpha PF back >1.05.
- [WATCH] switch/rotation churn on low-caps: MARSCOIN 0.1384 in -> 0.1288 out same morning (-7% ~4h); DASH switch exit -9.6% exceeds SL 5-7% band (switch sells not SL-bound by design, but record it); rolling_stats switch pf 3.36 untrustworthy until outcomes fixed. Revisit $6-min-notional fee drag after data clean.
- [minor] tp_breach_state:ENAUSDT stale (position exited 9/22) -> include in dispatch cleanup.
- Ops: proxy failover cn02 OK 11:10; DynamicGate GREED 1h respected (Leo 8/28 ruling; EXTREME_GREED 0.5h still dormant, unfired); daily-loss breaker 9/23 tier2 released 9/24; stepwise drawdown mild 3.5% x0.7 active.

**Decision**: NO parameter change / NO code change / NO new strategy. Commit: none; pytest n/a (no diff). Leo notify: not required (no major change per rule 5; fix runs under 9/22 standing order "发现就修、立刻派单").

### 2026-10-01 11:15 — Weekly strategy review #5 (NO param/code change)

**Data**: no_signal_tracker(0d no-signal), trade_outcomes last 20 (15 closed: 7W/8L, avg net -0.36%), live cron-scan 11:14 (NO_OPPORTUNITIES, threshold 80/eff 82), weekly_learning+weekly_backtest (9/27), portfolio 3 holdings (ICP/PENGU/NEAR, gross $16.9, cash $398.4, equity ~$415, exposure 4.1%), BTC 200SMA computed live.

**Step 2/3 rule evaluations**:
1. Time-decay: consecutive_no_signal_days=0 (fills 9/30 ICP + 10/1 NEAR) -> floor cut 65->60 NOT triggered; floor stays 65.
2. Grid: OFF (disabled by HMM HIGH_VOL), grid_state empty, zero grid loss -> nothing to tighten; FEAR+ADX<25+LOW/NORMAL spec unchanged.
3. F&G 71->74 (GREED) regime-mapping re-check per rule 3c: GREED threshold 80 (eff 82), bollinger ON size=100% SL6% hold48h, dca OFF (greed), rsi OFF (greed), trend/grid/vwap OFF (HMM HIGH_VOL, data-driven), BTC trend gate CONFIRMED_BULL 1.0x, bull_regime MILD_BULL confirm 0/2 — all per design; mapping correct, no change.
4. BTC $83,508 vs 200SMA $71,353 = +17.0% (>>5% proximity) -> no trend-filter-removal prep (rule 3d).

**Step 2 strategy sanity**:
- bollinger: sole enabled strategy AND largest cumulative loser (17 trades, avg -2.03%, sum -$10.2; within last-20 window 2W/5L, all exits at designed 5-7% SL — bounded). Under walk-forward hard constraint (alpha PF at 1.00 line) no parameter/architecture change permitted; re-review after HMM leaves HIGH_VOL and multi-strategy resumes.
- switch: 29 trades, 51.7% WR, avg +1.79%, +$11.7 — healthy, unchanged.
- HMM HIGH_VOL persists -> single-strategy mode is data-driven, by design; no override.

**Findings / watch updates**:
- [9/24 watch resolved-decision] walk-forward alpha re-judge (due 10/1): 9/27 bare alpha portfolio PF 1.00 (+0.05%, 1250 trades; SOL 1.01/ETH 1.28/AVAX 0.94/BNB 0.74/LINK 1.02). Both unlock conditions (a) outcomes cleaned (b) PF>1.05 still unmet -> param_optimizer search-space widening stays DEFERRED. param_optimization 9/27 rejected again (0 trades < 5) — gate working as designed.
- [WATCH x3wk] BNB backtest negative PF 3rd straight week (0.84/0.74/0.74). Keep in pool for walk-forward baseline continuity; do not cherry-pick mid-decay. Re-judge if 4th negative week.
- [DATA-INTEGRITY, carried] trade_outcomes 19 open rows vs 3 actual holdings — reconciliation improving (ledger shadow clean 20+ consecutive rounds, switch/reconciled rows closing normally) but stale-row backfill not fully landed. Structural fix in flight under WO-1003 exit-parity / WO-1005 audit funnel; re-dispatch only if still >10 stale rows at 10/8 review.
- [Fix progress verified] WO-1006 landed 10/1 08:56 (clamp SL price to placeable band, kills TP-only rescue -2010 loop — covers ensure_tp_sl fee-shortfall fix). WO-1001..1005 batch-landed 9/30 (REST+TG control plane, config SSOT, prod-DB write guard, hyperopt TPE w/ walk-forward dual gate, DRYRUN isolation). Reconciler live-verified in 11:14 scan. Of the four in-flight fixes, 3 have landed evidence; dca fallback direction gate has no direct commit yet but dca is OFF (greed) — not blocking.
- Weekly PnL since 9/24: 31 closed, net ≈ -$1.47 (~-0.36% of equity) — mild, no systemic degradation. DynamicGate GREED 1h cadence respected (Leo 8/28 ruling); 10:40 & 11:00 SKIPs were correct gate behavior.

**Decision**: NO parameter change / NO code change / NO new strategy. Commit: none; pytest n/a (no diff). Leo notify: not required (no major change per rule 5).

## CVaR shadow 期满拍板（2026-10-02 10:30，Leo 拍板）
- **决定：阶段二不激活，shadow 延长（方案 A）**——7 天对拍：257 样本、88.3% 缩仓命中（scale 0.8）、medium 227/low 30 无 high、激活则仓位 -17.7%
- **复评触发条件（任一命中即提请 Leo 复评）**：①cvar_95 破 -10 ②risk_level 出现 high ③策略池恢复（evolver 重新启用策略，当前 bollinger 被 PF<1 禁用中）
- 复评节点：下次策略周检讨（约 10/8）自动带入本决定；周检讨模板须核对上述三个触发条件
- 依据：样本全牛市段缺熊市验证；bollinger 禁用期无新仓 overlay 无用武之地；greed 期全程压仓拖累收益
