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

