"""
WO-1046g (2026-10-13): S3 core/satellite layering — BTC core channel.

Spec: deliverables/WO-1046/20261012_WO-1046_机制级止盈重构_影响面分析与
分阶段方案.md Phase 3. Travis P3 authorization (2026-10-13 00:41): BTC
core_quota 0.40-0.50 guardrail.

BTC is already in the dynamic coin pool and scored by the 12-factor
scanner. The core layer does NOT add a new signal source — it adds a
PREFERENCE (when the regime is CONFIRMED_BULL and BTC itself passes the
threshold, prefer entering BTC over the natural best) plus a QUOTA
(core_quota = equity × core_allocation_pct, clamped to [0.40, 0.50]).

Config precedence (mirrors the WO-1046f flat_reset pattern):
    CORE_DEFAULTS < risk_limits.yaml `core_layer` < kv `core_layer_config`
kv_set("core_layer_config", {"enabled": false}) rolls the whole branch
back to pure satellite semantics (one-key rollback).

Safety invariants:
  * core_allocation_pct is ALWAYS hard-clamped to [min, max] — a kv or
    yaml typo cannot push the quota outside the spec guardrail.
  * Bear veto still applies to BTC: a vetoed core falls back to the
    natural best satellite candidate (core is a preference, not an
    override of risk gates).
  * Cash-reserve cap and total-exposure cap are NOT waived for core.
  * A missing/None `layer` value is always treated as satellite
    (backward compatible with pre-WO-1046g rows).
"""

import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CORE_DEFAULTS = {
    "enabled": True,
    "core_symbol": "BTCUSDT",
    "core_allocation_pct": 0.45,      # spec midpoint
    "core_allocation_pct_min": 0.40,  # spec guardrail lower bound
    "core_allocation_pct_max": 0.50,  # spec guardrail upper bound
    "core_max_pct": 50.0,             # core single-position cap (% equity)
    "core_sl_pct": 3.5,                # BTC-tight SL (ATR naturally small)
    "core_max_hold_hours": 168.0,     # 7d strategic hold (satellite: 48h)
    "core_position_scale": 1.5,       # kelly-fallback path scale
    "core_zombie_exit": False,       # strategic core skips zombie exit
}


def get_core_cfg() -> Dict:
    """Merge CORE_DEFAULTS <- risk_limits.yaml `core_layer` <- kv
    `core_layer_config`, then hard-clamp the allocation pct to the
    spec guardrail [core_allocation_pct_min, core_allocation_pct_max]."""
    cfg = dict(CORE_DEFAULTS)
    try:
        import yaml
        from pathlib import Path

        p = Path(__file__).parent.parent / "config" / "risk_limits.yaml"
        y = (yaml.safe_load(p.read_text()) or {}).get("core_layer") or {}
        if isinstance(y, dict):
            cfg.update({k: y[k] for k in cfg if k in y})
    except Exception:
        pass
    try:
        from src.state_db import get_state_db

        kvf = get_state_db().kv_get("core_layer_config") or {}
        if isinstance(kvf, dict):
            cfg.update({k: kvf[k] for k in cfg if k in kvf})
    except Exception:
        pass
    # Guardrail (spec): the allocation ALWAYS lives in [min, max].
    try:
        lo = float(cfg["core_allocation_pct_min"])
        hi = float(cfg["core_allocation_pct_max"])
        if lo > hi:
            raise ValueError("min > max")
        cfg["core_allocation_pct"] = min(
            max(float(cfg["core_allocation_pct"]), lo), hi
        )
    except Exception:
        cfg["core_allocation_pct"] = CORE_DEFAULTS["core_allocation_pct"]
        cfg["core_allocation_pct_min"] = CORE_DEFAULTS["core_allocation_pct_min"]
        cfg["core_allocation_pct_max"] = CORE_DEFAULTS["core_allocation_pct_max"]
    return cfg


def core_quota_pct() -> float:
    """Guardrail-clamped core allocation fraction (e.g. 0.45)."""
    return float(get_core_cfg()["core_allocation_pct"])


def core_active_holding(portfolio_positions: Optional[Dict]) -> Optional[str]:
    """Symbol of the currently-held CORE position, or None.

    One core position at a time (core_max_pct is sized for a single
    strategic slot). Missing/None layer values are satellite.
    """
    if not portfolio_positions:
        return None
    for sym, pos in portfolio_positions.items():
        pos = pos or {}
        try:
            if float(pos.get("quantity") or 0) > 0 and pos.get("layer") == "core":
                return sym
        except Exception:
            continue
    return None


def core_preflight(ctx, client, opportunities, dynamic_threshold) -> Tuple[bool, str]:
    """All-conditions-met check for activating the core channel this round.

    Returns (ok, reason). Any failure degrades to the pure satellite path —
    bit-identical to pre-WO-1046g behavior:
      1. core_layer enabled (kv/yaml)
      2. bull tier == CONFIRMED_BULL (ctx key from scan_phases, with a
         direct _check_btc_trend fallback)
      3. BTC itself clears the threshold in this scan round
      4. BTC not held (non-dust semantics of the duplicate-entry guard)
      5. core slot free (no active layer==core position)
    """
    cfg = get_core_cfg()
    if not cfg.get("enabled", False):
        return False, "core_layer disabled"
    core_symbol = cfg["core_symbol"]

    # (2) CONFIRMED_BULL regime
    tier = ctx.get("bull_tier")
    if not tier:
        try:
            from src.trade_executor import _check_btc_trend

            tier = (_check_btc_trend()[1] or {}).get("tier")
        except Exception:
            tier = None
    if tier != "CONFIRMED_BULL":
        return False, f"bull_tier={tier}"

    # (3) BTC itself over the line in this scan round
    btc_opp = None
    for o in opportunities or []:
        if o.get("symbol") == core_symbol:
            btc_opp = o
            break
    if btc_opp is None:
        return False, "BTC not in opportunities"
    if float(btc_opp.get("score") or 0) < float(dynamic_threshold):
        return False, f"BTC raw {btc_opp.get('score')} < threshold {dynamic_threshold}"

    # (4) not held
    from src.trade_executor import _symbol_already_held

    if _symbol_already_held(client, core_symbol):
        return False, "BTC already held"

    # (5) core slot free
    _pf = ctx.get("portfolio")
    held = None
    try:
        held = core_active_holding(getattr(_pf, "positions", None))
    except Exception:
        held = None
    if held:
        return False, f"core slot occupied by {held}"

    return True, (
        f"CONFIRMED_BULL + BTC {btc_opp.get('score')} >= {dynamic_threshold}"
    )
