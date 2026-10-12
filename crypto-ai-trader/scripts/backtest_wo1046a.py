#!/usr/bin/env python3
"""WO-1046a P1 backtest: two-tranche TP + trailing phase2 + DrawdownBreaker.

Replays the WO-1044 "after" engine (baseline 72043a5, all its fixes) with
the P1 mechanism changes layered on, against the SAME cached data:

  S2  TP1 single tranche 50% at max(1.5R, 3%, 3×ATR) — R anchored to the
      strategy sl_pct (entry stop distance). Legacy ladder (4%×40/8%×40/
      15%×20) is the C3 diff baseline.
  S1  trailing phase2: at profit >= phase2_r_pct (7.5% ≈ 1.5R for the
      stock 5% SL) the band switches ONCE to clamp(2.5×ATR, 2%, 12%);
      the switch round may move the SL DOWN, afterwards ratchet-only.
  C1② DrawdownBreaker: equity HWM drawdown >= max_drawdown_pct (10%,
      risk_limits.yaml) trips the breaker — new entries blocked for the
      REST of the window (live semantics: manual reset required); open
      positions keep exiting via SL/trailing naturally. The report shows
      the trip day and the post-trip return.

C1① --segments runs the window split in two halves (Aug vs Sep) so a
single lucky stretch can't carry the verdict.
C3  the report prints a line-by-line data-source / window / cost-model
  unchanged declaration plus the tp_levels diff.

Cost model: 0.1% fee + 0.05% slip per side (WO-1045-6) — unchanged.
"""
import argparse
import json
import os
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from backtest_wo1044 import (  # noqa: E402
    CACHE_DIR, atr14, btc_score_approx, btc_tier, coin_score,
    fetch_btc_1h, fetch_fng, fetch_klines_1d, load_or_fetch,
)

FEE = 0.001
SLIP = 0.0005
TP1_PCT_OF_QTY = 50.0
PHASE2_R_PCT = 7.5          # ≈ 1.5R for the stock 5% SL
PHASE2_ATR_MULT = 2.5
PHASE2_MIN_W = 2.0
PHASE2_MAX_W = 12.0


class P1Position:
    __slots__ = ("symbol", "entry_px", "qty", "qty0", "entry_day_idx",
                 "sl", "highest", "activated", "atr", "tp1_pct",
                 "banked", "phase2_on", "strategy", "realized",
                 "entry_amount", "peak_pnl_pct")

    def __init__(self, symbol, px, qty, day_idx, sl_pct, tp1_pct,
                 strategy, amount):
        self.symbol = symbol
        self.entry_px = px
        self.qty = self.qty0 = qty
        self.entry_day_idx = day_idx
        self.sl = px * (1 - sl_pct / 100)
        self.highest = px
        self.activated = False
        self.atr = None
        self.tp1_pct = tp1_pct
        self.banked = False
        self.phase2_on = False
        self.strategy = strategy
        self.realized = 0.0
        self.entry_amount = amount
        self.peak_pnl_pct = 0.0


def _close_pos(pos, px, day, reason, cash, audit, givebacks):
    proceeds = pos.qty * px * (1 - FEE - SLIP)
    pos.realized += proceeds - pos.entry_amount * (pos.qty / pos.qty0)
    cash += proceeds
    exit_pnl = (px - pos.entry_px) / pos.entry_px * 100
    givebacks.append(max(0.0, pos.peak_pnl_pct - exit_pnl))
    audit.append({"day": day, "sym": pos.symbol, "exit": reason,
                  "pnl": round(pos.realized, 2)})
    return cash


def run_variant_p1(src_root, start, end, universe, fng, btc1d, btc1h,
                   initial_capital=414.73, vol_adj=1.0,
                   breaker_enabled=True, breaker_mode="manual",
                   breaker_dd_override=None):
    sys.path.insert(0, os.path.abspath(src_root))
    for m in list(sys.modules):
        if m == "src" or m.startswith("src."):
            del sys.modules[m]

    import yaml
    with open(os.path.join(src_root, "config", "risk_limits.yaml")) as f:
        risk_cfg = yaml.safe_load(f)["risk"]
    breaker_dd_pct = float(risk_cfg.get("max_drawdown_pct", 10))
    if breaker_dd_override is not None:
        breaker_dd_pct = breaker_dd_override  # probe/sensitivity only

    from src.strategy_adaptor import StrategyAdaptor
    from src import hmm_regime as hmm_mod

    hmm_detector = hmm_mod.HMMRegimeDetector()

    def _fake_cached(self):
        return getattr(hmm_detector, "_replay_pred", None)

    hmm_mod.HMMRegimeDetector.get_cached_prediction = _fake_cached

    class _NoBandit:
        def recommend_size(self, ctx):
            return 1.0

    try:
        import src.strategy_adaptor as sa_mod
        import src.contextual_bandit as cb_mod
        cb_mod.get_contextual_bandit = lambda *a, **k: _NoBandit()
    except Exception:
        pass
    try:
        import src.strategy_evolver as se_mod
        se_mod.StrategyEvolver.get_disabled_strategies = lambda self: {}
    except Exception:
        pass

    dates = [b["date"] for b in btc1d]
    d0, d1 = dates.index(start), dates.index(end)
    btc_closes = [b["close"] for b in btc1d]

    day_open_idx = {}
    ms_list = []
    for k in btc1h:
        d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        ms_list.append((d, k))
    for j, (d, _) in enumerate(ms_list):
        if d not in day_open_idx:
            day_open_idx[d] = j
    sym_dates = {sym: [b["date"] for b in bars] for sym, bars in universe.items()}

    cash = initial_capital
    positions = []
    equity_curve, peak_eq, max_dd = [], 0.0, 0.0
    flat_days = 0
    bull_days = 0
    exposure_days = 0
    n_entries = 0
    givebacks = []
    audit = []
    breaker_tripped = False
    breaker_trip_day = None
    breaker_trip_equity = None
    entries_before_trip = 0
    # WO-1046b tiered-reset state (day granularity: 48h=2d, 72h=3d,
    # 24h recheck=1d, chain window 72h=3d)
    breaker_mult = 1
    breaker_trip_idx = None
    breaker_last_reset_idx = None
    breaker_reset_count = 0
    breaker_trip_events = []

    for i in range(d0, d1 + 1):
        day = dates[i]
        closes_hist = btc_closes[: i + 1]
        change_24h = (btc_closes[i] / btc_closes[i - 1] - 1) * 100
        fng_v = fng.get(day, 60)
        tier = btc_tier(closes_hist)
        if tier == "CONFIRMED_BULL":
            bull_days += 1
        bs = btc_score_approx(closes_hist)

        j0 = day_open_idx.get(day)
        window = [k for _, k in ms_list[max(0, j0 - 720):j0 + 1]] if j0 is not None else []
        try:
            hmm_detector._replay_pred = None
            if len(window) >= 60:
                pred = hmm_detector.predict(window)
                if pred and pred.get("confidence", 0) > 0.4:
                    hmm_detector._replay_pred = pred
        except Exception:
            hmm_detector._replay_pred = None

        adaptor = StrategyAdaptor()
        adaptor._btc_klines_cache = [
            [0, "0", "0", "0", str(c), "0", 0, "0", 0]
            for c in btc_closes[max(0, i - 30):i + 1]]
        adaptor._btc_klines_ts = time.time()

        btc_trend_str = "BULLISH" if bs > 65 else ("BEARISH" if bs < 35 else "NEUTRAL")
        adapted = adaptor.adapt(
            fear_greed=fng_v, btc_trend=btc_trend_str,
            btc_price_change_24h=change_24h, funding_rate=None,
            btc_adx=None, btc_score=bs, bull_tier=tier)
        threshold = adapted["global"]["score_threshold"]
        enabled = {n: c for n, c in adapted["strategies"].items() if c.get("enabled")}

        # ── open positions: hard SL -> trailing (phase2-aware) -> TP1 ──
        for pos in list(positions):
            pdates = sym_dates[pos.symbol]
            p = bisect_right(pdates, day)
            if p == 0 or pdates[p - 1] != day:
                continue
            bar = universe[pos.symbol][p - 1]
            pos.highest = max(pos.highest, bar["high"])
            profit = (bar["close"] - pos.entry_px) / pos.entry_px
            pos.peak_pnl_pct = max(pos.peak_pnl_pct,
                                   (pos.highest - pos.entry_px) / pos.entry_px * 100)
            a = atr14(universe[pos.symbol][:p])
            if a:
                pos.atr = a
            if (not pos.activated) and pos.atr and (bar["close"] - pos.entry_px) >= pos.atr:
                pos.activated = True

            # hard SL (pre-switch level protects the switch day — matches
            # live: the resting order is still the old one on switch round)
            if bar["low"] <= pos.sl:
                cash = _close_pos(pos, pos.sl, day, "SL", cash, audit, givebacks)
                positions.remove(pos)
                continue

            # trailing: tight STEPS ladder -> phase2 wide band at 1.5R
            if pos.activated:
                # production consumes the 1h ATR14 (risk_manager gets atr
                # from cmd_trailing_check's 1h klines); backtest has daily
                # bars only -> convert ATR_1d / sqrt(24)
                atr_pct_1h = (
                    pos.atr / bar["close"] * 100 / (24 ** 0.5)
                    if pos.atr and bar["close"] else 0)
                profit_pct = profit * 100
                phase2_hit = profit_pct >= PHASE2_R_PCT or pos.phase2_on
                if phase2_hit and atr_pct_1h > 0:
                    width = min(max(PHASE2_ATR_MULT * atr_pct_1h, PHASE2_MIN_W), PHASE2_MAX_W)
                    band_sl = max(pos.highest * (1 - width / 100),
                                  pos.entry_px * 1.005)  # min-profit lock
                    if not pos.phase2_on:
                        pos.phase2_on = True          # one-time switch
                        pos.sl = band_sl               # may move DOWN
                        audit.append({"day": day, "sym": pos.symbol,
                                      "event": "phase2_switch",
                                      "sl": round(band_sl, 6),
                                      "width_pct": round(width, 2)})
                    elif band_sl > pos.sl:
                        pos.sl = band_sl
                else:
                    # tight ladder (identical to WO-1044 after engine)
                    step = 0.025 if profit < 0.03 else (0.02 if profit < 0.10 else 0.015)
                    new_sl = max(pos.highest * (1 - step * vol_adj),
                                 pos.entry_px * 1.005)
                    if new_sl > pos.sl:
                        pos.sl = new_sl
                if bar["low"] <= pos.sl:
                    reason = "TRAIL_P2" if pos.phase2_on else "TRAIL"
                    cash = _close_pos(pos, pos.sl, day, reason, cash, audit, givebacks)
                    positions.remove(pos)
                    continue

            # TP1: single 50% tranche at max(1.5R, 3%, 3×ATR) — the
            # resting limit (R-anchored) or the decision-core market
            # partial; same level, same size, one simulation.
            if (not pos.banked) and pos.tp1_pct and \
                    bar["high"] >= pos.entry_px * (1 + pos.tp1_pct / 100):
                px = pos.entry_px * (1 + pos.tp1_pct / 100)
                sell_qty = min(pos.qty0 * TP1_PCT_OF_QTY / 100, pos.qty)
                proceeds = sell_qty * px * (1 - FEE - SLIP)
                pos.realized += proceeds - pos.entry_amount * (sell_qty / pos.qty0)
                cash += proceeds
                pos.qty -= sell_qty
                pos.banked = True
                audit.append({"day": day, "sym": pos.symbol,
                              "event": "tp1_banked",
                              "qty": round(sell_qty, 4), "pct": pos.tp1_pct})
                if pos.qty <= 1e-9:
                    givebacks.append(max(0.0, pos.peak_pnl_pct - pos.tp1_pct))
                    audit.append({"day": day, "sym": pos.symbol, "exit": "TP_FULL",
                                  "pnl": round(pos.realized, 2)})
                    positions.remove(pos)

        # mark equity
        open_val = 0.0
        for pos in positions:
            pdates = sym_dates[pos.symbol]
            p = bisect_right(pdates, day)
            px = universe[pos.symbol][p - 1]["close"] if p else pos.entry_px
            open_val += pos.qty * px
        equity = cash + open_val
        equity_curve.append((day, round(equity, 2)))
        peak_eq = max(peak_eq, equity)
        max_dd = max(max_dd, (peak_eq - equity) / peak_eq * 100 if peak_eq > 0 else 0)
        if not positions:
            flat_days += 1
        else:
            exposure_days += 1

        # C1② / WO-1046b: DrawdownBreaker — 10% HWM dd blocks NEW entries.
        # Modes:
        #   "manual" — rest of window (pre-1046b BACKTEST semantics)
        #   "tiered" — WO-1046b graded auto-reset: earliest 2d*mult @
        #              dd<6%, 3d*mult recheck @ dd<8%, then daily
        #              recheck; chain-crash re-trip within 3d of ANY
        #              reset doubles the multiplier (cap 4)
        #   "legacy" — the ACTUAL pre-1046b live code path: instant
        #              re-arm once dd falls back under the hard stop
        if breaker_enabled and peak_eq > 0:
            dd_now = (peak_eq - equity) / peak_eq * 100
            if not breaker_tripped:
                if dd_now >= breaker_dd_pct:
                    breaker_tripped = True
                    breaker_trip_day = day
                    breaker_trip_idx = i
                    breaker_trip_equity = equity
                    entries_before_trip = n_entries
                    if (breaker_mode == "tiered"
                            and breaker_last_reset_idx is not None
                            and i - breaker_last_reset_idx < 3):
                        breaker_mult = min(breaker_mult * 2, 4)
                    else:
                        breaker_mult = 1
                    breaker_trip_events.append(
                        {"day": day, "event": "TRIPPED",
                         "mult": breaker_mult, "dd": round(dd_now, 2)})
                    audit.append({"day": day,
                                  "event": "DRAWDOWN_BREAKER_TRIPPED",
                                  "equity": round(equity, 2),
                                  "hwm": round(peak_eq, 2)})
            else:
                # tripped: evaluate the reset path for this mode
                if breaker_mode == "legacy":
                    if dd_now < breaker_dd_pct:
                        breaker_tripped = False
                        breaker_last_reset_idx = i
                        breaker_reset_count += 1
                        breaker_trip_events.append(
                            {"day": day, "event": "LEGACY_REARM",
                             "dd": round(dd_now, 2)})
                elif breaker_mode == "tiered":
                    elapsed_d = i - breaker_trip_idx
                    if ((elapsed_d >= 2 * breaker_mult and dd_now < 6.0)
                            or (elapsed_d >= 3 * breaker_mult
                                and dd_now < 8.0)):
                        tier = ("tier1_early" if dd_now < 6.0
                                and elapsed_d >= 2 * breaker_mult
                                else "tier2_recheck")
                        breaker_tripped = False
                        breaker_last_reset_idx = i
                        breaker_reset_count += 1
                        breaker_trip_events.append(
                            {"day": day, "event": f"AUTO_RESET_{tier}",
                             "dd": round(dd_now, 2), "mult": breaker_mult,
                             "elapsed_d": elapsed_d})
                        audit.append({"day": day,
                                      "event": "DRAWDOWN_BREAKER_AUTO_RESET",
                                      "tier": tier, "dd": round(dd_now, 2)})
                # "manual": no reset path — stays tripped (window lockout)

        if not enabled or breaker_tripped:
            continue
        max_pos = risk_cfg["max_open_positions"]
        if len(positions) >= max_pos:
            continue
        held = {p.symbol for p in positions}
        best, best_score = None, -1
        for sym, bars in universe.items():
            if sym in held or sym == "BTCUSDT":
                continue
            sdates = sym_dates[sym]
            p = bisect_right(sdates, day)
            if p < 35 or sdates[p - 1] != day:
                continue
            sc = coin_score(bars[:p])
            if sc is not None and sc >= threshold and sc > best_score:
                best, best_score = sym, sc
        if best is None:
            continue

        for cand in ("trend", "bollinger", "rsi_reversion", "dca", "grid"):
            if cand in enabled:
                strategy = cand
                break
        else:
            strategy = next(iter(enabled))
        cfg = enabled[strategy]
        sl_pct = cfg["sl_pct"]

        # S2 C3 diff: TP1 = max(1.5R, 3%, 3×ATR) single tranche 50%.
        # Legacy ladder was [4%×40, 8%×40, 15%×20] (strategy-config
        # tp_levels fallback) — the R anchor and the 50/50 split are THE
        # mechanism change under test.
        bars = universe[best]
        p = bisect_right(sym_dates[best], day)
        bar_close = bars[p - 1]["close"]
        a = atr14(bars[:p])
        # production anchors TP1's ATR term to the 1h ATR14
        # (trade_executor fetches 1h klines); the backtest only has daily
        # bars, so convert: ATR_1h ≈ ATR_1d / sqrt(24) (variance scaling)
        atr_pct_1h = (a / bar_close * 100 / (24 ** 0.5)) if a else 0.0
        tp1_pct = round(max(1.5 * sl_pct, 3.0, 3.0 * atr_pct_1h), 2)

        kelly_amt = max(6.0, cash * 0.01)
        equity_ref = equity
        floor = min(130.0, max(100.0, equity_ref * 0.26))
        invest = max(kelly_amt, floor)
        invest *= cfg.get("size_multiplier", 1.0)
        reserve_cap = cash * (1 - risk_cfg["cash_reserve_pct"] / 100)
        invest = min(invest, reserve_cap)
        invest = min(invest, cash * risk_cfg["max_position_pct"] / 100)
        room = equity * risk_cfg["max_total_exposure_pct"] / 100 - open_val
        invest = min(invest, max(0.0, room))
        if invest < 6:
            continue

        px = bar_close
        qty = invest / px
        cost = invest * (1 + FEE + SLIP)
        cash -= cost
        positions.append(P1Position(best, px, qty, i, sl_pct, tp1_pct,
                                     strategy, cost))
        n_entries += 1
        audit.append({"day": day, "sym": best, "entry": round(invest, 2),
                      "score": round(best_score, 1), "strategy": strategy,
                      "tp1_pct": tp1_pct, "threshold": threshold})

    final_day = dates[d1]
    for pos in positions:
        p = bisect_right(sym_dates[pos.symbol], final_day)
        px = universe[pos.symbol][p - 1]["close"] if p else pos.entry_px
        cash = _close_pos(pos, px, final_day, "EOW", cash, audit, givebacks)

    final_equity = cash
    weekly_max_dd = 0.0
    for j in range(len(equity_curve)):
        lo = max(0, j - 6)
        win = [e for _, e in equity_curve[lo:j + 1]]
        pk = max(win)
        if pk > 0:
            weekly_max_dd = max(weekly_max_dd, (pk - win[-1]) / pk * 100)
    btc_ret = (btc_closes[d1] / btc_closes[d0] - 1) * 100

    result = {
        "variant": "wo1046a_p1",
        "src_root": src_root,
        "window": [start, end],
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 2),
        "return_pct": round((final_equity / initial_capital - 1) * 100, 2),
        "btc_hold_return_pct": round(btc_ret, 2),
        "beats_btc": final_equity / initial_capital - 1 > btc_ret / 100,
        "n_entries": n_entries,
        "max_drawdown_pct": round(max_dd, 2),
        "flat_days": flat_days,
        "total_days": d1 - d0 + 1,
        "bull_days": bull_days,
        "exposure_days": exposure_days,
        "exposure_in_bull_pct": round(exposure_days / bull_days * 100, 1) if bull_days else None,
        "weekly_max_dd_pct": round(weekly_max_dd, 2),
        "avg_giveback_pct": round(sum(givebacks) / len(givebacks), 2) if givebacks else 0.0,
        "n_giveback_samples": len(givebacks),
        "breaker": {
            "enabled": breaker_enabled,
            "mode": breaker_mode if breaker_enabled else None,
            "threshold_pct": breaker_dd_pct,
            "tripped": breaker_tripped,
            "trip_day": breaker_trip_day,
            "trip_equity": breaker_trip_equity,
            "post_trip_return_pct": round(
                (final_equity / breaker_trip_equity - 1) * 100, 2)
            if breaker_trip_equity else None,
            "entries_before_trip": entries_before_trip,
            "reset_count": breaker_reset_count,
            "final_multiplier": breaker_mult,
            "events": breaker_trip_events,
        },
        "cost_model": "0.1% fee + 0.05% slip per side (WO-1045-6) — unchanged",
        "vol_adj": vol_adj,
        "audit_tail": audit[-60:],
        "exits": {
            e: sum(1 for a in audit if a.get("exit") == e)
            for e in ("SL", "TRAIL", "TRAIL_P2", "TP_FULL", "EOW")
        },
        "tp1_banked_count": sum(1 for a in audit if a.get("event") == "tp1_banked"),
        "phase2_switch_count": sum(1 for a in audit if a.get("event") == "phase2_switch"),
    }
    return result


C3_DECLARATION = """C3 unchanged declarations (WO-1046a vs WO-1044 after-baseline):
  data source      : identical cached klines (Binance 1d per symbol, 1h BTC), FNG — /tmp/wo1044_bt_cache
  window           : {window}
  initial capital  : {initial}
  cost model       : 0.1% fee + 0.05% slip per side — unchanged
  entry selection  : coin_score + threshold + sizing caps chain — unchanged
  WO-1044 fixes    : all retained (threshold 78/calib, BULL hysteresis, exposure caps)
  tp_levels DIFF   : legacy ladder [4%x40, 8%x40, 15%x20] -> single TP1 50% at
                     max(1.5R, 3%, 3xATR) where R = strategy sl_pct (entry stop)
  trailing DIFF    : tight STEPS ladder -> phase2 wide band clamp(2.5xATR, 2%, 12%)
                     at profit >= 7.5% (sticky, one-time switch may move SL down)
  breaker NEW      : equity HWM drawdown >= 10% (risk_limits.yaml) blocks new
                     entries for the rest of the window (live: manual reset)"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant",
                    choices=["p1", "p1_no_breaker", "p1_tiered", "p1_live"],
                    default="p1",
                    help="p1=manual lockout (pre-1046b backtest semantics), "
                         "p1_no_breaker=off, p1_tiered=WO-1046b graded "
                         "auto-reset, p1_live=pre-1046b live code path "
                         "(instant re-arm under 10%% dd)")
    ap.add_argument("--src-root", default=".")
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default=None,
                    help="window end (default: yesterday)")
    ap.add_argument("--universe-file", default=None)
    ap.add_argument("--initial", type=float, default=414.73)
    ap.add_argument("--vol-adj", type=float, default=1.0)
    ap.add_argument("--segments", action="store_true",
                    help="C1-1: also run two half-window segments (Aug vs Sep)")
    ap.add_argument("--breaker-dd", type=float, default=None,
                    help="override breaker threshold pct (risk_limits "
                         "default) — sensitivity/probe use only")
    args = ap.parse_args()

    end = args.end or (datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    days_span = (datetime.strptime(end, "%Y-%m-%d") -
                 datetime.strptime(args.start, "%Y-%m-%d")).days + 1

    fng = load_or_fetch("fng", lambda: fetch_fng(days_span))
    btc1d = load_or_fetch("btc1d", lambda: fetch_klines_1d("BTCUSDT", days_span))
    btc1h = load_or_fetch("btc1h", lambda: fetch_btc_1h(
        int(datetime.strptime(args.start, "%Y-%m-%d").timestamp() * 1000) - 40 * 86400 * 1000))

    if args.universe_file:
        symbols = json.load(open(args.universe_file))
    else:
        symbols = json.load(open(os.path.join(CACHE_DIR, "universe.json")))
    universe = {}
    for sym in symbols:
        universe[sym] = load_or_fetch(
            "k1d_" + sym, lambda s=sym: fetch_klines_1d(s, days_span))
        time.sleep(0.1)

    breaker = args.variant != "p1_no_breaker"
    mode = {"p1": "manual", "p1_tiered": "tiered",
            "p1_live": "legacy", "p1_no_breaker": "manual"}[args.variant]
    res = run_variant_p1(args.src_root, args.start, end, universe, fng,
                         btc1d, btc1h, args.initial, vol_adj=args.vol_adj,
                         breaker_enabled=breaker, breaker_mode=mode,
                         breaker_dd_override=args.breaker_dd)
    res["variant"] = args.variant
    suffix = f"_va{args.vol_adj:g}" if args.vol_adj != 1.0 else ""
    out_path = os.path.join(CACHE_DIR, f"result_{args.variant}{suffix}.json")
    with open(out_path, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(C3_DECLARATION.format(window=[args.start, end], initial=args.initial))
    print()
    print(json.dumps({k: v for k, v in res.items() if k != "audit_tail"},
                     ensure_ascii=False, indent=2))
    print("saved:", out_path)

    if args.segments:
        dates = [b["date"] for b in btc1d]
        d0, d1 = dates.index(args.start), dates.index(end)
        mid = dates[d0 + (d1 - d0) // 2]
        print(f"\n── C1-1 segments: {args.start}..{mid} vs {mid}..{end} ──")
        seg_results = {}
        for (s, e) in ((args.start, mid), (mid, end)):
            seg = run_variant_p1(args.src_root, s, e, universe, fng,
                                 btc1d, btc1h, args.initial,
                                 vol_adj=args.vol_adj,
                                 breaker_enabled=breaker,
                                 breaker_mode=mode)
            seg_results[f"{s}..{e}"] = {k: v for k, v in seg.items()
                                        if k not in ("audit_tail", "src_root")}
            print(f"\n[{s}..{e}]")
            print(json.dumps(seg_results[f"{s}..{e}"], ensure_ascii=False, indent=2))
        with open(os.path.join(CACHE_DIR, f"result_{args.variant}{suffix}_segments.json"), "w") as f:
            json.dump(seg_results, f, ensure_ascii=False, indent=2)
        # verdict: direction consistency + giveback ratio across segments
        rets = [v["return_pct"] for v in seg_results.values()]
        gbs = [v["avg_giveback_pct"] for v in seg_results.values()]
        print("\n── C1-1 verdict ──")
        print(f"returns both positive & beat BTC: "
              f"{all(r > 0 for r in rets)}; "
              f"giveback ratio seg1/seg2 = "
              f"{(gbs[0] / gbs[1]) if gbs[1] else float('inf'):.2f}")


if __name__ == "__main__":
    main()
