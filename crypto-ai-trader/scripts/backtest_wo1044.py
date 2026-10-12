#!/usr/bin/env python3
"""WO-1044 pipeline replay backtest: strategy-deadlock fixes A-E, 2026-08-01 → today.

Compares the FULL scan→size→protect pipeline BEFORE (git worktree at the
pre-WO-1044 commit) vs AFTER (working tree) on identical daily inputs,
benchmarked against buy-and-hold BTC (acceptance bar: beat BTC %).

Faithful pieces (imported live from --src-root):
  - StrategyAdaptor.adapt() full chain (regime map, BTC/vol/funding
    overlays, GARCH SL/TP, HMM overlay incl. WO-1044-A arbitration,
    evolver veto disabled for replay, bandit pinned 1.0)
  - AdaptiveTrailingStop STEPS (before 6/5/3/2, after 2.5/2/2/1.5)
  - risk_limits.yaml caps per src-root (before 10/50/50/5, after 30/95/10/4)
  - btc_trend_gate tier logic (replicated 1:1 on historical closes)

Emulated pieces (documented approximations):
  - Per-coin signal score: klines-computable factor subset (7d/30d
    momentum, volume surge, SMA20 deviation) — NOT the live six-factor
    agent blend (news/onchain/orderbook history unavailable).
  - Executor sizing: Kelly degenerate-history path emulated as the 1%
    floor (matches live $6-10 dust); after-variant adds the WO-1044-E
    concentration floor clamp(equity*0.26, 100, 130). All caps applied.
  - Daily granularity, one entry per day max, entry at day close;
    SL-before-TP intra-day ordering (pessimistic); fees 0.1%/side.
  - HMM model applied retrospectively (trained state from state.db) —
    generic vol/trend clustering, noted as approximation.

Usage:
  python3 scripts/backtest_wo1044.py --variant before --src-root /tmp/wo1044_before
  python3 scripts/backtest_wo1044.py --variant after  --src-root .
"""
import argparse
import json
import math
import os
from bisect import bisect_right
import sys
import time
from datetime import datetime, timezone

import requests

CACHE_DIR = "/tmp/wo1044_bt_cache"
FEE = 0.001  # per side
SLIP = 0.0005  # per-side slippage (WO-1045-6: 0.1% fee + 0.05% slip per side)


# ────────────────────────── data layer ──────────────────────────

def _http_get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch_fng(days):
    """alternative.me F&G daily history (ascending)."""
    data = _http_get("https://api.alternative.me/fng/",
                     {"limit": min(days + 5, 1000), "format": "json"})
    out = {}
    for item in data["data"]:
        d = datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
        out[d.strftime("%Y-%m-%d")] = int(item["value"])
    return out


def fetch_klines_1d(symbol, total_days):
    limit = min(total_days + 70, 1000)  # lookback for indicators
    data = _http_get("https://api.binance.com/api/v3/klines",
                     {"symbol": symbol, "interval": "1d", "limit": limit})
    bars = []
    for k in data:
        bars.append({
            "date": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
        })
    return bars


def fetch_btc_1h(start_ms):
    """BTC 1h klines from start_ms to now (for HMM), ascending raw lists."""
    out, end_time = [], None
    while True:
        params = {"symbol": "BTCUSDT", "interval": "1h", "limit": 1000}
        if end_time:
            params["endTime"] = end_time
        data = _http_get("https://api.binance.com/api/v3/klines", params)
        if not data:
            break
        out = data + out
        earliest = data[0][0]
        if earliest <= start_ms or len(data) < 1000:
            break
        end_time = earliest - 1
        time.sleep(0.3)
    return out


def load_or_fetch(cache_key, producer):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, cache_key + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    val = producer()
    with open(path, "w") as f:
        json.dump(val, f)
    return val


# ────────────────────────── indicators ──────────────────────────

def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def atr14(bars):
    if len(bars) < 15:
        return None
    trs = []
    for i in range(-14, 0):
        h, l = bars[i]["high"], bars[i]["low"]
        pc = bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def coin_score(bars):
    """Klines-computable momentum composite (0-100), scanner subset."""
    if len(bars) < 35:
        return None
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    c = closes[-1]
    ret7 = c / closes[-8] - 1
    ret30 = c / closes[-31] - 1
    vol_ma = sum(vols[-21:-1]) / 20
    vol_ratio = vols[-1] / vol_ma if vol_ma > 0 else 1.0
    s20 = sma(closes[-21:-1], 20) or c
    dev = (c - s20) / s20
    score = (
        50
        + 18 * math.tanh(ret7 / 0.08)
        + 12 * math.tanh(ret30 / 0.15)
        + 10 * min(vol_ratio - 1.0, 1.0)
        + 10 * math.tanh(dev / 0.05)
    )
    return max(0.0, min(100.0, score))


def btc_score_approx(closes):
    if len(closes) < 60:
        return 50.0
    e21, e55 = ema(closes[-60:], 21), ema(closes[-60:], 55)
    r = rsi(closes[-20:])
    macd_up = e21 is not None and e55 is not None and e21 > e55
    s = 50 + (20 if macd_up else -20) + 15 * max(-1.5, min(1.5, (r - 50) / 25))
    return max(5.0, min(95.0, s))


def btc_tier(closes):
    """Faithful replica of btc_trend_gate tier logic on daily closes."""
    if len(closes) < 200:
        return "PROXIMITY_WARMUP"
    p = closes[-1]
    s200 = sum(closes[-200:]) / 200
    s100 = sum(closes[-100:]) / 100
    slope_ref = sum(closes[-214:-14]) / 200 if len(closes) >= 214 else s200
    slope = (s200 / slope_ref - 1) * 100
    dev = (p / s200 - 1) * 100
    if p < s100 and slope < -0.1:
        return "DEEP_BEAR"
    if p >= s200 and slope >= 0:
        return "CONFIRMED_BULL"
    if p >= s100:
        return "PROXIMITY_WARMUP" if abs(dev) < 5.0 else "TRANSITION"
    return "PROXIMITY_WARMUP"


# ────────────────────────── simulation ──────────────────────────

class Position:
    __slots__ = ("symbol", "entry_px", "qty", "qty0", "entry_day_idx",
                 "sl", "highest", "activated", "atr", "tp_idx", "tp_levels",
                 "strategy", "realized", "entry_amount")

    def __init__(self, symbol, px, qty, day_idx, sl_pct, tp_levels, strategy, amount):
        self.symbol = symbol
        self.entry_px = px
        self.qty = self.qty0 = qty
        self.entry_day_idx = day_idx
        self.sl = px * (1 - sl_pct / 100)
        self.highest = px
        self.activated = False
        self.atr = None
        self.tp_idx = 0
        self.tp_levels = tp_levels
        self.strategy = strategy
        self.realized = 0.0
        self.entry_amount = amount


def run_variant(variant, src_root, start, end, universe, fng, btc1d, btc1h,
                initial_capital=414.73, vol_adj=1.0):
    sys.path.insert(0, os.path.abspath(src_root))
    for m in list(sys.modules):
        if m == "src" or m.startswith("src."):
            del sys.modules[m]

    import yaml
    with open(os.path.join(src_root, "config", "risk_limits.yaml")) as f:
        risk_cfg = yaml.safe_load(f)["risk"]

    from src.strategy_adaptor import StrategyAdaptor
    from src.adaptive_trailing import AdaptiveTrailingStop
    from src import hmm_regime as hmm_mod

    # ── replay determinism patches ──
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

    ats = AdaptiveTrailingStop()

    # date index helpers
    dates = [b["date"] for b in btc1d]
    d0, d1 = dates.index(start), dates.index(end)
    btc_closes = [b["close"] for b in btc1d]

    # btc 1h bars indexed by day-open ms
    day_open_ms = {}
    for b in btc1d:
        day_open_ms[b["date"]] = None
    ms_list = []
    for k in btc1h:
        d = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        ms_list.append((d, k))
    bars_by_day = {}
    for d, k in ms_list:
        bars_by_day.setdefault(d, []).append(k)
    # first 1h-bar index per day (fast HMM window lookup)
    day_open_idx = {}
    for j, (d, _) in enumerate(ms_list):
        if d not in day_open_idx:
            day_open_idx[d] = j
    # per-symbol date index (k1d lists start at various dates — index by
    # date, never by the btc1d positional index)
    sym_dates = {sym: [b["date"] for b in bars] for sym, bars in universe.items()}

    cash = initial_capital
    positions = []
    equity_curve, peak_eq, max_dd = [], 0.0, 0.0
    flat_days = 0
    bull_days = 0        # WO-1045-5: should-be-in-market days (CONFIRMED_BULL)
    exposure_days = 0    # WO-1045-5: days with at least one open position
    n_entries = 0
    realized_total = 0.0
    audit = []

    for i in range(d0, d1 + 1):
        day = dates[i]
        closes_hist = btc_closes[: i + 1]
        change_24h = (btc_closes[i] / btc_closes[i - 1] - 1) * 100
        fng_v = fng.get(day, 60)
        tier = btc_tier(closes_hist)
        if tier == "CONFIRMED_BULL":
            bull_days += 1
        bs = btc_score_approx(closes_hist)

        # HMM prediction on trailing 30d of 1h bars
        j0 = day_open_idx.get(day)
        if j0 is not None:
            window = [k for _, k in ms_list[max(0, j0 - 720):j0 + 1]]
        else:
            window = []
        try:
            hmm_detector._replay_pred = None
            if len(window) >= 60:
                pred = hmm_detector.predict(window)
                if pred and pred.get("confidence", 0) > 0.4:
                    hmm_detector._replay_pred = pred
        except Exception:
            hmm_detector._replay_pred = None

        adaptor = StrategyAdaptor()
        # no look-ahead GARCH window: trailing 31 daily bars as raw lists
        adaptor._btc_klines_cache = [
            [0, "0", "0", "0", str(c), "0", 0, "0", 0] for c in btc_closes[max(0, i - 30):i + 1]
        ]
        adaptor._btc_klines_ts = time.time()

        btc_trend_str = "BULLISH" if bs > 65 else ("BEARISH" if bs < 35 else "NEUTRAL")
        adapted = adaptor.adapt(
            fear_greed=fng_v,
            btc_trend=btc_trend_str,
            btc_price_change_24h=change_24h,
            funding_rate=None,
            btc_adx=None,
            btc_score=bs,
            bull_tier=tier,
        )
        threshold = adapted["global"]["score_threshold"]
        enabled = {n: c for n, c in adapted["strategies"].items() if c.get("enabled")}

        # ── open-position daily simulation (SL first, then trail, then TP) ──
        for pos in list(positions):
            pdates = sym_dates[pos.symbol]
            p = bisect_right(pdates, day)
            if p == 0 or pdates[p - 1] != day:
                continue  # symbol has no bar for this day
            bar = universe[pos.symbol][p - 1]
            pos.highest = max(pos.highest, bar["high"])
            profit = (bar["close"] - pos.entry_px) / pos.entry_px
            a = atr14(universe[pos.symbol][:p])
            if a:
                pos.atr = a
            # activation (1×ATR, mirrors risk_manager.TrailingStop)
            if (not pos.activated) and pos.atr and (bar["close"] - pos.entry_px) >= pos.atr:
                pos.activated = True
            # hard SL
            if bar["low"] <= pos.sl:
                proceeds = pos.qty * pos.sl * (1 - FEE - SLIP)
                pos.realized += proceeds - pos.entry_amount * (pos.qty / pos.qty0)
                cash += proceeds
                positions.remove(pos)
                audit.append({"day": day, "sym": pos.symbol, "exit": "SL", "pnl": round(pos.realized, 2)})
                continue
            # trailing SL
            if pos.activated:
                t = ats.calculate_trailing_sl(
                    entry_price=pos.entry_px, current_price=bar["close"],
                    highest_price=pos.highest, initial_sl=pos.sl,
                    volatility_adjustment=vol_adj,
                )
                if t["trailing_active"] and t["trailing_sl"] > pos.sl:
                    pos.sl = t["trailing_sl"]
                if bar["low"] <= pos.sl:
                    proceeds = pos.qty * pos.sl * (1 - FEE - SLIP)
                    pos.realized += proceeds - pos.entry_amount * (pos.qty / pos.qty0)
                    cash += proceeds
                    positions.remove(pos)
                    audit.append({"day": day, "sym": pos.symbol, "exit": "TRAIL", "pnl": round(pos.realized, 2)})
                    continue
            # TP ladder (partial)
            while pos.tp_idx < len(pos.tp_levels):
                tp = pos.tp_levels[pos.tp_idx]
                if bar["high"] >= pos.entry_px * (1 + tp["pct"] / 100):
                    sell_qty = pos.qty0 * tp["size_pct"] / 100
                    sell_qty = min(sell_qty, pos.qty)
                    px = pos.entry_px * (1 + tp["pct"] / 100)
                    proceeds = sell_qty * px * (1 - FEE - SLIP)
                    pos.realized += proceeds - pos.entry_amount * (sell_qty / pos.qty0)
                    cash += proceeds
                    pos.qty -= sell_qty
                    pos.tp_idx += 1
                    if pos.qty <= 1e-9:
                        positions.remove(pos)
                        audit.append({"day": day, "sym": pos.symbol, "exit": "TP_FULL", "pnl": round(pos.realized, 2)})
                        break
                else:
                    break

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

        # ── new entry (one per day, close price) ──
        if not enabled:
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
            hist = bars[:p]
            sc = coin_score(hist)
            if sc is not None and sc >= threshold and sc > best_score:
                best, best_score = sym, sc
        if best is None:
            continue

        # strategy pick: prefer trend, then bollinger, rsi, dca, grid
        for cand in ("trend", "bollinger", "rsi_reversion", "dca", "grid"):
            if cand in enabled:
                strategy = cand
                break
        else:
            strategy = next(iter(enabled))
        cfg = enabled[strategy]
        sl_pct = cfg["sl_pct"]
        tp_levels = cfg.get("tp_levels") or [
            {"pct": 4, "size_pct": 40}, {"pct": 8, "size_pct": 40}, {"pct": 15, "size_pct": 20}]

        # sizing: Kelly degenerate-history floor path emulated
        kelly_amt = max(6.0, cash * 0.01)
        if variant == "after":
            equity_ref = equity
            floor = min(130.0, max(100.0, equity_ref * 0.26))
            invest = max(kelly_amt, floor)
        else:
            invest = kelly_amt
        invest *= cfg.get("size_multiplier", 1.0)

        # caps chain (mirror execute_auto_trade order)
        reserve_cap = cash * (1 - risk_cfg["cash_reserve_pct"] / 100)
        invest = min(invest, reserve_cap)
        invest = min(invest, cash * risk_cfg["max_position_pct"] / 100)
        room = equity * risk_cfg["max_total_exposure_pct"] / 100 - open_val
        invest = min(invest, max(0.0, room))
        if invest < 6:
            continue

        px = universe[best][bisect_right(sym_dates[best], day) - 1]["close"]
        qty = invest / px
        cost = invest * (1 + FEE + SLIP)
        cash -= cost
        positions.append(Position(best, px, qty, i, sl_pct, tp_levels, strategy, cost))
        n_entries += 1
        audit.append({"day": day, "sym": best, "entry": round(invest, 2),
                      "score": round(best_score, 1), "strategy": strategy,
                      "threshold": threshold})

    # close remainder at final close
    final_day = dates[d1]
    for pos in positions:
        p = bisect_right(sym_dates[pos.symbol], final_day)
        px = universe[pos.symbol][p - 1]["close"] if p else pos.entry_px
        proceeds = pos.qty * px * (1 - FEE - SLIP)
        pos.realized += proceeds - pos.entry_amount * (pos.qty / pos.qty0)
        cash += proceeds
        audit.append({"day": final_day, "sym": pos.symbol, "exit": "EOW", "pnl": round(pos.realized, 2)})

    final_equity = cash
    # WO-1045-5: rolling 7-day max drawdown
    weekly_max_dd = 0.0
    for j in range(len(equity_curve)):
        lo = max(0, j - 6)
        win = [e for _, e in equity_curve[lo:j + 1]]
        pk = max(win)
        if pk > 0:
            weekly_max_dd = max(weekly_max_dd, (pk - win[-1]) / pk * 100)
    btc_ret = (btc_closes[d1] / btc_closes[d0] - 1) * 100
    result = {
        "variant": variant,
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
        "cost_model": "0.1% fee + 0.05% slip per side (WO-1045-6)",
        "vol_adj": vol_adj,
        "audit_tail": audit[-40:],
        "exits": {
            e: sum(1 for a in audit if a.get("exit") == e)
            for e in ("SL", "TRAIL", "TP_FULL", "EOW")
        },
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["before", "after"], required=True)
    ap.add_argument("--src-root", default=".")
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--universe-file", default=None,
                    help="JSON list of symbols (default: auto from cache)")
    ap.add_argument("--initial", type=float, default=414.73)
    ap.add_argument("--vol-adj", type=float, default=1.0,
                    help="trailing volatility_adjustment sensitivity (WO-1045-1)")
    args = ap.parse_args()

    # window end = yesterday (complete daily bars)
    end = (datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    days_span = (datetime.now(timezone.utc) - datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)).days + 1

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
        time.sleep(0.15)

    res = run_variant(args.variant, args.src_root, args.start, end,
                      universe, fng, btc1d, btc1h, args.initial, vol_adj=args.vol_adj)
    suffix = f"_va{args.vol_adj:g}" if args.vol_adj != 1.0 else ""
    out_path = os.path.join(CACHE_DIR, f"result_{args.variant}{suffix}.json")
    with open(out_path, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in res.items() if k != "audit_tail"},
                     ensure_ascii=False, indent=2))
    print("saved:", out_path)


if __name__ == "__main__":
    main()
