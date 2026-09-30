"""
WO-1001-4: configuration SSOT — single layered resolver.

Precedence (highest wins):
    DB kv override (cfg:KEY)  >  os.environ  >  config/settings.yaml  >  DEFAULTS

Design notes:
- os.environ keeps its historical priority during the migration window so
  existing deployments (crontab exported vars) do not flip behaviour the
  day this lands. Migration per key = add it to settings.yaml + swap the
  call site to cfg_get(); the env layer keeps working until the operator
  retires the exported var. No big-bang.
- Secrets (API keys/tokens) are NOT migrated into yaml/kv on purpose —
  they stay env-only. This store is for behaviour switches and tuning.
- kv overrides are cached with a short TTL (see _KV_TTL). Cross-process
  override changes may take up to _KV_TTL to propagate; scan cadence is
  10 min, so 30 s staleness is immaterial.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_YAML_PATH = os.environ.get("SETTINGS_YAML") or str(
    Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
)
_KV_PREFIX = "cfg:"
_KV_TTL = 30.0

# Typed code-level defaults (lowest layer). Keys used by call sites that
# have been migrated onto the store.
DEFAULTS: Dict[str, Any] = {
    "AUTO_EXECUTE": "false",
    "NEW_POSITIONS_HALTED": "1",
    "SL_RECONCILE_DRYRUN": "1",
    "TRADING_MODE": "spot",
    "ENABLE_FUTURES": "false",
    "USE_TESTNET": "false",
    "PAPER_INITIAL_BALANCE": "1000",
    "PAPER_FEE_RATE": "0.001",
    "PAPER_SLIPPAGE_PCT": "0.0005",
    "PAPER_MIN_ORDER_USDT": "5",
}

_lock = threading.Lock()
_yaml_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}
_kv_cache: Dict[str, Any] = {"ts": 0.0, "data": {}}


def _load_yaml_layer() -> Dict[str, Any]:
    now = time.time()
    with _lock:
        if now - _yaml_cache["ts"] < _KV_TTL:
            return _yaml_cache["data"]
    data: Dict[str, Any] = {}
    try:
        import yaml
        with open(_YAML_PATH) as f:
            y = yaml.safe_load(f) or {}
        flat = y.get("settings", y) if isinstance(y, dict) else {}
        if isinstance(flat, dict):
            data = {str(k).upper(): v for k, v in flat.items()}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("config_store: settings.yaml load failed (%s)", exc)
    with _lock:
        _yaml_cache["ts"] = now
        _yaml_cache["data"] = data
    return data


def _load_kv_layer() -> Dict[str, Any]:
    now = time.time()
    with _lock:
        if now - _kv_cache["ts"] < _KV_TTL:
            return _kv_cache["data"]
    data: Dict[str, Any] = {}
    try:
        from src.state_db import get_state_db
        db = get_state_db()
        for k, v in (db.kv_get_prefix(_KV_PREFIX) or {}).items():
            data[k[len(_KV_PREFIX):].upper()] = v
    except Exception as exc:
        logger.warning("config_store: kv layer load failed (%s) — env/yaml only", exc)
    with _lock:
        _kv_cache["ts"] = now
        _kv_cache["data"] = data
    return data


def cfg_get(key: str, default: Any = None) -> Any:
    """Resolve `key` through kv > env > yaml > DEFAULTS (then `default`)."""
    key = key.upper()
    kv = _load_kv_layer()
    if key in kv:
        return kv[key]
    env = os.environ.get(key)
    if env is not None:
        return env
    yml = _load_yaml_layer()
    if key in yml:
        return yml[key]
    return DEFAULTS.get(key, default)


def cfg_get_bool(key: str, default: bool = False) -> bool:
    v = cfg_get(key, default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def describe(key: str) -> Dict[str, Any]:
    """Audit view: value + source per layer (for ops/debugging)."""
    key = key.upper()
    kv = _load_kv_layer()
    yml = _load_yaml_layer()
    layers = {
        "kv": kv.get(key, None),
        "env": os.environ.get(key),
        "yaml": yml.get(key, None),
        "default": DEFAULTS.get(key, None),
    }
    source = next((s for s in ("kv", "env", "yaml", "default")
                   if layers[s] is not None), "fallback")
    return {"key": key, "value": cfg_get(key), "source": source, "layers": layers}


def set_override(key: str, value: Any) -> None:
    """Runtime override (persisted to kv, survives restarts)."""
    from src.state_db import get_state_db
    get_state_db().kv_set(_KV_PREFIX + key.upper(), value)
    with _lock:
        _kv_cache["ts"] = 0.0  # invalidate


def clear_override(key: str) -> bool:
    """Remove a kv override (falls back to env/yaml/default)."""
    from src.state_db import get_state_db
    db = get_state_db()
    k = _KV_PREFIX + key.upper()
    existed = db.kv_get(k) is not None
    db.kv_remove(k)
    with _lock:
        _kv_cache["ts"] = 0.0
    return existed


def invalidate_cache() -> None:
    """Test helper: drop both caches (conftest isolation friendly)."""
    with _lock:
        _kv_cache["ts"] = 0.0
        _yaml_cache["ts"] = 0.0
