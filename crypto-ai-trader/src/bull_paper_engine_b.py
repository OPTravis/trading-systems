"""P0-C: B-variant paper engine (ATR-disciplined / R-multiple / multi-timeframe).

Runs on the SAME scan clock and the SAME candidate universe as the A baseline
(BullPaperEngine), but uses an independent $200 paper sleeve so the two are
directly comparable. Every filter decision is logged to paper_bull_filter_decisions
so we can attribute edge to individual gates after the 14-day run.

B rules (per Leo 2026-08-26 ruling):
  - SL: 2.0 * ATR(14, 4H), hard-floored at -8%, hard-ceilinged at -3%
  - TP: staged - 1R closes 1/3, 2R closes 1/3, Chandelier 3*ATR(22,1D) trails rest
  - Score threshold: 78 (vs A core ~60 / sat 60)
  - MTF: 4H EMA20 > EMA50 must align before a 1H/4H entry
  - ADX(14,4H) > 25 to open, < 20 blocks (hysteresis band 20<->25)
  - RVOL(20) > 1.2 confirms breakout participation
  - 8h post-entry "subjective SL" cooldown: ATR/EMA/Chandelier stops don't fire
    in the first 8h; only the hard -8% floor and thesis-level exits are active.

A stays untouched - baseline must be clean.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.bull_paper_engine import (
    BullPaperEngine,
    compute_indicators,
    _atr,
    FEE_RATE,
    SLIPPAGE,
)

logger = logging.getLogger(__name__)

# B-variant parameters
B_START_CASH = 200.0
B_SCORE_MIN = 78
B_SL_ATR_MULT = 2.0
B_SL_HARD_FLOOR = 0.08
B_SL_HARD_CEIL = 0.03
B_RVOL_MIN = 1.2
B_ADX_OPEN = 25.0
B_ADX_CLOSE = 20.0
B_COOLING_MS = 8 * 3600 * 1000
B_CHANDELIER_ATR_PERIOD = 22
B_CHANDELIER_MULT = 3.0
B_STAGE1_R = 1.0
B_STAGE1_FRAC = 1 / 3
B_STAGE2_R = 2.0
B_STAGE2_FRAC = 1 / 3
B_MAX_OPEN = 5
B_RISK_PER_TRADE = 0.02


def _rvol(klines: List[Dict], period: int = 20) -> Optional[float]:
    """Current bar volume / mean volume over prior `period` bars."""
    vols = []
    for k in klines:
        v = k.get("volume", k.get("qvol", 0))
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        vols.append(v)
    vols = [v for v in vols if v > 0]
    if len(vols) < period + 1:
        return None
    cur = vols[-1]
    base = sum(vols[-(period + 1):-1]) / period
    if base <= 0:
        return None
    return cur / base


def _daily_atr22(klines_1d: List[Dict]) -> Optional[float]:
    if len(klines_1d) < 25:
        return None
    highs = [float(k["high"]) for k in klines_1d]
    lows = [float(k["low"]) for k in klines_1d]
    closes = [float(k["close"]) for k in klines_1d]
    return _atr(highs, lows, closes, B_CHANDELIER_ATR_PERIOD)


class BullPaperEngineB(BullPaperEngine):
    """B variant. Inherits price/klines plumbing from A; overrides entry
    gating, risk sizing, and exits. Thesis-level exits (REGIME_EXIT /
    CLOSE_BELOW_EMA200) are applied via B's own method so they act on the
    B sleeve (self.portfolio resolves to group B)."""

    def __init__(self, db, client):
        super().__init__(db, client)
        self._b_portfolio = None
        self._adx_latched: Dict[str, bool] = {}

    @property
    def portfolio(self):
        if self._b_portfolio is None:
            from src.bull_paper_portfolio import BullPaperPortfolio
            self._b_portfolio = BullPaperPortfolio(
                self.db, start_cash=B_START_CASH, group="B"
            )
        return self._b_portfolio

    def _log_decision(self, symbol, decision, *, score=None, fail_filter="",
                      ind=None, rvol=None, r_multiple=None, regime="", notes=""):
        try:
            with self.db._get_conn() as conn:
                conn.execute(
                    """INSERT INTO paper_bull_filter_decisions
                       (scan_time, ab_group, symbol, score, decision, fail_filter,
                        atr14_4h, ema20_4h, ema50_4h, ema200_4h,
                        adx14_4h, rvol20, r_multiple, regime, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(time.time() * 1000), "B", symbol, score, decision,
                     fail_filter,
                     (ind or {}).get("atr"),
                     (ind or {}).get("ema20"), (ind or {}).get("ema50"),
                     (ind or {}).get("ema200"),
                     (ind or {}).get("adx"), rvol, r_multiple, regime, notes),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[B] filter-decision log failed {symbol}: {e}")

    def evaluate_b_entry(self, symbol, score, regime):
        """Return (ok, reason, context). Logs every candidate."""
        ctx: Dict[str, Any] = {}
        if regime not in ("CONFIRMED_BULL", "MILD_BULL"):
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="regime", regime=regime)
            return False, "regime_not_bull", ctx
        if score < B_SCORE_MIN:
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="score", regime=regime)
            return False, f"score_{score:.0f}<{B_SCORE_MIN}", ctx

        klines = self.get_klines(symbol, "4h", limit=250)
        ind = compute_indicators(klines)
        ctx["ind"] = ind
        if None in (ind.get("ema20"), ind.get("ema50"), ind.get("ema200"),
                    ind.get("atr"), ind.get("adx")):
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="insufficient_data", ind=ind, regime=regime)
            return False, "insufficient_data", ctx
        # P0-C review: explicit ATR sanity — 0 / NaN / out-of-range ATR must not
        # fall through to risk math (would produce divide-by-zero or insane SL)
        _atr_v = ind.get("atr") or 0
        try:
            _px0 = float(self.get_price(symbol))
        except Exception:
            _px0 = 0
        if not (_atr_v > 0) or _px0 <= 0 or _atr_v / _px0 > 0.25:
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="atr_sanity_check", ind=ind,
                               regime=regime, notes=f"atr={_atr_v},px={_px0}")
            return False, "atr_sanity_check", ctx

        adx = ind["adx"]
        if adx < B_ADX_CLOSE:
            self._adx_latched[symbol] = False
        if adx >= B_ADX_OPEN:
            self._adx_latched[symbol] = True
        adx_ok = self._adx_latched.get(symbol, False) or adx >= B_ADX_OPEN
        if not adx_ok:
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="adx", ind=ind, regime=regime)
            return False, f"adx_{adx:.1f}<{B_ADX_OPEN}", ctx

        if not (ind["ema20"] > ind["ema50"]):
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="mtf_ema20_below_ema50", ind=ind, regime=regime)
            return False, "mtf_ema_alignment", ctx

        rv = _rvol(klines, 20)
        ctx["rvol"] = rv
        if rv is None or rv < B_RVOL_MIN:
            self._log_decision(symbol, "reject", score=score,
                               fail_filter="rvol", ind=ind, rvol=rv, regime=regime)
            return False, f"rvol_{rv if rv is None else round(rv,2)}<{B_RVOL_MIN}", ctx

        atr = ind["atr"]
        px = self.get_price(symbol)
        raw_sl_dist = B_SL_ATR_MULT * atr
        sl_dist = max(raw_sl_dist, px * B_SL_HARD_CEIL)
        sl_dist = min(sl_dist, px * B_SL_HARD_FLOOR)
        r_multiple = sl_dist / px if px > 0 else 0
        ctx["sl_dist"] = sl_dist
        ctx["r_multiple"] = r_multiple
        ctx["price"] = px

        self._log_decision(symbol, "accept", score=score, ind=ind,
                           rvol=rv, r_multiple=r_multiple, regime=regime)
        return True, "all_criteria_met", ctx

    def try_b_entry(self, symbol, score, regime):
        held = self.portfolio.get_open_positions()
        if len(held) >= B_MAX_OPEN:
            return None
        if any(p["symbol"] == symbol for p in held):
            return None
        ok, reason, ctx = self.evaluate_b_entry(symbol, score, regime)
        if not ok:
            return None

        px = ctx["price"]
        sl_dist = ctx["sl_dist"]
        fill_px = px * (1 + SLIPPAGE)
        sl = fill_px - sl_dist
        equity = self._total_equity()
        risk_budget = equity * B_RISK_PER_TRADE
        qty = risk_budget / sl_dist if sl_dist > 0 else 0
        notional = qty * fill_px
        actual_risk_usd = qty * sl_dist  # what the SL actually risks
        if notional < 10:
            return None
        if notional > self.portfolio.cash * 0.95:
            qty = (self.portfolio.cash * 0.95) / fill_px
            notional = qty * fill_px
            actual_risk_usd = qty * sl_dist

        pos = self.portfolio.open_position(
            symbol, "core", qty, fill_px,
            stop_loss=sl, take_profit=0.0,
            atr_entry=ctx["ind"]["atr"], tier=1,
            fee_rate=FEE_RATE,
            notes=(f"B score={score:.0f},adx={ctx['ind']['adx']:.1f},"
                   f"atr={ctx['ind']['atr']:.4f},rvol={ctx['rvol']:.2f},"
                   f"r={ctx['r_multiple']:.3f},"
                   f"risk_usd={actual_risk_usd:.2f},notional={notional:.2f}"),
        )
        logger.info(f"[B] sizing {symbol}: risk_budget=${risk_budget:.2f} "
                    f"actual_risk=${actual_risk_usd:.2f} notional=${notional:.2f}")
        self._arm_scaleouts(pos.id, symbol, fill_px, sl_dist,
                            atr_entry=ctx["ind"]["atr"])
        return {"symbol": symbol, "action": "B_OPEN", "price": fill_px,
                "qty": qty, "sl": sl, "position_id": pos.id,
                "r_multiple": ctx["r_multiple"]}

    def _arm_scaleouts(self, position_id, symbol, entry, sl_dist, atr_entry=0.0):
        now = int(time.time() * 1000)
        rows = [
            (f"so_{uuid.uuid4().hex[:10]}", position_id, "B", symbol,
             1, B_STAGE1_R, B_STAGE1_FRAC, entry, atr_entry,
             entry + B_STAGE1_R * sl_dist, "pending", 0, 0.0, now),
            (f"so_{uuid.uuid4().hex[:10]}", position_id, "B", symbol,
             2, B_STAGE2_R, B_STAGE2_FRAC, entry, atr_entry,
             entry + B_STAGE2_R * sl_dist, "pending", 0, 0.0, now),
        ]
        with self.db._get_conn() as conn:
            conn.executemany(
                """INSERT INTO paper_bull_scaleouts
                   (id, position_id, ab_group, symbol, stage, r_multiple,
                    fraction, entry_price, atr_at_entry, trigger_price,
                    status, fired_time, fired_price, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
            conn.commit()

    def process_b_thesis_exits(self, regime, prices):
        """Thesis-level exits on B sleeve: regime drop and break of 4H EMA200.
        These void staged scale-outs and are NOT subject to the 8h cooldown."""
        events = []
        positions = self.portfolio.get_open_positions()
        for pos in positions:
            sym = pos["symbol"]
            px = prices.get(sym) or self.get_price(sym)
            if regime in ("NEUTRAL", "FEAR", "DEEP_BEAR"):
                self.portfolio.close_position(pos["id"], px, reason="B_REGIME_EXIT")
                self._void_scaleouts(pos["id"])
                events.append({"symbol": sym, "action": "CLOSE",
                               "reason": "B_REGIME_EXIT", "price": px,
                               "qty": pos["quantity"]})
                continue
            try:
                ind = compute_indicators(self.get_klines(sym, "4h", limit=250))
                if ind["ema200"] and px < ind["ema200"]:
                    self.portfolio.close_position(pos["id"], px, reason="B_BELOW_EMA200")
                    self._void_scaleouts(pos["id"])
                    events.append({"symbol": sym, "action": "CLOSE",
                                   "reason": "B_BELOW_EMA200", "price": px,
                                   "qty": pos["quantity"]})
            except Exception as e:
                logger.warning(f"[B] thesis-exit check failed {sym}: {e}")
        return events

    def process_b_exits(self, prices):
        """Hard floor always active; staged TP; ATR SL + Chandelier suppressed
        during 8h cooldown."""
        events = []
        now_ms = int(time.time() * 1000)
        positions = self.portfolio.get_open_positions()
        for pos in positions:
            sym = pos["symbol"]
            px = prices.get(sym) or self.get_price(sym)
            entry = pos["entry_price"]
            cooling = (now_ms - pos["entry_time"]) < B_COOLING_MS
            qty = pos["quantity"]

            if px <= entry * (1 - B_SL_HARD_FLOOR):
                self.portfolio.close_position(pos["id"], px, reason="B_HARD_FLOOR_8PCT")
                self._void_scaleouts(pos["id"])
                events.append({"symbol": sym, "action": "CLOSE",
                               "reason": "B_HARD_FLOOR_8PCT", "price": px, "qty": qty})
                continue

            self._process_scaleouts(pos, px)
            live = self._get_pos(pos["id"])
            if not live or live["status"] != "open" or live["quantity"] < 1e-12:
                continue

            if not cooling and pos["stop_loss"] > 0 and px <= pos["stop_loss"]:
                self.portfolio.close_position(pos["id"], px, reason="B_ATR_SL")
                self._void_scaleouts(pos["id"])
                events.append({"symbol": sym, "action": "CLOSE",
                               "reason": "B_ATR_SL", "price": px,
                               "qty": live["quantity"]})
                continue

            if not cooling:
                try:
                    kd = self.get_klines(sym, "1d", limit=40)
                    atr_d = _daily_atr22(kd)
                    if atr_d:
                        recent_high = max(float(k["high"]) for k in kd[-22:])
                        chandelier = recent_high - B_CHANDELIER_MULT * atr_d
                        if chandelier > pos["stop_loss"]:
                            self.portfolio.update_stops(pos["id"], stop_loss=chandelier)
                        if px <= chandelier:
                            self.portfolio.close_position(
                                pos["id"], px, reason="B_CHANDELIER_EXIT")
                            self._void_scaleouts(pos["id"])
                            events.append({"symbol": sym, "action": "CLOSE",
                                           "reason": "B_CHANDELIER_EXIT",
                                           "price": px, "qty": live["quantity"]})
                except Exception as e:
                    logger.warning(f"[B] chandelier check failed {sym}: {e}")
        return events

    def _get_pos(self, position_id):
        with self.db._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_bull_positions WHERE id=?", (position_id,)
            ).fetchone()
        return dict(row) if row else None

    def _process_scaleouts(self, pos, px):
        with self.db._get_conn() as conn:
            pendings = conn.execute(
                """SELECT * FROM paper_bull_scaleouts
                   WHERE position_id=? AND status='pending' ORDER BY stage""",
                (pos["id"],),
            ).fetchall()
        for so in pendings:
            if px >= so["trigger_price"]:
                close_qty = pos["quantity"] * so["fraction"]
                if close_qty * px < 5:
                    continue
                self.portfolio.close_position(
                    pos["id"], px, quantity=close_qty,
                    reason=f"B_TP_{so['stage']}R")
                with self.db._get_conn() as conn:
                    conn.execute(
                        """UPDATE paper_bull_scaleouts SET status='fired',
                           fired_time=?, fired_price=? WHERE id=?""",
                        (int(time.time() * 1000), px, so["id"]))
                    conn.commit()

    def _void_scaleouts(self, position_id):
        with self.db._get_conn() as conn:
            conn.execute(
                """UPDATE paper_bull_scaleouts SET status='voided'
                   WHERE position_id=? AND status='pending'""",
                (position_id,))
            conn.commit()
