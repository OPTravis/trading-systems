"""
BGeometrics (api.bitcoin-data.com) cross-process disk cache — bug#14.

Free tier: 10 req/h, 15 req/day, data (MVRV/SOPR/NUPL) updates ~daily.
Every hourly cron scan spawns a FRESH process, so the in-memory caches in
surge_detector (_BGE_CACHE) and dimension_scorer (_MVRV_CACHE) gave zero
cross-process reuse: dimension 1 + surge 3 = ~4 req/h ≈ 96 req/day against
a 15/day quota → guaranteed daily 429s (observed 8/19, 8/20 ×2, 8/21).

This module owns ONLY the disk layer (no HTTP here — each caller keeps its
own fetch logic so existing request-mocking tests stay valid):

- JSON file, default <project>/data/bgeometrics_cache.json
  (override via env BGE_CACHE_PATH; tests point this at tmp dirs)
- entries keyed by endpoint ("/mvrv", "/sopr", "/nupl"); dimension_scorer
  and surge_detector share the same "/mvrv" key
- atomic write via tmp+rename; readers tolerate missing/corrupt files
- every failure path degrades silently: cache I/O must never break trading

Freshness (TTL) is judged by the CALLER using its own constants, so entry
layout is intentionally minimal: {"value": float|None, "fetched_at": epoch}.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "bgeometrics_cache.json"


def _cache_path() -> Path:
    env = os.environ.get("BGE_CACHE_PATH", "").strip()
    return Path(env) if env else DEFAULT_PATH


def load_entry(endpoint: str) -> Optional[Dict[str, Any]]:
    """Return {"value": ..., "fetched_at": ...} for endpoint, or None.

    Missing file, corrupt JSON, or malformed entries all → None (caller
    falls through to its own fetch path).
    """
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        entry = data.get("entries", {}).get(endpoint)
        if (
            isinstance(entry, dict)
            and "fetched_at" in entry
            and "value" in entry
            and isinstance(entry["fetched_at"], (int, float))
        ):
            return {"value": entry["value"], "fetched_at": float(entry["fetched_at"])}
    except FileNotFoundError:
        pass
    except Exception as e:  # corrupt file etc.
        logger.debug(f"bge disk cache read failed ({endpoint}): {e}")
    return None


def save_entry(endpoint: str, value: Optional[float], fetched_at: Optional[float] = None) -> None:
    """Persist one endpoint entry atomically (tmp + rename).

    Best-effort: any I/O failure is logged at debug and swallowed — the
    in-memory cache still works for the rest of this process.
    """
    path = _cache_path()
    entries: Dict[str, Any] = {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", {})
        if not isinstance(entries, dict):
            entries = {}
    except Exception:
        entries = {}  # missing or corrupt → start fresh rather than fail

    entries[endpoint] = {
        "value": value,
        "fetched_at": fetched_at if fetched_at is not None else time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)  # atomic on POSIX
    except Exception as e:
        logger.debug(f"bge disk cache write failed ({endpoint}): {e}")
