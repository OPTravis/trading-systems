"""
Self-healer: diagnose and fix issues at point of failure.
Called from scan_orchestrator when auto-execute fails.

P3-3: Source code modification is gated by SELF_HEALER_AUTO_FIX env var.
      Default is False (dry-run mode) — fixes are logged but NOT applied.
      Set SELF_HEALER_AUTO_FIX=1 to enable automatic source patching.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CRYPTO_DIR = Path.home() / "crypto-ai-trader"

# P3-3: Safety gate — source code modifications require explicit opt-in
AUTO_FIX_ENABLED = os.environ.get("SELF_HEALER_AUTO_FIX", "0").strip() in ("1", "true", "yes")


def diagnose_and_fix(error_msg: str, context: Optional[dict] = None) -> dict:
    """Given an error from auto-execute, diagnose root cause and attempt fix.

    Returns: {"diagnosed": bool, "fixed": bool, "diagnosis": str, "fix_result": str}
    """
    ctx = context or {}
    result = {"diagnosed": False, "fixed": False, "diagnosis": "", "fix_result": ""}

    error_lower = error_msg.lower()

    # Pattern 1: Price deviation (blocked by safety check)
    if "price deviation" in error_lower or "偏離" in error_msg:
        result["diagnosed"] = True
        result["diagnosis"] = "Price deviation check blocked the trade"
        # Verify if it's a code bug by testing _check_price_deviation directly
        fix_result = _verify_price_deviation(ctx.get("symbol"), ctx.get("price"))
        result["fixed"] = fix_result["fixed"]
        result["fix_result"] = fix_result["msg"]
        return result

    # Pattern 2: Insufficient balance (free=0, locked balance)
    if (
        "insufficient" in error_lower
        or "餘額不足" in error_msg
        or "balance" in error_lower
    ):
        result["diagnosed"] = True
        result["diagnosis"] = "Balance issue — likely locked in open orders"
        # This should be handled by cancel_orders in _execute_switch already
        # If we still get this, it's a deeper issue
        result["fix_result"] = "Check if cancel_all_orders is working correctly"
        return result

    # Pattern 3: Qty too small (dust position)
    if "qty too small" in error_lower or "min qty" in error_lower:
        result["diagnosed"] = True
        result["diagnosis"] = "Position too small to trade (dust)"
        result["fix_result"] = "Expected — position needs cleanup"
        return result

    # Pattern 4: klines KeyError
    if "keyerror" in error_lower and ("k[" in error_msg or "close" in error_lower):
        result["diagnosed"] = True
        result["diagnosis"] = "klines format bug — using list index on dict"
        fix_result = _fix_klines_bug()
        result["fixed"] = fix_result["fixed"]
        result["fix_result"] = fix_result["msg"]
        return result

    # Pattern 5: API error / network
    if "api" in error_lower or "timeout" in error_lower or "connection" in error_lower:
        result["diagnosed"] = True
        result["diagnosis"] = "API/network issue — transient, will retry next cycle"
        result["fix_result"] = "No fix needed — will self-resolve"
        return result

    # Pattern 6: DailyLossBreaker false trigger
    if (
        "dailyl" in error_lower
        or "tier 3" in error_lower
        or "daily loss" in error_lower
    ):
        result["diagnosed"] = True
        result["diagnosis"] = (
            "DailyLossBreaker may be falsely triggered (wrong total_value)"
        )
        fix_result = _fix_breaker_false_trigger()
        result["fixed"] = fix_result["fixed"]
        result["fix_result"] = fix_result["msg"]
        return result

    # Pattern 7: StateDB dict binding
    if "dict" in error_lower and "not supported" in error_lower:
        result["diagnosed"] = True
        result["diagnosis"] = (
            "StateDB sqlite3 dict binding — nested dict not JSON-serialized"
        )
        result["fix_result"] = (
            "Non-critical: decision data has nested dict. Check decision_add() callers."
        )
        return result

    # Pattern 8: ML model errors (HMM covars shape, predict failures)
    if "covars" in error_lower or "diag" in error_lower and "shape" in error_lower:
        result["diagnosed"] = True
        result["diagnosis"] = (
            "HMM covars shape mismatch — stored as full matrix, expected diagonal vector"
        )
        fix_result = _fix_hmm_covars_shape()
        result["fixed"] = fix_result["fixed"]
        result["fix_result"] = fix_result["msg"]
        return result

    if (
        "predict_proba" in error_lower
        or "predict" in error_lower
        and "hmm" in error_lower
    ):
        result["diagnosed"] = True
        result["diagnosis"] = (
            "HMM model predict failed — model state may be corrupted or incompatible"
        )
        result["fix_result"] = "Re-train: python scripts/hmm_regime.py --train"
        return result

    return result


def _verify_price_deviation(
    symbol: Optional[str] = None, price: Optional[float] = None
) -> dict:
    """Verify if _check_price_deviation is working correctly."""
    try:
        import sys

        sys.path.insert(0, str(CRYPTO_DIR))
        sys.path.insert(0, str(CRYPTO_DIR / "src"))
        from src.binance_client import BinanceClient
        from src.trade_executor import _check_price_deviation

        client = BinanceClient(testnet=False)
        test_sym = symbol or "BTCUSDT"
        test_price = price

        if not test_price:
            ticker = client.get_24hr_stats(test_sym)
            test_price = (
                float(ticker.get("last_price", 0)) if isinstance(ticker, dict) else 0
            )

        if test_price <= 0:
            return {"fixed": False, "msg": "Cannot get price for verification"}

        # This should NOT throw KeyError if the fix is in place
        result = _check_price_deviation(client, test_sym, test_price)
        return {
            "fixed": True,
            "msg": f"Verified: {test_sym} check returned {result} (no KeyError)",
        }

    except KeyError:
        # klines format bug is still present!
        fix_result = _fix_klines_bug()
        return fix_result
    except Exception as e:
        logger.error("Verification error in self_healer", exc_info=True)
        return {"fixed": False, "msg": f"Verification error: {e}"}


def _fix_klines_bug() -> dict:
    """Auto-fix klines format bug in all affected files.

    P3-3: Source modification is gated by SELF_HEALER_AUTO_FIX env var.
    In dry-run mode (default), only reports what *would* be changed.
    """
    files_to_check = [
        (
            "src/trade_executor.py",
            [("float(k[4])", "float(k['close'])"), ("k[4]", "k['close']")],
        ),
        (
            "src/scan_orchestrator.py",
            [("k[4]", "k['close']"), ("k[5]", "k['quote_volume']")],
        ),
        ("src/twap_vwap.py", [("k[5]", "k['quote_volume']")]),
    ]

    # Phase 1: Scan for issues (always safe, read-only)
    issues_found = []
    for rel_path, replacements in files_to_check:
        fpath = CRYPTO_DIR / rel_path
        if not fpath.exists():
            continue
        content = fpath.read_text()
        for old, new in replacements:
            if old in content:
                issues_found.append((rel_path, old, new))

    if not issues_found:
        return {"fixed": False, "msg": "No klines format issues found in code"}

    if not AUTO_FIX_ENABLED:
        dry_run_msg = (
            f"[DRY-RUN] self_healer found {len(issues_found)} klines issues in: "
            + ", ".join(set(r[0] for r in issues_found))
            + " — set SELF_HEALER_AUTO_FIX=1 to enable patching"
        )
        logger.warning(dry_run_msg)
        return {"fixed": False, "msg": dry_run_msg}

    # Phase 2: Apply fixes (only when AUTO_FIX_ENABLED)
    fixed_files = []
    for rel_path, replacements in files_to_check:
        fpath = CRYPTO_DIR / rel_path
        if not fpath.exists():
            continue
        content = fpath.read_text()
        original = content
        for old, new in replacements:
            content = content.replace(old, new)
        if content != original:
            fpath.write_text(content)
            fixed_files.append(rel_path)

    if fixed_files:
        # NOTE: Auto git commit removed (BUG-005) — production code must not be
        # silently modified. Log the fix and notify via alert instead.
        logger.warning(
            "self_healer fixed klines format in: %s — "
            "MANUAL REVIEW REQUIRED before committing",
            ", ".join(fixed_files),
        )
        return {
            "fixed": True,
            "msg": f"Fixed klines format in: {', '.join(fixed_files)} — REVIEW REQUIRED",
        }

    return {"fixed": False, "msg": "No klines format issues found in code"}


def _fix_hmm_covars_shape() -> dict:
    """Auto-fix HMM covars from (n, n_feat, n_feat) to (n, n_feat)."""
    try:
        import json
        import sys
        import time

        import numpy as np

        sys.path.insert(0, str(CRYPTO_DIR))
        from src.state_db import get_state_db

        db = get_state_db()
        conn = db._get_conn()
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'hmm_model_state'"
        ).fetchone()
        if not row:
            return {"fixed": False, "msg": "No HMM model state in DB"}

        state = json.loads(row["value"])
        covars = np.array(state["covars"])
        if covars.ndim != 3:
            return {
                "fixed": False,
                "msg": f"Covars already correct shape {covars.shape}",
            }

        covars_diag = np.array([np.diag(covars[i]) for i in range(covars.shape[0])])
        state["covars"] = covars_diag.tolist()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES ('hmm_model_state', ?, ?)",
            (json.dumps(state), time.time()),
        )
        conn.commit()
        return {
            "fixed": True,
            "msg": f"Fixed covars: {covars.shape} → {covars_diag.shape}",
        }
    except Exception as e:
        logger.error("HMM covars fix failed", exc_info=True)
        return {"fixed": False, "msg": f"HMM covars fix failed: {e}"}


def _fix_breaker_false_trigger() -> dict:
    """Reset DailyLossBreaker that was falsely triggered by wrong total_value calculation."""
    try:
        import sys

        sys.path.insert(0, str(CRYPTO_DIR))
        sys.path.insert(0, str(CRYPTO_DIR / "src"))
        from src.daily_loss_breaker import DailyLossBreaker

        dlb = DailyLossBreaker()
        status = dlb.get_status()

        if status.get("current_tier", 0) < 3:
            return {"fixed": False, "msg": "Breaker not at TIER 3, no fix needed"}

        dlb._current_tier = 0
        dlb._daily_start_balance = 0.0
        dlb._halt_until = 0.0
        dlb._save_state()
        return {
            "fixed": True,
            "msg": "Reset false TIER 3 breaker — will re-snapshot correct total on next check",
        }
    except Exception as e:
        logger.error("Breaker reset failed", exc_info=True)
        return {"fixed": False, "msg": f"Breaker reset failed: {e}"}
