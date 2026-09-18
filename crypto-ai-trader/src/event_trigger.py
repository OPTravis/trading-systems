"""Event-driven rapid-change trigger (Phase 1, Leo 2026-09-18 roadmap).

Market-respond layer on top of the schedule-driven scan stack: when a
rapid-change condition is met, the resident layer (reside_scan.py) fires a
full cron-scan immediately with EVENT_TRIGGER env set, and
scripts/scan_gate.py waives ONLY its time-gate — every in-scan risk check
(correlation / max-position / circuit breaker) runs unchanged.

Four trigger channels:
  (a) BTC rapid move     — |Δ| ≥ 1.5% vs prev sample (~10min) or ≥ 2.5% vs
                           prev-prev sample (~20min)
  (b) Holding rapid move — |Δ| ≥ 3.0% / 5.0% on any held symbol
  (c) HMM regime flip    — regime differs between two consecutive checks;
                           no debounce. Channel stays inactive (no trigger,
                           no error) while no trained HMM model exists in
                           the state DB — current live status.
  (d) Fill on a holding  — new Binance trade id observed on a held symbol
                           (covers OCO fills; the portfolio reconciler in
                           every cron-scan already re-lists TP/SL after a
                           fill, so (d)'s added value is faster capital
                           redeployment via the immediate full scan).

Sampling granularity: reside_scan is cron-driven every 10min (crontab
untouched, cost constraint), so the "5min/15min" framework legs are
realized as 10min/20min sample-pair windows. Thresholds were calibrated on
30-day |move| distributions (delivery record 20260918): each threshold sits
between the empirical p99.4 and p99.9+ of its window distribution —
~0.2-1.8 expected triggers/day per channel, further capped by debounce.

Debounce: non-regime channels fire at most once per 15min (state-file
timestamp). Regime flips are exempt.

State: JSON file (default /root/trading-state/event_trigger_state.json)
holding the rolling price samples, last non-regime trigger ts, last regime
and last seen trade ids per holding. All writes are atomic (tmp+rename).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BTC_SYMBOL = "BTCUSDT"

# --- Thresholds (%, per spec framework; calibrated 2026-09-18, see header) ---
BTC_THRESHOLDS = {"w1": 1.5, "w2": 2.5}    # (a) vs prev / prev-prev sample
HOLDING_THRESHOLDS = {"w1": 3.0, "w2": 5.0}  # (b)

WINDOW1_SEC = 10 * 60   # "5min leg" = previous sample (~10min ago)
WINDOW2_SEC = 20 * 60   # "15min leg" = sample before that (~20min ago)
SAMPLE_GRACE_SEC = 150  # accept a window anchor up to 2.5min off-nominal
HISTORY_SEC = 25 * 60   # retain samples covering both windows
DEBOUNCE_SEC = 15 * 60  # non-regime channels: max one trigger per 15min

STATE_FILE_DEFAULT = "/root/trading-state/event_trigger_state.json"

TRIGGER_BTC_MOVE = "BTC_MOVE"
TRIGGER_HOLDING_MOVE = "HOLDING_MOVE"
TRIGGER_REGIME_FLIP = "REGIME_FLIP"
TRIGGER_FILL = "FILL"


class EventTriggerEngine:
    """Pure-logic rapid-change detector with JSON-file persistence.

    The engine never touches the network: callers feed it a price snapshot,
    an optional regime label and per-holding max trade ids; it appends the
    snapshot to its rolling history, evaluates the four channels and
    returns the winning trigger (or None). Debounce state updates only on
    an actual trigger so an untriggered tick never silences a later one.
    """

    def __init__(self, state_file: Optional[str] = None):
        self.state_file = state_file or STATE_FILE_DEFAULT
        self.price_history: Dict[str, List[List[float]]] = {}
        self.last_nonregime_trigger_ts: float = 0.0
        self.last_regime: Optional[str] = None
        self.last_trade_ids: Dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            self.price_history = data.get("price_history", {})
            self.last_nonregime_trigger_ts = float(
                data.get("last_nonregime_trigger_ts", 0.0))
            self.last_regime = data.get("last_regime")
            self.last_trade_ids = {
                k: int(v) for k, v in data.get("last_trade_ids", {}).items()}
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            logger.info("event_trigger: no usable state file, cold start")

    def save(self) -> None:
        data = {
            "price_history": self.price_history,
            "last_nonregime_trigger_ts": self.last_nonregime_trigger_ts,
            "last_regime": self.last_regime,
            "last_trade_ids": self.last_trade_ids,
        }
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except OSError:
            logger.warning("event_trigger: state save failed (non-fatal)",
                           exc_info=True)

    # ---------------------------------------------------------------- helpers
    def _append_prices(self, now: float, prices: Dict[str, float]) -> None:
        for sym, px in (prices or {}).items():
            self.price_history.setdefault(sym, []).append([now, float(px)])
        cutoff = now - HISTORY_SEC
        for sym in self.price_history:
            self.price_history[sym] = [
                s for s in self.price_history[sym] if s[0] >= cutoff]

    def _window_move(self, symbol: str, now: float,
                     window_sec: int) -> Optional[float]:
        """|%Δ| between the newest sample and the oldest in-window anchor."""
        hist = self.price_history.get(symbol)
        if not hist or len(hist) < 2:
            return None
        newest_ts, newest_px = hist[-1]
        anchor_from = now - window_sec - SAMPLE_GRACE_SEC
        anchor_to = now - window_sec + SAMPLE_GRACE_SEC
        for ts, px in hist[:-1]:
            if anchor_from <= ts <= anchor_to:
                if px <= 0:
                    return None
                return abs(newest_px - px) / px * 100
        return None

    def _debounced(self, now: float) -> bool:
        if self.last_nonregime_trigger_ts <= 0:
            return False  # never triggered
        return (now - self.last_nonregime_trigger_ts) < DEBOUNCE_SEC

    # ------------------------------------------------------------------ check
    def record_and_check(
        self,
        now: float,
        prices: Dict[str, float],
        holdings: Optional[List[str]] = None,
        regime_now: Optional[str] = None,
        trade_ids_now: Optional[Dict[str, int]] = None,
    ) -> Optional[dict]:
        """Append snapshot, evaluate all channels, persist state.

        Returns a trigger dict {"type", "symbol", "pct", "window_sec",
        "detail"} for the winning channel or None. Price channel takes
        precedence over fill; regime flips are exempt from debounce.
        """
        holdings = sorted(set(holdings or []))
        self._append_prices(now, prices)

        candidates: List[dict] = []

        # (a) BTC rapid move — always evaluated, BTC must be in prices
        for wkey, wsec in (("w1", WINDOW1_SEC), ("w2", WINDOW2_SEC)):
            move = self._window_move(BTC_SYMBOL, now, wsec)
            if move is not None and move >= BTC_THRESHOLDS[wkey]:
                candidates.append({
                    "type": TRIGGER_BTC_MOVE, "symbol": BTC_SYMBOL,
                    "pct": round(move, 2), "window_sec": wsec,
                    "detail": f"{move:.2f}%/{wsec // 60}m≥{BTC_THRESHOLDS[wkey]}%",
                })

        # (b) any holding rapid move
        for sym in holdings:
            if sym == BTC_SYMBOL:
                continue
            for wkey, wsec in (("w1", WINDOW1_SEC), ("w2", WINDOW2_SEC)):
                move = self._window_move(sym, now, wsec)
                if move is not None and move >= HOLDING_THRESHOLDS[wkey]:
                    candidates.append({
                        "type": TRIGGER_HOLDING_MOVE, "symbol": sym,
                        "pct": round(move, 2), "window_sec": wsec,
                        "detail": f"{move:.2f}%/{wsec // 60}m≥{HOLDING_THRESHOLDS[wkey]}%",
                    })

        # (c) HMM regime flip — exempt from debounce; inactive on None
        regime_flip = None
        if regime_now is not None and self.last_regime is not None \
                and regime_now != self.last_regime:
            regime_flip = {
                "type": TRIGGER_REGIME_FLIP, "symbol": BTC_SYMBOL,
                "pct": 0.0, "window_sec": 0,
                "detail": f"{self.last_regime}->{regime_now}",
            }

        # (d) fill on a holding (covers OCO fills); cold start only seeds
        fill = None
        for sym, tid in (trade_ids_now or {}).items():
            known = self.last_trade_ids.get(sym)
            if known is not None and tid > known:
                fill = fill or {
                    "type": TRIGGER_FILL, "symbol": sym,
                    "pct": 0.0, "window_sec": 0,
                    "detail": f"trade_id {known}->{tid}",
                }

        # Pick winner: regime flip (undebounced) > price > fill
        winner = None
        if regime_flip is not None:
            winner = regime_flip
        elif candidates:
            winner = max(candidates, key=lambda c: c["pct"])
        elif fill is not None:
            winner = fill

        if winner is not None and winner["type"] != TRIGGER_REGIME_FLIP:
            if self._debounced(now):
                logger.info(
                    "event_trigger: %s %s DEBOUNCED (last %.0fs ago) — %s",
                    winner["type"], winner["symbol"],
                    now - self.last_nonregime_trigger_ts, winner["detail"])
                winner = None
            else:
                self.last_nonregime_trigger_ts = now

        # persist evolving state regardless of outcome
        if regime_now is not None:
            self.last_regime = regime_now
        for sym, tid in (trade_ids_now or {}).items():
            self.last_trade_ids[sym] = int(tid)
        self.save()

        if winner is not None:
            logger.info("event_trigger: %s %s — %s", winner["type"],
                        winner["symbol"], winner["detail"])
        return winner


def bypass_env(reason: str) -> Dict[str, str]:
    """Env vars that make run_cron.sh's gate waive ONLY its time-gate."""
    return {"EVENT_TRIGGER": reason or "unspecified"}
