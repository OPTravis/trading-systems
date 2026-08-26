"""
BULL Paper Trading Engine — live paper execution of the BULL override strategy.

Runs alongside the existing scan pipeline. Does NOT place real orders.
Tracks everything in paper_bull_* tables (fully isolated from live).

Core strategy parameters (from walk-forward backtest, proposal v2):
  CORE: 60% = $240, max 2 symbols, $120/slot, ATR 2.5x SL, 72h lock,
        pyramid +25% at +8%, EMA50 reduce 50%, EMA200 full close, hard -12%
  SATELLITE: 25% = $100, 3% Kelly cap ($12), 6% TP, ATR 2.0x SL,
             max hold 60 bars (10d), max 5 positions, score >=60
  CASH BUFFER: 15% = $60

Entry conditions (core):
  - Regime == CONFIRMED_BULL
  - Selected by scanner with score >=60 and ADX >25
  - Close > EMA20, EMA20>EMA50>EMA200 stack
  - Next 4H open fill with 0.05% slippage + 0.1% fee

Exit conditions (core):
  - Trailing SL (ATR 2.5x from entry, then trail above breakeven after +4%)
  - EMA50: close below EMA50 → reduce 50% (EMA50_REDUCE)
  - EMA200: close below EMA200 → close all (CLOSE_BELOW_EMA200)
  - Hard stop: -12% from entry (HARD_STOP_12PCT)
  - 72h minimum hold before any non-SL exit
  - Portfolio DD: -10% from BULL-entry peak

Regime demotion:
  - CONFIRMED_BULL → MILD_BULL: keep core, no new entries, no pyramids
  - CONFIRMED_BULL → NEUTRAL/FEAR/DEEP_BEAR: close all core
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Allocation ────────────────────────────────────────────────────────
TOTAL_CAPITAL = 400.0
CORE_ALLOCATION = 240.0
SATELLITE_ALLOCATION = 100.0
CASH_BUFFER = 60.0

CORE_PER_SLOT = 120.0
CORE_MAX_SYMBOLS = 2
CORE_PYRAMID_PCT = 0.25       # +25% additional at +8%
PYRAMID_TRIGGER_PCT = 0.08

# Cost model (same as backtest)
FEE_RATE = 0.001
SLIPPAGE = 0.0005
COST_PER_SIDE = FEE_RATE + SLIPPAGE  # 0.15%

# Core exit
ATR_PERIOD = 14
ATR_MULT = 2.5
MIN_HOLD_BARS = 18          # 72h at 4H
HARD_STOP_PCT = 0.12
REDUCE_EMA50_PCT = 0.50
PORTFOLIO_DD_STOP = 0.10
TRAIL_BREAKEVEN_TRIGGER = 0.04  # move SL to breakeven after +4%

# Satellite
SAT_KELLY_CAP = 0.03
SAT_MAX_POSITIONS = 5
SAT_TP_PCT = 0.06
SAT_SL_ATR_MULT = 2.0
SAT_MAX_HOLD_BARS = 60
SAT_SCORE_MIN = 60
SAT_MAX_TRADES_30D = 15

# EMA periods
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        trs.append(tr)
    # Wilder smoothing
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def _adx(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> Optional[float]:
    if len(closes) < period * 3:
        return None
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(tr)
    atr = sum(trs[:period]) / period
    pdi = sum(plus_dm[:period]) / period
    mdi = sum(minus_dm[:period]) / period
    dx_list = []
    for i in range(period, len(trs)):
        atr = (atr*(period-1)+trs[i])/period
        pdi = (pdi*(period-1)+plus_dm[i])/period
        mdi = (mdi*(period-1)+minus_dm[i])/period
        pDI = 100*pdi/atr if atr > 0 else 0
        mDI = 100*mdi/atr if atr > 0 else 0
        dx = 100*abs(pDI-mDI)/(pDI+mDI) if (pDI+mDI) > 0 else 0
        dx_list.append(dx)
    if len(dx_list) < period:
        return None
    return sum(dx_list[-period:]) / period


def compute_indicators(klines_4h: List[Dict]) -> Dict[str, Any]:
    """Compute EMA20/50/200, ATR, ADX from 4H klines."""
    closes = [float(k["close"]) for k in klines_4h]
    highs = [float(k["high"]) for k in klines_4h]
    lows = [float(k["low"]) for k in klines_4h]
    return {
        "close": closes[-1] if closes else None,
        "ema20": _ema(closes, EMA_FAST),
        "ema50": _ema(closes, EMA_MID),
        "ema200": _ema(closes, EMA_SLOW),
        "atr": _atr(highs, lows, closes, ATR_PERIOD),
        "adx": _adx(highs, lows, closes, ATR_PERIOD),
    }


class BullPaperEngine:
    """Live paper trading engine for BULL override strategy."""

    def __init__(self, db, client):
        self.db = db
        self.client = client
        self._portfolio = None

    @property
    def portfolio(self):
        if self._portfolio is None:
            from src.bull_paper_portfolio import BullPaperPortfolio
            self._portfolio = BullPaperPortfolio(self.db, start_cash=TOTAL_CAPITAL)
        return self._portfolio

    def get_klines(self, symbol: str, interval: str = "4h", limit: int = 250) -> List[Dict]:
        return self.client.get_klines(symbol, interval, limit=limit)

    def get_price(self, symbol: str) -> float:
        return float(self.client.get_ticker_price(symbol))

    def evaluate_core_entry(self, symbol: str, indicators: Dict) -> Tuple[bool, str]:
        """Check if a symbol meets core entry criteria.
        Returns (should_enter, reason)."""
        if None in (indicators["ema20"], indicators["ema50"],
                    indicators["ema200"], indicators["atr"], indicators["adx"]):
            return False, "insufficient_data"
        if indicators["adx"] < 25:
            return False, f"adx_{indicators['adx']:.1f}<25"
        if not (indicators["ema20"] > indicators["ema50"] > indicators["ema200"]):
            return False, "ema_stack_not_bullish"
        if indicators["close"] < indicators["ema20"]:
            return False, "close_below_ema20"
        return True, "all_criteria_met"

    def process_core_exits(self, regime: str, prices: Dict[str, float]) -> List[Dict]:
        """Check all open core positions for exit conditions.
        Returns list of exit events."""
        from src.bull_regime import STATE_EMOJI
        events = []
        positions = self.portfolio.get_open_positions(side="core")

        for pos in positions:
            sym = pos["symbol"]
            px = prices.get(sym, pos["entry_price"])
            entry = pos["entry_price"]
            qty = pos["quantity"]
            bars_held = self._bars_since(pos["entry_time"])
            sl = pos["stop_loss"]
            tp = pos["take_profit"]

            # If regime dropped to NEUTRAL/FEAR/DEEP_BEAR, close all
            if regime in ("NEUTRAL", "FEAR", "DEEP_BEAR") and bars_held >= 0:
                self.portfolio.close_position(pos["id"], px, reason="REGIME_EXIT")
                # P0-A6: core thesis invalidated — close same-symbol satellite too
                _sat_closed = self.portfolio.close_satellites_for_symbol(sym, px, reason="CORE_REGIME_EXIT")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "REGIME_EXIT",
                               "price": px, "qty": qty, "sat_closed": _sat_closed})
                continue

            # Hard stop -12% (always active)
            if px <= entry * (1 - HARD_STOP_PCT):
                self.portfolio.close_position(pos["id"], px, reason="HARD_STOP_12PCT")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "HARD_STOP_12PCT",
                               "price": px, "qty": qty})
                continue

            # SL hit
            if sl > 0 and px <= sl:
                self.portfolio.close_position(pos["id"], px, reason="SL_HIT")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "SL_HIT",
                               "price": px, "qty": qty})
                continue

            # TP hit
            if tp > 0 and px >= tp:
                self.portfolio.close_position(pos["id"], px, reason="TP_HIT")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "TP_HIT",
                               "price": px, "qty": qty})
                continue

            # Only check EMA-based exits after minimum hold
            if bars_held >= MIN_HOLD_BARS:
                klines = self.get_klines(sym, "4h", limit=250)
                ind = compute_indicators(klines)
                if ind["ema200"] and px < ind["ema200"]:
                    self.portfolio.close_position(pos["id"], px, reason="CLOSE_BELOW_EMA200")
                    _sat_closed = self.portfolio.close_satellites_for_symbol(sym, px, reason="CORE_BELOW_EMA200")
                    events.append({"symbol": sym, "action": "CLOSE",
                                   "reason": "CLOSE_BELOW_EMA200", "price": px, "qty": qty,
                                   "sat_closed": _sat_closed})
                    continue
                if ind["ema50"] and px < ind["ema50"]:
                    # Reduce 50%
                    close_qty = qty * REDUCE_EMA50_PCT
                    if close_qty * px > 5:  # minimum notional
                        self.portfolio.close_position(
                            pos["id"], px, quantity=close_qty, reason="EMA50_REDUCE"
                        )
                        events.append({"symbol": sym, "action": "REDUCE_50",
                                       "reason": "EMA50_REDUCE", "price": px, "qty": close_qty})
                    continue

            # Trailing SL update
            self._update_trailing_sl(pos, px, entry)

        return events

    def process_satellite_exits(self, prices: Dict[str, float]) -> List[Dict]:
        """Check satellite positions for TP/SL/max-hold."""
        events = []
        positions = self.portfolio.get_open_positions(side="satellite")
        for pos in positions:
            sym = pos["symbol"]
            px = prices.get(sym, pos["entry_price"])
            entry = pos["entry_price"]
            qty = pos["quantity"]
            bars_held = self._bars_since(pos["entry_time"])

            # TP +6%
            if px >= entry * (1 + SAT_TP_PCT):
                self.portfolio.close_position(pos["id"], px, reason="SAT_TP_6PCT")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "SAT_TP_6PCT",
                               "price": px, "qty": qty})
                continue
            # SL
            if pos["stop_loss"] > 0 and px <= pos["stop_loss"]:
                self.portfolio.close_position(pos["id"], px, reason="SAT_SL")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "SAT_SL",
                               "price": px, "qty": qty})
                continue
            # Max hold
            if bars_held >= SAT_MAX_HOLD_BARS:
                self.portfolio.close_position(pos["id"], px, reason="SAT_MAX_HOLD")
                events.append({"symbol": sym, "action": "CLOSE", "reason": "SAT_MAX_HOLD",
                               "price": px, "qty": qty})
                continue
            # Trailing update
            self._update_sat_trailing(pos, px)
        return events

    def try_core_entry(self, symbol: str, score: float, regime: str) -> Optional[Dict]:
        """Attempt to open a core position if conditions met."""
        if regime != "CONFIRMED_BULL":
            return None
        held = self.portfolio.get_open_positions(side="core")
        if len(held) >= CORE_MAX_SYMBOLS:
            return None
        if any(p["symbol"] == symbol for p in held):
            return None
        if self.portfolio.cash < CORE_PER_SLOT * 0.9:
            return None

        klines = self.get_klines(symbol, "4h", limit=250)
        ind = compute_indicators(klines)
        ok, reason = self.evaluate_core_entry(symbol, ind)
        if not ok:
            return None

        px = self.get_price(symbol)
        # Apply slippage for simulated fill
        fill_px = px * (1 + SLIPPAGE)
        atr = ind["atr"]
        sl = fill_px - ATR_MULT * atr
        # No fixed TP for core (trailing only, hard stop at -12%)
        tp = 0.0

        qty = CORE_PER_SLOT / fill_px
        pos = self.portfolio.open_position(
            symbol, "core", qty, fill_px,
            stop_loss=sl, take_profit=tp,
            atr_entry=atr, tier=1,
            fee_rate=FEE_RATE,
            notes=f"score={score:.0f},adx={ind['adx']:.1f},atr={atr:.4f}",
        )
        return {
            "symbol": symbol, "action": "OPEN_CORE", "price": fill_px,
            "qty": qty, "sl": sl, "atr": atr, "adx": ind["adx"],
            "position_id": pos.id,
        }

    def try_satellite_entry(self, symbol: str, score: float, regime: str) -> Optional[Dict]:
        """Attempt to open a satellite position."""
        if regime not in ("CONFIRMED_BULL", "MILD_BULL"):
            return None
        if score < SAT_SCORE_MIN:
            return None
        held = self.portfolio.get_open_positions(side="satellite")
        if len(held) >= SAT_MAX_POSITIONS:
            return None
        if any(p["symbol"] == symbol for p in held):
            return None

        # 30-day trade count check
        import time as _t
        cutoff = int(_t.time() * 1000) - 30 * 86400_000
        trades = self.portfolio.get_trade_history(limit=50)
        recent_sat = [t for t in trades if t["side"] == "satellite"
                      and t["action"] == "SELL" and t["timestamp"] >= cutoff]
        if len(recent_sat) >= SAT_MAX_TRADES_30D:
            return None

        # Kelly cap: 3% of total portfolio
        current_val = self._total_equity()
        max_notional = current_val * SAT_KELLY_CAP
        if max_notional < 5:
            return None

        klines = self.get_klines(symbol, "4h", limit=250)
        ind = compute_indicators(klines)
        if ind["atr"] is None:
            return None

        px = self.get_price(symbol)
        fill_px = px * (1 + SLIPPAGE)
        qty = max_notional / fill_px
        sl = fill_px - SAT_SL_ATR_MULT * ind["atr"]
        tp = fill_px * (1 + SAT_TP_PCT)

        pos = self.portfolio.open_position(
            symbol, "satellite", qty, fill_px,
            stop_loss=sl, take_profit=tp,
            atr_entry=ind["atr"], tier=1,
            fee_rate=FEE_RATE,
            notes=f"score={score:.0f}",
        )
        return {
            "symbol": symbol, "action": "OPEN_SAT", "price": fill_px,
            "qty": qty, "sl": sl, "tp": tp,
            "position_id": pos.id,
        }

    def try_pyramid(self, prices: Dict[str, float], regime: str) -> List[Dict]:
        """Add pyramid lots to winning core positions (+8% from entry)."""
        if regime != "CONFIRMED_BULL":
            return []
        events = []
        positions = self.portfolio.get_open_positions(side="core")
        for pos in positions:
            sym = pos["symbol"]
            px = prices.get(sym)
            if not px:
                continue
            entry = pos["entry_price"]
            gain = (px - entry) / entry
            # Check if already pyramided (tier 2 exists)
            if pos["tier"] >= 2:
                continue
            if gain >= PYRAMID_TRIGGER_PCT:
                pyramid_usd = CORE_PER_SLOT * CORE_PYRAMID_PCT
                if self.portfolio.cash < pyramid_usd * 1.1:
                    continue
                fill_px = px * (1 + SLIPPAGE)
                qty = pyramid_usd / fill_px
                klines = self.get_klines(sym, "4h", limit=250)
                ind = compute_indicators(klines)
                sl = fill_px - ATR_MULT * (ind["atr"] or 0)
                new_pos = self.portfolio.open_position(
                    sym, "core", qty, fill_px,
                    stop_loss=sl, atr_entry=ind["atr"], tier=2,
                    fee_rate=FEE_RATE,
                    notes=f"pyramid@gain{gain:.1%}",
                )
                events.append({
                    "symbol": sym, "action": "PYRAMID", "price": fill_px,
                    "qty": qty, "sl": sl, "position_id": new_pos.id,
                })
        return events

    def _update_trailing_sl(self, pos: Dict, px: float, entry: float):
        """Move SL to breakeven after +4%, then trail ATR-based."""
        gain = (px - entry) / entry
        if gain < TRAIL_BREAKEVEN_TRIGGER:
            return
        # At least breakeven
        new_sl = max(pos["stop_sl"] if "stop_sl" in pos else pos.get("stop_loss", 0),
                      entry * 1.001)
        # Trail 2.5 ATR if we have recent ATR
        try:
            klines = self.get_klines(pos["symbol"], "4h", limit=200)
            ind = compute_indicators(klines)
            if ind["atr"]:
                trail_sl = px - ATR_MULT * ind["atr"]
                new_sl = max(new_sl, trail_sl)
        except Exception:
            pass
        if new_sl > pos.get("stop_loss", 0):
            self.portfolio.update_stops(pos["id"], stop_loss=new_sl)

    def _update_sat_trailing(self, pos: Dict, px: float):
        """Satellite trailing: lock profits after +3%."""
        entry = pos["entry_price"]
        gain = (px - entry) / entry
        if gain < 0.03:
            return
        new_sl = max(pos.get("stop_loss", 0), entry * 1.005)
        if new_sl > pos.get("stop_loss", 0):
            self.portfolio.update_stops(pos["id"], stop_loss=new_sl)

    def _bars_since(self, entry_ts_ms: int) -> int:
        """Estimate number of 4H bars since entry."""
        now_ms = int(time.time() * 1000)
        return max(0, (now_ms - entry_ts_ms) // (4 * 3600 * 1000))

    def _total_equity(self) -> float:
        """Quick equity estimate (cash + open positions at last price)."""
        positions = self.portfolio.get_open_positions()
        mv = 0.0
        for p in positions:
            try:
                px = self.get_price(p["symbol"])
                mv += p["quantity"] * px
            except Exception:
                mv += p["quantity"] * p["entry_price"]
        return self.portfolio.cash + mv

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive paper trading status for reports."""
        positions = self.portfolio.get_open_positions()
        prices = {}
        for p in positions:
            try:
                prices[p["symbol"]] = self.get_price(p["symbol"])
            except Exception:
                prices[p["symbol"]] = p["entry_price"]

        val = self.portfolio.portfolio_value(prices)
        trades = self.portfolio.get_trade_history(limit=200)

        # EMA50_REDUCE stats
        ema50_reduces = [t for t in trades if "EMA50_REDUCE" in (t.get("details") or "")]
        core_sells = [t for t in trades if t["side"] == "core" and t["action"] == "SELL"]
        ema50_rate = len(ema50_reduces) / max(len(core_sells), 1)

        # Slippage tracking (we simulate at 0.05%, but track vs expectations)
        # In paper mode, slippage is baked into fill prices
        avg_slippage = SLIPPAGE  # simulated

        # Exit reason breakdown
        exit_reasons: Dict[str, int] = {}
        for t in trades:
            if t["action"] == "SELL":
                reason = t.get("details", "unknown")
                exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        # Core-satellite overlap tracking
        core_syms = {p["symbol"] for p in positions if p["side"] == "core"}
        sat_syms = {p["symbol"] for p in positions if p["side"] == "satellite"}
        overlap_syms = core_syms & sat_syms
        overlap_mv = 0.0
        for p in positions:
            if p["symbol"] in overlap_syms:
                overlap_mv += p["quantity"] * prices.get(p["symbol"], p["entry_price"])
        total_mv = val["market_value"]

        return {
            "portfolio": val,
            "positions": positions,
            "prices": prices,
            "cash": self.portfolio.cash,
            "total_value": val["total_value"],
            "total_return": val["total_return"],
            "unrealized_pnl": val["unrealized_pnl"],
            "core_count": val["core_count"],
            "sat_count": val["sat_count"],
            "ema50_reduce_count": len(ema50_reduces),
            "core_exit_count": len(core_sells),
            "ema50_rate": ema50_rate,
            "avg_slippage": avg_slippage,
            "exit_reasons": exit_reasons,
            "recent_trades": trades[:20],
            "overlap_symbols": list(overlap_syms),
            "overlap_notional": overlap_mv,
            "overlap_pct": overlap_mv / total_mv if total_mv > 0 else 0.0,
        }

    def format_report(self) -> str:
        """Format paper trading status for scan report."""
        s = self.get_status()
        lines = []
        lines.append("📋 BULL Paper Trading")
        lines.append(
            f"   權益: ${s['total_value']:.2f} (${s['cash']:.2f} cash + "
            f"${s['portfolio']['market_value']:.2f} positions) | "
            f"回報: {s['total_return']:+.2%}"
        )

        # Core positions
        core = [p for p in s["positions"] if p["side"] == "core"]
        if core:
            lines.append(f"   🎯 Core ({len(core)}/{CORE_MAX_SYMBOLS}):")
            for p in core:
                px = s["prices"].get(p["symbol"], p["entry_price"])
                pnl_pct = (px - p["entry_price"]) / p["entry_price"]
                bars = self._bars_since(p["entry_time"])
                lines.append(
                    f"     {p['symbol']:10s} {p['quantity']:.4f} @ ${p['entry_price']:.4f} → ${px:.4f} "
                    f"({pnl_pct:+.1%}) SL ${p['stop_loss']:.4f} | {bars} bars | tier{p['tier']}"
                )
        else:
            lines.append("   🎯 Core: (空倉)")

        # Satellite positions
        sat = [p for p in s["positions"] if p["side"] == "satellite"]
        if sat:
            lines.append(f"   🛰️ Satellite ({len(sat)}/{SAT_MAX_POSITIONS}):")
            for p in sat:
                px = s["prices"].get(p["symbol"], p["entry_price"])
                pnl_pct = (px - p["entry_price"]) / p["entry_price"]
                lines.append(
                    f"     {p['symbol']:10s} {p['quantity']:.4f} @ ${p['entry_price']:.4f} → ${px:.4f} "
                    f"({pnl_pct:+.1%}) SL ${p['stop_loss']:.4f} TP ${p['take_profit']:.4f}"
                )

        # Monitoring metrics
        lines.append(
            f"   📊 EMA50減半: {s['ema50_reduce_count']}/{s['core_exit_count']} "
            f"({s['ema50_rate']:.0%}) | 模擬滑價: {s['avg_slippage']*100:.2f}%"
        )
        if s.get("overlap_symbols"):
            lines.append(
                f"   🔗 Core-Sat重疊: {', '.join(s['overlap_symbols'])} "
                f"(${s['overlap_notional']:.2f}, {s['overlap_pct']:.1%} of positions)"
            )
        if s["exit_reasons"]:
            reasons_str = ", ".join(f"{k}:{v}" for k, v in s["exit_reasons"].items())
            lines.append(f"   📤 出場原因: {reasons_str}")

        return "\n".join(lines)
