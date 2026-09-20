"""Near-realtime structured event alerts → cloud-drive live_alerts/.

Order-4 (2026-09-20, Leo): SL/TP fills, switch executions, regime flips
and protection-order failures used to be invisible until the hourly
bridge report. Every emit() drops one small JSON file into ALERT_DIR on
the cloud drive — Travis can poll the directory and see key events
within one cron tick (10 min worst case, typically seconds).

Design rules:
- NEVER raises: an alerting failure must not break the trading path.
- Atomic write (tmp + os.replace) so a poller never sees partial JSON.
- Bounded retention: keeps the newest MAX_FILES alerts, prunes the rest.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Cloud drive is mounted on the trading host at this absolute path.
ALERT_DIR = Path("/Coze/Drive/Crypto_Trading_Monitor/live_alerts")

#: Bounded retention — beyond this count the oldest alerts are pruned.
MAX_FILES = 200


def emit(
    event_type: str,
    symbol: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Write one structured alert file. Returns True on success.

    Payload: {event_type, symbol, timestamp, timestamp_iso, details}.
    Filename: {epoch_ms}_{event_type}_{symbol}.json (lexicographically
    time-ordered, collision-safe at millisecond granularity).
    """
    now = time.time()
    payload = {
        "event_type": event_type,
        "symbol": symbol,
        "timestamp": round(now, 3),
        "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
        "details": details or {},
    }
    fname = f"{int(now * 1000)}_{event_type}_{symbol or 'NA'}.json"
    try:
        ALERT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ALERT_DIR / (fname + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, ALERT_DIR / fname)
        _prune()
        return True
    except Exception as e:  # noqa: BLE001 — alerting must never break flow
        logger.warning(
            "live_alerts: emit(%s, %s) failed (non-fatal): %s",
            event_type, symbol, e)
        return False


def _prune() -> None:
    """Keep only the newest MAX_FILES alert files (best-effort)."""
    try:
        files = sorted(
            (p for p in ALERT_DIR.iterdir() if p.suffix == ".json"),
            reverse=True,  # newest first (ms-prefixed names sort by time)
        )
        for stale in files[MAX_FILES:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass
