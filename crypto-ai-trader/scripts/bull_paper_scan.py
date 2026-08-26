#!/usr/bin/env python3
"""
BULL Phase 2 Paper Trading Scan.

Called every 4H (aligned with scan schedule) to:
  1. Evaluate BULL regime (BTC SMA200, F&G, ADX)
  2. Process exits for open paper positions
  3. Look for new core/satellite entries using scanner results
  4. Check pyramiding opportunities
  5. Update capture ratio
  6. Generate report section

Can also be run standalone for testing:
  python3 scripts/bull_paper_scan.py
"""

import sys, os, json, time, logging
sys.path.insert(0, os.path.expanduser("~/crypto-ai-trader"))
os.chdir(os.path.expanduser("~/crypto-ai-trader"))

from datetime import datetime
from src.state_db import StateDB
from src.binance_client import BinanceClient
from src.bull_regime import BullRegimeDetector, evaluate_regime, STATE_EMOJI, STATE_CN
from src.bull_paper_engine import (
    BullPaperEngine, TOTAL_CAPITAL, CORE_ALLOCATION, SATELLITE_ALLOCATION,
    CASH_BUFFER, CORE_PER_SLOT, CORE_MAX_SYMBOLS, SAT_MAX_POSITIONS,
    FEE_RATE, SLIPPAGE,
)
from src.capture_tracker import CaptureTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bull_paper")

DB_PATH = "/root/trading-state/state.db"


def get_btc_daily_sma200(client):
    klines = client.get_klines("BTCUSDT", "1d", limit=210)
    closes = [float(k["close"]) for k in klines]
    if len(closes) < 200:
        return None, None
    sma200 = sum(closes[-200:]) / 200
    return closes[-1], sma200


def get_btc_adx_4h(client, period=14):
    klines = client.get_klines("BTCUSDT", "4h", limit=period*5+50)
    if len(klines) < period*3:
        return None
    from src.bull_paper_engine import _adx
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    closes = [float(k["close"]) for k in klines]
    return _adx(highs, lows, closes, period)


def get_fng_history():
    from src.data_feed_fng import FearGreedIndex
    feed = FearGreedIndex()
    data = feed.get_history(limit=10)
    result = {}
    for item in data:
        dt = datetime.strptime(item["timestamp"], "%Y-%m-%d")
        day_epoch = int(dt.timestamp()) - (int(dt.timestamp()) % 86400)
        result[day_epoch] = int(item["value"])
    return result


def run_paper_scan(scanner_opportunities=None, dry_run=False):
    """
    Main paper scan entry point.

    Args:
        scanner_opportunities: list of opportunity dicts from live scanner.
            Each should have 'symbol', 'score', etc. If None, will fetch top movers.
        dry_run: if True, don't execute trades, just evaluate and report.

    Returns:
        dict with regime, actions, report text.
    """
    db = StateDB(DB_PATH)
    client = BinanceClient()

    # ── 1. Evaluate regime ───────────────────────────────────────────
    det = BullRegimeDetector(db=db, client=client)
    state = det.load_state()
    btc_close, btc_sma200 = get_btc_daily_sma200(client)
    btc_adx = get_btc_adx_4h(client)
    fng_hist = get_fng_history()
    now_ms = int(time.time() * 1000)

    old_regime = state.regime
    state, transition = evaluate_regime(
        state, btc_close, btc_sma200, btc_close, btc_adx, fng_hist, now_ms
    )
    det.save_state(state)
    if transition:
        det.record_transition(transition)
        logger.info(f"Regime transition: {transition['from']} -> {transition['to']}")

    regime = state.regime
    logger.info(f"Regime: {STATE_EMOJI.get(regime,'')} {regime}")

    # ── 2. Get prices ────────────────────────────────────────────────
    engine = BullPaperEngine(db, client)
    open_positions = engine.portfolio.get_open_positions()
    all_symbols = {p["symbol"] for p in open_positions}
    prices = {}
    for sym in all_symbols:
        try:
            prices[sym] = engine.get_price(sym)
        except Exception as e:
            logger.warning(f"Price fetch failed for {sym}: {e}")
            prices[sym] = 0

    # ── 3. Process exits ─────────────────────────────────────────────
    actions = []
    core_exits = engine.process_core_exits(regime, prices)
    actions.extend(core_exits)
    sat_exits = engine.process_satellite_exits(prices)
    actions.extend(sat_exits)

    # Refresh prices after exits
    open_positions = engine.portfolio.get_open_positions()
    all_symbols = {p["symbol"] for p in open_positions}
    prices = {}
    for sym in all_symbols:
        try:
            prices[sym] = engine.get_price(sym)
        except Exception:
            prices[sym] = 0

    # ── 4. Check pause conditions ────────────────────────────────────
    status = engine.get_status()
    pause_alerts = []
    if status["total_return"] < -0.216:
        pause_alerts.append(f"🛑 PAPER PAUSED: Core MaxDD {status['total_return']:.2%} < -21.6%")
    if status["ema50_rate"] > 0.35 and status["core_exit_count"] >= 5:
        pause_alerts.append(f"🛑 PAPER PAUSED: EMA50_REDUCE rate {status['ema50_rate']:.0%} > 35%")
    if status["avg_slippage"] > 0.001:
        pause_alerts.append(f"🛑 PAPER PAUSED: Avg slippage {status['avg_slippage']*100:.2f}% > 0.10%")

    # Check whipsaw (regime transitions in last 7 days)
    transitions = det.get_transitions(50)
    week_ago = now_ms - 7 * 86400_000
    recent_transitions = [t for t in transitions if t["ts"] >= week_ago]
    bull_neutral_swaps = sum(
        1 for t in recent_transitions
        if {t["from_state"], t["to_state"]} == {"CONFIRMED_BULL", "NEUTRAL"}
        or {t["from_state"], t["to_state"]} == {"CONFIRMED_BULL", "FEAR"}
        or {t["from_state"], t["to_state"]} == {"CONFIRMED_BULL", "DEEP_BEAR"}
    )
    if bull_neutral_swaps > 3:
        pause_alerts.append(
            f"🛑 PAPER PAUSED: {bull_neutral_swaps} CONFIRMED_BULL↔NEUTRAL swaps this week (>3)"
        )

    # ── 5. New entries (only if no pause) ────────────────────────────
    if not pause_alerts and not dry_run:
        if regime == "CONFIRMED_BULL":
            # Try core entries from scanner opportunities or top movers
            # Core selection per backtest select_core_symbols():
            # BTC always takes slot 1 if it meets entry.
            # Slot 2 goes to higher ADX between SOL and ETH.
            # This mirrors the walk-forward validated logic exactly.
            core_candidates = []
            for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                try:
                    kl = engine.get_klines(sym, "4h", limit=250)
                    from src.bull_paper_engine import compute_indicators
                    ind = compute_indicators(kl)
                    ok, reason = engine.evaluate_core_entry(sym, ind)
                    if ok:
                        adx_val = ind["adx"] or 0
                        core_candidates.append((sym, adx_val))
                    else:
                        logger.debug(f"Core {sym} rejected: {reason}")
                except Exception as e:
                    logger.debug(f"Core candidate check {sym}: {e}")

            # BTC always first if it qualifies
            desired = []
            btc_entry = next((c for c in core_candidates if c[0] == "BTCUSDT"), None)
            alt_entries = [c for c in core_candidates if c[0] != "BTCUSDT"]
            alt_entries.sort(key=lambda x: x[1], reverse=True)

            if btc_entry:
                desired.append(btc_entry[0])
            for sym, _ in alt_entries:
                if len(desired) < 2:
                    desired.append(sym)

            # If BTC qualifies but isn't held and both slots are full,
            # rotate out the lower-priority alt (per backtest logic)
            held_core = engine.portfolio.get_open_positions(side="core")
            held_syms = {p["symbol"] for p in held_core}
            if btc_entry and "BTCUSDT" not in held_syms and len(held_core) >= 2:
                # Find the alt to rotate out (lowest ADX among held alts)
                held_alts = [p for p in held_core if p["symbol"] != "BTCUSDT"]
                if held_alts:
                    # Rotate the one NOT in desired (or lower ADX if both)
                    rotate_out = None
                    for p in held_alts:
                        if p["symbol"] not in desired:
                            rotate_out = p
                            break
                    if rotate_out is None:
                        # Both held alts are in desired but BTC needs slot —
                        # rotate the lower ADX one
                        held_alts_with_adx = []
                        for p in held_alts:
                            kl = engine.get_klines(p["symbol"], "4h", limit=250)
                            ind = compute_indicators(kl)
                            held_alts_with_adx.append((p, ind["adx"] or 0))
                        held_alts_with_adx.sort(key=lambda x: x[1])
                        rotate_out = held_alts_with_adx[0][0]

                    if rotate_out:
                        px = engine.get_price(rotate_out["symbol"])
                        # Respect 72h minimum hold for non-SL exits
                        bars_held = engine._bars_since(rotate_out["entry_time"])
                        if bars_held >= 18:
                            engine.portfolio.close_position(
                                rotate_out["id"], px, reason="CORE_ROTATE_BTC"
                            )
                            actions.append({
                                "symbol": rotate_out["symbol"],
                                "action": "ROTATE_OUT",
                                "price": px,
                                "qty": rotate_out["quantity"],
                                "reason": "CORE_ROTATE_BTC",
                            })
                            logger.info(f"Core rotation: closed {rotate_out['symbol']} for BTC")
                        else:
                            logger.info(
                                f"BTC qualifies but {rotate_out['symbol']} in min-hold "
                                f"({bars_held}/18 bars), will rotate after lockup"
                            )

            # Open desired core positions
            for sym in desired[:2]:
                try:
                    result = engine.try_core_entry(sym, 70, regime)
                    if result:
                        actions.append(result)
                        logger.info(f"Core entry: {result}")
                except Exception as e:
                    logger.error(f"Core entry failed for {sym}: {e}")

            # Pyramid
            try:
                pyramids = engine.try_pyramid(prices, regime)
                actions.extend(pyramids)
            except Exception as e:
                logger.error(f"Pyramid failed: {e}")

        # Satellite entries (both CONFIRMED_BULL and MILD_BULL)
        if regime in ("CONFIRMED_BULL", "MILD_BULL"):
            sat_candidates = scanner_opportunities or []
            if not sat_candidates:
                # Use broader universe for satellite
                sat_universe = [
                    "SOLUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
                    "NEARUSDT", "AAVEUSDT", "OPUSDT", "ARBUSDT",
                ]
                for sym in sat_universe:
                    sat_candidates.append({"symbol": sym, "score": 65})

            for opp in sat_candidates[:8]:
                sym = opp.get("symbol", "")
                score = opp.get("score", 60)
                if not sym.endswith("USDT"):
                    sym = sym + "USDT"
                try:
                    result = engine.try_satellite_entry(sym, score, regime)
                    if result:
                        actions.append(result)
                        logger.info(f"Satellite entry: {result}")
                except Exception as e:
                    logger.debug(f"Sat entry failed for {sym}: {e}")

    # ── 5b. P0-C: B-variant A/B engine (same clock, same universe) ──
    b_actions = []
    try:
        from src.bull_paper_engine_b import BullPaperEngineB
        eng_b = BullPaperEngineB(db, client)

        # B prices for B-held symbols
        b_open = eng_b.portfolio.get_open_positions()
        b_prices = {}
        for p in b_open:
            try:
                b_prices[p["symbol"]] = eng_b.get_price(p["symbol"])
            except Exception:
                b_prices[p["symbol"]] = 0

        # thesis-level exits first (regime / EMA200), then risk exits
        b_actions.extend(eng_b.process_b_thesis_exits(regime, b_prices))
        # refresh after thesis exits
        b_open = eng_b.portfolio.get_open_positions()
        b_prices = {}
        for p in b_open:
            try:
                b_prices[p["symbol"]] = eng_b.get_price(p["symbol"])
            except Exception:
                b_prices[p["symbol"]] = 0
        b_actions.extend(eng_b.process_b_exits(b_prices))

        # B entries: same candidate universe A scanned, same time frame
        if not pause_alerts and not dry_run and regime in ("CONFIRMED_BULL", "MILD_BULL"):
            b_candidates = scanner_opportunities or []
            if not b_candidates:
                b_universe = [
                    "BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "LINKUSDT",
                    "DOTUSDT", "NEARUSDT", "AAVEUSDT", "OPUSDT", "ARBUSDT",
                ]
                for sym in b_universe:
                    b_candidates.append({"symbol": sym, "score": 80})
            for opp in b_candidates[:12]:
                sym = opp.get("symbol", "")
                score = opp.get("score", 80)
                if not sym.endswith("USDT"):
                    sym = sym + "USDT"
                try:
                    res = eng_b.try_b_entry(sym, score, regime)
                    if res:
                        b_actions.append(res)
                        logger.info(f"[B] entry: {res}")
                except Exception as e:
                    logger.debug(f"[B] entry failed for {sym}: {e}")
        actions.extend(b_actions)
    except Exception as e:
        logger.error(f"[B] engine failure (non-fatal): {e}")

    # ── 6. Update capture tracker ────────────────────────────────────
    ct = CaptureTracker(db)
    btc_price_now = float(client.get_ticker_price("BTCUSDT"))
    current_status = engine.get_status()
    ct.record(
        paper_value=current_status["total_value"],
        btc_price=btc_price_now,
        core_pnl=sum(
            (prices.get(p["symbol"], p["entry_price"]) - p["entry_price"]) * p["quantity"]
            for p in current_status["positions"] if p["side"] == "core"
        ),
        sat_pnl=sum(
            (prices.get(p["symbol"], p["entry_price"]) - p["entry_price"]) * p["quantity"]
            for p in current_status["positions"] if p["side"] == "satellite"
        ),
    )

    # ── 7. Build report ──────────────────────────────────────────────
    time_info = det.get_time_in_state()
    hours = time_info["hours_in_state"]
    time_str = f"{hours:.0f}h" if hours < 48 else f"{hours/24:.1f}d"

    # F&G avg for display
    recent_fng = list(fng_hist.values())[:7]
    fng_avg = sum(recent_fng) / len(recent_fng) if recent_fng else 0

    report_lines = [
        f"🐂 BULL Phase 2 Paper — {STATE_EMOJI.get(regime,'')} {STATE_CN.get(regime, regime)}",
        f"   持續 {time_str} | BTC ${btc_close:,.0f} vs SMA200 ${btc_sma200:,.0f} "
        f"({(btc_close/btc_sma200-1)*100:+.1f}%) | ADX {btc_adx:.1f} | F&G7d {fng_avg:.0f}",
    ]

    if transition:
        report_lines.append(
            f"   🔄 轉換: {transition['from']} → {transition['to']} | {transition['reason'][:80]}"
        )
    elif state.last_transition_reason:
        report_lines.append(
            f"   上次轉換: {state.last_transition_from}→{regime} "
            f"({time_str}前) | {state.last_transition_reason[:60]}"
        )

    # Condition lights
    conds = []
    conds.append(f"BTC{'✓' if state.btc_above_sma else '✗'}")
    conds.append(f"FNG{'✓' if state.fng_ok else '✗'}")
    conds.append(f"ADX{'✓' if state.btc_adx_ok else '✗'}")
    if regime == "CONFIRMED_BULL":
        conds.append(f"確認{state.confirm_count}根")
    else:
        conds.append(f"計數{state.confirm_count}/2")
    report_lines.append(f"   {' '.join(conds)}")

    report_lines.append("")
    report_lines.append(engine.format_report())
    report_lines.append("")

    # P0-C: A/B comparison block (B engine, independent sleeve)
    try:
        from src.bull_paper_ab_metrics import (
            format_ab_report, snapshot_daily, verify_ab_isolation,
        )
        from src.bull_paper_engine_b import B_START_CASH
        # price map covering both A and B open symbols
        ab_syms = set(prices.keys())
        try:
            for _p in BullPaperEngineB(db, client).portfolio.get_open_positions():
                ab_syms.add(_p["symbol"])
        except Exception:
            pass
        ab_prices = dict(prices)
        for sym in ab_syms:
            if not ab_prices.get(sym):
                try:
                    ab_prices[sym] = float(client.get_ticker_price(sym))
                except Exception:
                    ab_prices[sym] = 0.0
        # P0-C protocol: daily A/B isolation verification (Leo 2026-08-26).
        # Any anomaly is a hard stop signal that must surface in every scan.
        iso = verify_ab_isolation(db)
        if not iso["ok"]:
            report_lines.append("🚨 A/B 隔離穿窿（即刻停 B 組並回滾 main）:")
            for a in iso["anomalies"]:
                report_lines.append(f"   - {a}")
            report_lines.append("")
            logger.error(f"[P0-C] A/B ISOLATION BREACH: {iso['anomalies']}")
        report_lines.append(format_ab_report(db, TOTAL_CAPITAL, B_START_CASH, ab_prices))
        report_lines.append("")
        # one daily snapshot per scan day (idempotent UPSERT)
        try:
            snapshot_daily(db, ab_prices, TOTAL_CAPITAL, B_START_CASH)
        except Exception as e:
            logger.debug(f"AB snapshot failed: {e}")
    except Exception as e:
        logger.debug(f"AB report failed: {e}")

    report_lines.append(ct.format_report())

    if actions:
        report_lines.append("")
        report_lines.append(f"📋 本輪操作 ({len(actions)}):")
        for a in actions:
            sym = a.get("symbol", "?")
            act = a.get("action", "?")
            px = a.get("price", 0)
            qty = a.get("qty", 0)
            reason = a.get("reason", "")
            extra = f" SL=${a['sl']:.4f}" if "sl" in a and a["sl"] else ""
            extra += f" TP=${a['tp']:.4f}" if "tp" in a and a["tp"] else ""
            report_lines.append(f"   • {act} {sym} {qty:.6f} @ ${px:.4f}{extra} {reason}")

    if pause_alerts:
        report_lines.append("")
        report_lines.extend(pause_alerts)

    if dry_run:
        report_lines.append("\n⚠️ DRY RUN — no trades executed")

    report_text = "\n".join(report_lines)

    return {
        "regime": regime,
        "regime_state": state.to_dict(),
        "transition": transition,
        "btc_close": btc_close,
        "btc_sma200": btc_sma200,
        "btc_adx": btc_adx,
        "fng_avg": fng_avg,
        "actions": actions,
        "pause_alerts": pause_alerts,
        "status": current_status,
        "report": report_text,
        "whipsaw_count_7d": bull_neutral_swaps,
    }


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = run_paper_scan(dry_run=dry)
    print(result["report"])
    print()
    print(f"Regime: {result['regime']}")
    print(f"Actions: {len(result['actions'])}")
    if result['pause_alerts']:
        print(f"PAUSE ALERTS: {result['pause_alerts']}")
