"""P0-2 Defense Item 1: multi-stage circuit tiers (drawdown escalation).

Three tiers on **intraday drawdown** (equity vs UTC-day anchor equity):
  Tier 1  -6%  -> STOP_NEW          block new entries, keep existing positions
  Tier 2  -10% -> DELEVERAGE        compress open exposure to 40% of equity
  Tier 3  -15% -> LIQUIDATE_ALL     market-sell everything + cooldown +
                                   manual confirmation required to resume

Auto-release for Tiers 1/2 (ALL three conditions):
  a) drawdown recovered to within -3%
  b) hmm regime is not extreme (bear_trend / high_vol / bear / volatile)
  c) cooldown elapsed (>= 4h since trip)
Tier 3 never auto-releases — only manual_reset() clears it.

Design rules (work order 2026-09-21):
- SPOT ONLY, no leverage assumptions.
- Does NOT touch the existing risk main path (drawdown_breaker /
  daily_loss_breaker / circuit_breaker). Independent state machine hooked
  at the tail of the reconcile step.
- sell-side actions (T2/T3) are gated by kv CIRCUIT_TIERS_MODE:
    "report" (default) — evaluate + alert only, no market sells
    "act"              — execute deleverage / liquidation
  The new-entry gate (T1 STOP_NEW) is always enforced — blocking entries
  costs nothing irreversible, mirroring the dust_reaper rollout philosophy.
- Equity marks use live tickers; if any held symbol's price is missing the
  round is skipped (fail-open evaluation) to avoid phantom liquidations.
- Every tier transition emits a live_alerts JSON (event CIRCUIT_TIER).
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Tier thresholds on intraday drawdown (fraction, positive numbers).
TIER1_DD = 0.06
TIER2_DD = 0.10
TIER3_DD = 0.15

#: Drawdown must recover to within this before T1/T2 auto-release.
RELEASE_DD = 0.03

#: Minimum cooldown before a tier may auto-release (seconds).
COOLDOWN_S = 4 * 3600

#: Tier3 cooldown before manual reset is even accepted (seconds).
T3_COOLDOWN_S = 24 * 3600

#: Target open-exposure fraction of equity under Tier 2.
T2_TARGET_EXPOSURE = 0.40

#: hmm_regime values considered "extreme" for the release condition.
EXTREME_REGIMES = {"bear_trend", "high_vol", "bear", "volatile"}

KV_STATE = "circuit_tiers:state"
KV_MODE = "CIRCUIT_TIERS_MODE"
DEFAULT_MODE = "report"


# -- state persistence (kv, single JSON blob) ---------------------------------

def _load_state(db):
    raw = db.kv_get(KV_STATE) if db is not None else None
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _save_state(db, state):
    if db is None:
        return
    db.kv_set(KV_STATE, state)


def get_mode(db):
    mode = db.kv_get(KV_MODE) if db is not None else None
    return str(mode).strip().lower() if mode else DEFAULT_MODE


def manual_reset(db, note=""):
    """Human-only reset — the sole path out of Tier 3."""
    state = _load_state(db)
    prev = state.get("tier", 0)
    state.update({"tier": 0, "tripped_at": None, "reason": "manual_reset"})
    if note:
        state["note"] = note
    _save_state(db, state)
    from src.live_alerts import emit as emit_alert

    emit_alert("CIRCUIT_TIER", None, {
        "event": "manual_reset", "prev_tier": prev,
        "note": note, "ts": time.time(),
    })
    logger.info("circuit_tiers: manual reset (prev_tier=%s)", prev)
    return {"tier": 0, "action": "RESET", "prev_tier": prev}


# -- market data helpers ------------------------------------------------------

def _day_anchor(state, equity):
    """UTC-day anchored starting equity; rolls on first evaluation of a day."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    anchor_day = state.get("day")
    anchor_val = state.get("day_start_equity")
    if anchor_day != day or not anchor_val or anchor_val <= 0:
        state["day"] = day
        state["day_start_equity"] = equity
        return equity
    return float(anchor_val)


def _equity_and_positions(client, portfolio):
    """Cash + sum(qty * live price). Returns None when marks are unavailable."""
    db = getattr(portfolio, "_db", None)
    cash = 0.0
    if db is not None:
        try:
            cash = float(db.portfolio_get_cash_balance() or 0.0)
        except Exception:
            logger.warning("circuit_tiers: cash balance read failed",
                           exc_info=True)
            return None
    positions = portfolio.get_all_positions() or {}
    marks = []
    total_pos = 0.0
    for sym, pos in positions.items():
        qty = float(pos.get("qty") or pos.get("quantity") or 0.0)
        if qty <= 0:
            continue
        try:
            price = client.get_ticker_price(sym)
        except Exception:
            price = None
        if not price or price <= 0:
            logger.warning(
                "circuit_tiers: no live mark for %s — skipping round (fail-open)",
                sym)
            return None
        notional = qty * price
        total_pos += notional
        marks.append({
            "symbol": sym, "qty": qty, "price": price, "notional": notional,
        })
    marks.sort(key=lambda m: m["notional"], reverse=True)
    return {
        "cash": cash, "positions": marks, "open_notional": total_pos,
        "equity": cash + total_pos,
    }


def _regime_extreme(db):
    raw = db.kv_get("hmm_regime") if db is not None else None
    if not raw:
        return False
    if isinstance(raw, dict):
        raw = raw.get("regime") or raw.get("state") or ""
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        pass
    if isinstance(raw, dict):
        raw = raw.get("regime") or raw.get("state") or ""
    return str(raw).lower() in EXTREME_REGIMES


# -- actions ------------------------------------------------------------------

def _sell_notional(client, portfolio, mark, sell_notional):
    """Market-sell ~sell_notional worth of one position (dust_reaper style)."""
    qty = min(mark["qty"], sell_notional / mark["price"])
    if qty <= 0:
        return None
    try:
        order = client.place_order(
            symbol=mark["symbol"], side="SELL", order_type="MARKET",
            quantity=qty)
    except Exception:
        logger.error("circuit_tiers: sell failed for %s",
                     mark["symbol"], exc_info=True)
        return None
    if not order or not order.get("orderId"):
        return None
    fills = order.get("fills") or []
    if fills:
        fq = sum(float(f.get("qty", 0)) for f in fills)
        close_price = (
            sum(float(f.get("qty", 0)) * float(f.get("price", 0))
                for f in fills) / max(fq, 1e-12))
    else:
        close_price = mark["price"]
    try:
        portfolio.close_position(
            mark["symbol"], close_price=close_price,
            exit_reason="circuit_tiers_sell",
            client_order_id=str(order.get("clientOrderId")
                                or order.get("orderId")))
    except Exception:
        logger.error("circuit_tiers: close_position failed for %s",
                     mark["symbol"], exc_info=True)
    return {"symbol": mark["symbol"], "qty": qty, "price": close_price}


def _deleverage_to_target(client, portfolio, marks, target_notional):
    """Sell largest positions first until open notional <= target."""
    sold = []
    open_notional = sum(m["notional"] for m in marks)
    for mark in marks:
        if open_notional <= target_notional:
            break
        excess = open_notional - target_notional
        res = _sell_notional(client, portfolio, mark, excess)
        if res:
            sold.append(res)
            open_notional -= res["qty"] * res["price"]
    return sold


def _liquidate_all(client, portfolio, marks):
    sold = []
    for mark in marks:
        res = _sell_notional(client, portfolio, mark, mark["notional"])
        if res:
            sold.append(res)
    return sold


# -- evaluation ---------------------------------------------------------------

def evaluate_and_act(client, portfolio, log=None):
    """One tier evaluation round. Returns a summary dict (never raises)."""
    log = log or logger
    db = getattr(portfolio, "_db", None)
    if db is None:
        return {"tier": 0, "action": "NO_DB"}
    try:
        return _evaluate_inner(client, portfolio, db, log)
    except Exception:
        log.warning("circuit_tiers: evaluation failed (non-fatal)",
                    exc_info=True)
        return {"tier": -1, "action": "ERROR"}


def entry_blocked(db):
    """Gate for new entries. Returns a block reason or None.

    Tier >= 1 blocks new entries (STOP_NEW is enforced in every mode —
    skipping an entry is never irreversible).
    """
    state = _load_state(db)
    tier = int(state.get("tier") or 0)
    if tier >= 1:
        return "circuit_tiers tier %d (%s)" % (
            tier, state.get("reason", "unknown"))
    return None


def _evaluate_inner(client, portfolio, db, log):
    from src.live_alerts import emit as emit_alert

    state = _load_state(db)
    snap = _equity_and_positions(client, portfolio)
    if snap is None:
        return {"tier": int(state.get("tier") or 0),
                "action": "SKIP_NO_MARKS"}

    anchor = _day_anchor(state, snap["equity"])
    if anchor <= 0:
        return {"tier": 0, "action": "SKIP_BAD_ANCHOR"}
    dd = (snap["equity"] - anchor) / anchor  # negative on drawdown
    prev_tier = int(state.get("tier") or 0)
    extreme = _regime_extreme(db)
    mode = get_mode(db)

    # -- target tier from drawdown depth --
    if dd <= -TIER3_DD:
        target = 3
    elif dd <= -TIER2_DD:
        target = 2
    elif dd <= -TIER1_DD:
        target = 1
    else:
        target = 0

    # All tiers latch on trip — a tier may only leave via the auto-release
    # conditions below (T1/T2) or manual_reset (T3). Without latching, a
    # bounce from -7% to -5% would silently de-tier and ignore cooldown.
    new_tier = max(target, prev_tier)
    tripped_at = state.get("tripped_at")
    action = "HOLD"
    detail = {
        "dd_pct": round(dd * 100, 3), "anchor": round(anchor, 4),
        "equity": round(snap["equity"], 4), "mode": mode,
        "regime_extreme": extreme,
    }

    # -- auto-release check for tiers 1/2 --
    if new_tier in (1, 2):
        elapsed = (time.time() - tripped_at) if tripped_at else None
        released = (
            dd > -RELEASE_DD
            and not extreme
            and elapsed is not None
            and elapsed >= COOLDOWN_S
        )
        if released and target == 0:
            new_tier = 0
            tripped_at = None
            action = "RELEASE"
            detail["release"] = {
                "dd_pct": round(dd * 100, 3),
                "regime_extreme": extreme,
                "cooldown_elapsed_s": round(elapsed or 0),
            }
            emit_alert("CIRCUIT_TIER", None, {
                "event": "release", "prev_tier": prev_tier,
                "action": action, "detail": detail, "ts": time.time(),
            })
            log.info("circuit_tiers: released tier %s (dd %.2f%%, cooldown ok)",
                     prev_tier, dd * 100)

    # -- escalation / trip --
    if new_tier > prev_tier:
        tripped_at = time.time()
        action = {1: "STOP_NEW", 2: "DELEVERAGE",
                  3: "LIQUIDATE_ALL"}[new_tier]
        detail["event"] = "trip"
        emit_alert("CIRCUIT_TIER", None, {
            "event": "trip", "tier": new_tier, "prev_tier": prev_tier,
            "action": action, "detail": detail, "ts": time.time(),
        })
        log.warning("circuit_tiers: TRIP tier %s (dd %.2f%%, action %s, "
                    "mode %s)", new_tier, dd * 100, action, mode)

    # -- tier actions --
    if new_tier == 2 and action in ("DELEVERAGE", "HOLD"):
        target_notional = snap["equity"] * T2_TARGET_EXPOSURE
        detail["open_notional"] = round(snap["open_notional"], 4)
        detail["target_notional"] = round(target_notional, 4)
        if snap["open_notional"] > target_notional:
            if mode == "act":
                sold = _deleverage_to_target(
                    client, portfolio, snap["positions"], target_notional)
                detail["sold"] = sold
                if sold:
                    emit_alert("CIRCUIT_TIER", None, {
                        "event": "deleverage_executed", "tier": 2,
                        "sold": sold, "detail": detail, "ts": time.time(),
                    })
                    log.warning("circuit_tiers: deleverage sold %d "
                                "position(s)", len(sold))
            else:
                emit_alert("CIRCUIT_TIER", None, {
                    "event": "deleverage_needed_report_mode", "tier": 2,
                    "would_sell_notional": round(
                        snap["open_notional"] - target_notional, 4),
                    "detail": detail, "ts": time.time(),
                })
                log.warning(
                    "circuit_tiers: T2 deleverage needed but mode=report "
                    "(open %.2f > target %.2f)",
                    snap["open_notional"], target_notional)
                action = "DELEVERAGE_REPORTED"

    if new_tier == 3:
        detail["open_notional"] = round(snap["open_notional"], 4)
        if action == "LIQUIDATE_ALL":
            if mode == "act":
                sold = _liquidate_all(client, portfolio, snap["positions"])
                detail["sold"] = sold
                emit_alert("CIRCUIT_TIER", None, {
                    "event": "liquidate_all_executed", "tier": 3,
                    "sold": sold, "detail": detail, "ts": time.time(),
                })
                log.warning("circuit_tiers: T3 LIQUIDATE_ALL sold %d "
                            "position(s)", len(sold))
            else:
                emit_alert("CIRCUIT_TIER", None, {
                    "event": "liquidate_all_needed_report_mode", "tier": 3,
                    "open_notional": round(snap["open_notional"], 4),
                    "detail": detail, "ts": time.time(),
                })
                log.warning("circuit_tiers: T3 needed but mode=report — "
                            "positions kept, human decision pending")
                action = "LIQUIDATE_REPORTED"
        else:
            cooldown_left = max(
                0.0, T3_COOLDOWN_S - ((time.time() - tripped_at)
                                      if tripped_at else 0.0))
            detail["cooldown_left_s"] = round(cooldown_left)
            if cooldown_left <= 0:
                detail["note"] = "cooldown elapsed — manual_reset() pending"

    state.update({
        "tier": new_tier, "tripped_at": tripped_at,
        "reason": action if new_tier > 0 else state.get("reason"),
        "last_eval": time.time(),
    })
    _save_state(db, state)

    return {
        "tier": new_tier, "action": action, "prev_tier": prev_tier,
        "dd_pct": round(dd * 100, 3), "detail": detail,
    }
