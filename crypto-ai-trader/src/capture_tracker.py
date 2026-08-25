"""
Capture Ratio Tracker — BTC B&H benchmark for BULL paper trading.

Per Phase 2 requirements (2026-08-25):
  - Capture ratio uses BTC buy-and-hold as primary benchmark
  - Quarterly rebalanced 60/25/15 is secondary/reference only

Tracks:
  - Paper portfolio start value and BTC price at start
  - Rolling BTC B&H return since start
  - Paper portfolio return
  - Capture ratio = paper_return / btc_bh_return
  - Sats per day, core/satellite attribution

Persisted in StateDB kv store under 'capture_tracker_state'.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KV_KEY = "capture_tracker_state"
COLD_START_NOTE = (
    "Capture tracker not initialised. "
    "Call initialise(paper_start_value, btc_price) when Phase 2 paper starts."
)


@dataclass
class CaptureSnapshot:
    ts: int
    paper_value: float
    btc_price: float
    paper_return: float      # fractional
    btc_bh_return: float     # fractional
    capture_ratio: float     # paper / btc_bh
    core_pnl: float = 0.0
    sat_pnl: float = 0.0
    note: str = ""


class CaptureTracker:
    """Track paper portfolio performance vs BTC B&H."""

    def __init__(self, db=None):
        self.db = db
        self._state: Optional[Dict[str, Any]] = None

    def _load(self) -> Dict[str, Any]:
        if self._state is not None:
            return self._state
        if self.db is not None:
            raw = self.db.kv_get(KV_KEY)
            if raw:
                try:
                    self._state = json.loads(raw)
                    return self._state
                except (json.JSONDecodeError, TypeError):
                    pass
        self._state = {
            "initialised": False,
            "start_ts": 0,
            "start_value": 0.0,
            "start_btc": 0.0,
            "snapshots": [],
        }
        return self._state

    def _save(self):
        if self.db is not None and self._state is not None:
            self.db.kv_set(KV_KEY, json.dumps(self._state))

    def initialise(self, paper_start_value: float, btc_price: float):
        """Call once when Phase 2 paper trading begins."""
        self._state = {
            "initialised": True,
            "start_ts": int(time.time() * 1000),
            "start_value": paper_start_value,
            "start_btc": btc_price,
            "snapshots": [],
        }
        self._save()
        logger.info(
            f"[CAPTURE] Initialised: paper=${paper_start_value:.2f}, "
            f"BTC=${btc_price:,.2f} at t={self._state['start_ts']}"
        )

    def reset(self):
        """Reset all tracking data."""
        self._state = None
        if self.db is not None:
            self.db.kv_remove(KV_KEY)

    def record(
        self,
        paper_value: float,
        btc_price: float,
        core_pnl: float = 0.0,
        sat_pnl: float = 0.0,
        note: str = "",
    ) -> Optional[CaptureSnapshot]:
        """Record a daily snapshot. Returns the snapshot or None if not initialised."""
        s = self._load()
        if not s.get("initialised"):
            logger.warning(f"[CAPTURE] {COLD_START_NOTE}")
            return None

        paper_ret = (paper_value - s["start_value"]) / s["start_value"] if s["start_value"] > 0 else 0.0
        btc_ret = (btc_price - s["start_btc"]) / s["start_btc"] if s["start_btc"] > 0 else 0.0
        capture = paper_ret / btc_ret if abs(btc_ret) > 0.001 else 0.0

        snap = CaptureSnapshot(
            ts=int(time.time() * 1000),
            paper_value=paper_value,
            btc_price=btc_price,
            paper_return=paper_ret,
            btc_bh_return=btc_ret,
            capture_ratio=capture,
            core_pnl=core_pnl,
            sat_pnl=sat_pnl,
            note=note,
        )

        s["snapshots"].append(asdict(snap))
        # Keep last 180 snapshots (~6 months at daily frequency)
        if len(s["snapshots"]) > 180:
            s["snapshots"] = s["snapshots"][-180:]
        self._save()
        return snap

    def current(self) -> Optional[Dict[str, Any]]:
        """Get latest snapshot and summary."""
        s = self._load()
        if not s.get("initialised") or not s["snapshots"]:
            return None
        latest = s["snapshots"][-1]
        elapsed_ms = latest["ts"] - s["start_ts"]
        days = elapsed_ms / 86_400_000

        # Daily returns for Sharpe-like metric
        paper_rets = []
        prev = s["start_value"]
        for snap in s["snapshots"]:
            r = (snap["paper_value"] - prev) / prev if prev > 0 else 0.0
            paper_rets.append(r)
            prev = snap["paper_value"]

        return {
            "initialised": True,
            "start_ts": s["start_ts"],
            "start_value": s["start_value"],
            "start_btc": s["start_btc"],
            "days_elapsed": round(days, 1),
            "latest": latest,
            "snapshot_count": len(s["snapshots"]),
        }

    def format_report(self) -> str:
        """Format capture ratio for daily report."""
        info = self.current()
        if info is None:
            return "📊 Capture Ratio: 未初始化（Phase 2 啟動後開始追踪）"

        l = info["latest"]
        days = info["days_elapsed"]
        cap = l["capture_ratio"]

        if l["btc_bh_return"] >= 0:
            direction = "上升"
            cap_quality = "良好" if cap >= 0.3 else ("偏低" if cap >= 0.1 else "極低")
        else:
            direction = "下跌"
            cap_quality = "防守出色" if cap > 1 else ("防守正常" if cap >= 0 else "防守失敗")

        return (
            f"📊 BTC Capture Ratio: {cap:.1%} ({cap_quality})\n"
            f"   Paper: ${l['paper_value']:.2f} ({l['paper_return']:+.2%}) | "
            f"BTC B&H: {l['btc_bh_return']:+.2%} ({direction})\n"
            f"   起始 ${info['start_value']:.0f} / BTC ${info['start_btc']:,.0f} | "
            f"已追踪 {days:.1f} 天"
        )
