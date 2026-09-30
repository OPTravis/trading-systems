"""
WO-1001-2: Declarative pair-level protections (freqtrade-inspired four-pack).

StoplossGuard / MaxDrawdown / LowProfitPairs / CooldownPeriod + programmatic
pairlock. Design rules:

1.  **Pure core, zero IO** — evaluate_locks() consumes a normalized event
    stream (list of dicts) so backtests replay protection decisions exactly
    (see BacktestEngine apply_protections).
2.  **Locks only block NEW entries (BUY)** — they never block exits. A
    protection must not be able to trap capital in a falling position.
3.  **Fail-open** — any internal failure lets the trade proceed (logged);
    protections are an added layer, not a substitute for the OCO on-exchange
    stop.
4.  **Declarative** — config/protections.yaml; kv overrides are NOT used for
    these thresholds (they belong to the config SSOT hierarchy of WO-1001-4).

StateDB adapter (live path) reads ledger_events — the only table carrying
exit_reason (sl / tp / switch / reconciled; see WO-0928).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "protections.yaml"
)

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "stoploss_guard": {
        "enabled": True,
        "lookback_hours": 168,       # 7d
        "max_consecutive_sl": 3,     # last N closes all loss-exits -> lock
        "lock_hours": 48,
        # sl=live SL order, reconciled=OCO SL leg filled on exchange,
        # switch=portfolio forced rotation close at a loss.
        "sl_reasons": ["sl", "reconciled", "switch"],
    },
    "max_drawdown": {
        "enabled": True,
        "lookback_hours": 336,       # 14d
        "max_dd_usdt": 10.0,         # peak-to-trough cumulative pnl drop
        "lock_hours": 72,
    },
    "low_profit_pairs": {
        "enabled": True,
        "lookback_hours": 336,
        "min_trades": 3,             # need at least this many SELLs in window
        "min_profit_usdt": 0.0,      # sum(pnl) <= threshold -> lock
        "lock_hours": 96,
    },
    "cooldown_period": {
        "enabled": True,
        "minutes": 240,              # per-pair BUY cooldown after last BUY
    },
}

LOCK_KIND = "protections.lock"        # ledger_events audit kind (source field)
_KV_PREFIX = "prot:lock:"             # manual/programmatic pairlock kv keys


def _conn_of(db: Any):
    """StateDB (thread-local conns) | raw sqlite3 connection."""
    gc = getattr(db, "_get_conn", None)
    if callable(gc):
        return gc()
    return getattr(db, "_conn", getattr(db, "conn", db))



@dataclass
class Lock:
    symbol: str
    reason: str                       # stoploss_guard / max_drawdown /
                                      # low_profit_pairs / cooldown_period /
                                      # manual / programmatic
    until_ts: float
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def active(self) -> bool:
        return time.time() < self.until_ts if self.until_ts > 0 else True


# ── config ────────────────────────────────────────────────────────────

def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Layered config: DEFAULTS < yaml file (missing keys fall through)."""
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    cfg["enabled"] = True
    p = path or os.environ.get("PROTECTIONS_CONFIG", _CONFIG_PATH)
    try:
        import yaml
        with open(p) as f:
            user = yaml.safe_load(f) or {}
        cfg["enabled"] = bool(user.get("enabled", True))
        for section, vals in user.items():
            if section == "enabled" or not isinstance(vals, dict):
                continue
            if section in cfg:
                cfg[section].update(vals)
    except FileNotFoundError:
        pass  # pure defaults are valid
    except Exception as exc:  # corrupted yaml -> defaults + loud log
        logger.warning("protections: config load failed (%s) — using defaults", exc)
    return cfg


# ── pure core (backtest-replayable, zero IO) ──────────────────────────

def _sl_like(ev: Dict[str, Any], reasons: List[str]) -> bool:
    return ev.get("type") == "SELL" and ev.get("exit_reason") in reasons


def evaluate_locks(
    events: List[Dict[str, Any]],
    symbol: str,
    now: float,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Lock]:
    """Evaluate all four guards for one symbol over a normalized event stream.

    events: [{"symbol": str, "ts": float, "type": "BUY"|"SELL",
              "exit_reason": str|None, "pnl": float|None}, ...]
    Returns active Lock list (may be empty). Never raises on odd data.
    """
    if cfg is None:
        cfg = {k: dict(v) for k, v in DEFAULTS.items()}
        cfg["enabled"] = True
    if not cfg.get("enabled", True):
        return []

    evs = sorted(
        (e for e in events if e.get("symbol") == symbol),
        key=lambda e: e.get("ts", 0.0),
    )
    locks: List[Lock] = []

    # 1) StoplossGuard — trailing run of consecutive loss-exits in window
    sg = cfg["stoploss_guard"]
    if sg["enabled"]:
        win_s = now - sg["lookback_hours"] * 3600
        sells = [e for e in evs if _sl_like(e, sg["sl_reasons"]) and e["ts"] > win_s]
        # count the CONSECUTIVE trailing sl-likes: walk recent SELLs (any
        # reason) newest-first, stop at first non-sl close.
        recent_sells = [e for e in evs if e.get("type") == "SELL" and e["ts"] > win_s]
        run = 0
        for e in reversed(recent_sells):
            if e.get("exit_reason") in sg["sl_reasons"]:
                run += 1
            else:
                break
        if run >= sg["max_consecutive_sl"]:
            locks.append(Lock(
                symbol, "stoploss_guard", now + sg["lock_hours"] * 3600,
                f"{run} consecutive loss-exits in {sg['lookback_hours']}h window "
                f"(threshold {sg['max_consecutive_sl']})",
            ))

    # 2) MaxDrawdown — peak-to-trough drop of cumulative realized pnl
    md = cfg["max_drawdown"]
    if md["enabled"]:
        win_s = now - md["lookback_hours"] * 3600
        pnls = [float(e.get("pnl") or 0.0) for e in evs
                if e.get("type") == "SELL" and e["ts"] > win_s]
        cum = peak = 0.0
        dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        if dd >= md["max_dd_usdt"]:
            locks.append(Lock(
                symbol, "max_drawdown", now + md["lock_hours"] * 3600,
                f"pair drawdown {dd:.2f} USDT >= {md['max_dd_usdt']} in "
                f"{md['lookback_hours']}h window",
            ))

    # 3) LowProfitPairs — enough trades, still no profit
    lp = cfg["low_profit_pairs"]
    if lp["enabled"]:
        win_s = now - lp["lookback_hours"] * 3600
        sell_pnls = [float(e.get("pnl") or 0.0) for e in evs
                     if e.get("type") == "SELL" and e["ts"] > win_s]
        if len(sell_pnls) >= lp["min_trades"] and sum(sell_pnls) <= lp["min_profit_usdt"]:
            locks.append(Lock(
                symbol, "low_profit_pairs", now + lp["lock_hours"] * 3600,
                f"{len(sell_pnls)} closes, sum pnl {sum(sell_pnls):.2f} <= "
                f"{lp['min_profit_usdt']} in {lp['lookback_hours']}h window",
            ))

    # 4) CooldownPeriod — minimum spacing between BUYs of the same pair
    cd = cfg["cooldown_period"]
    if cd["enabled"]:
        buys = [e["ts"] for e in evs if e.get("type") == "BUY"]
        if buys and now - max(buys) < cd["minutes"] * 60:
            age_min = (now - max(buys)) / 60
            locks.append(Lock(
                symbol, "cooldown_period", max(buys) + cd["minutes"] * 60,
                f"last BUY {age_min:.0f}m ago < {cd['minutes']}m cooldown",
            ))

    return locks


# ── live adapter (StateDB) ────────────────────────────────────────────

def _load_live_events(db: Any, since_ts: float) -> List[Dict[str, Any]]:
    """ledger_events rows -> normalized event stream (oldest window needed)."""
    conn = _conn_of(db)
    rows = conn.execute(
        "SELECT ts, symbol, type, exit_reason, pnl FROM ledger_events "
        "WHERE ts > ? AND type IN ('BUY','SELL') ORDER BY ts",
        (since_ts,),
    ).fetchall()
    return [
        {"symbol": r[1], "ts": float(r[0]), "type": r[2],
         "exit_reason": r[3], "pnl": r[4]}
        for r in rows
    ]


def _static_lock(db: Any, symbol: str, now: float) -> Optional[Lock]:
    """Manual/programmatic pairlock persisted in kv (prot:lock:SYM)."""
    try:
        raw = db.kv_get(_KV_PREFIX + symbol)
        if not raw:
            return None
        d = json.loads(raw) if isinstance(raw, str) else raw
        until = float(d.get("until_ts", 0))
        if until and until <= now:  # expired -> self-clean
            db.kv_remove(_KV_PREFIX + symbol)
            return None
        return Lock(symbol, d.get("reason", "manual"), until,
                    d.get("detail", ""))
    except Exception:
        return None


def check_entry_allowed(
    db: Any, symbol: str, now: Optional[float] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[Lock]]:
    """Single entry gate for ALL buy paths (executor pretrade hook).

    Combines kv pairlocks + dynamic four-guard evaluation. Fail-open on any
    internal error — protections must never wedge the trade loop.
    """
    now = now if now is not None else time.time()
    try:
        if cfg is None:
            cfg = load_config()
        if not cfg.get("enabled", True):
            return True, None

        lock = _static_lock(db, symbol, now)
        if lock is not None:
            return False, lock

        horizon_h = max(
            cfg[s]["lookback_hours"] for s in
            ("stoploss_guard", "max_drawdown", "low_profit_pairs")
        )
        events = _load_live_events(db, now - horizon_h * 3600 - 60)
        for lk in evaluate_locks(events, symbol, now, cfg):
            return False, lk
        return True, None
    except Exception as exc:
        logger.warning("protections: check_entry_allowed failed for %s (%s) "
                       "— fail-open", symbol, exc)
        return True, None


# ── pairlock API (manual / programmatic locks with audit) ─────────────

def lock_pair(
    db: Any, symbol: str, reason: str, hours: float,
    source: str = "manual", detail: str = "",
) -> Lock:
    """Programmatic/manual pair lock — audited to ledger_events."""
    now = time.time()
    lock = Lock(symbol, reason, now + hours * 3600, detail or
                f"locked by {source} for {hours}h")
    db.kv_set(_KV_PREFIX + symbol, json.dumps(lock.as_dict()))
    _audit(db, "LOCK", lock)
    return lock


def unlock_pair(db: Any, symbol: str, source: str = "manual") -> bool:
    """Remove a kv pairlock (dynamic guards re-evaluate on next entry)."""
    key = _KV_PREFIX + symbol
    existed = db.kv_get(key) is not None
    db.kv_remove(key)
    if existed:
        _audit(db, "UNLOCK", Lock(symbol, source, 0.0, "manual unlock"))
    return existed


def _audit(db: Any, action: str, lock: Lock) -> None:
    try:
        conn = _conn_of(db)
        conn.execute(
            "INSERT INTO ledger_events (ts, type, symbol, qty, price, source, "
            "payload_json) VALUES (?,?,?,?,?,?,?)",
            (time.time(), "PROT_" + action, lock.symbol, 0.0, 0.0,
             LOCK_KIND, json.dumps(lock.as_dict())),
        )
        conn.commit()
    except Exception as exc:
        logger.warning("protections: audit write failed (%s)", exc)
