"""
Ledger -- single bookkeeping entry point for trade fills (WO-0924 P2).

PROBLEM (z2 report, Phase 1 P2): four truth sources are each written by
scattered callers with NO shared transaction boundary:

    portfolio rows        <- portfolio.add_position / close_position,
                             reconciler drift cleanup, sync_from_binance
    trades rows           <- db.trade_add from 4+ call sites
                             (executor, close_position, reconciler, ...)
    trade_outcomes rows   <- record_entry / record_outcome (2 call sites)
    tp_sl_tracker (kv)    <- executor placement, guardian heal, demote

A crash mid-sequence leaves them partially updated -- the exact structure
behind the INJ tracker-remnant class of incidents. The Ledger contract:

    ONE fill event -> ONE SQLite transaction -> ALL FOUR sources updated
    (or NONE of them: BEGIN IMMEDIATE, rollback on any step failure).

MODES (kv key "ledger:mode"; kill switch, default "shadow"):
    off     -- record_fill() is a no-op. Full rollback lever.
    shadow  -- Ledger only OBSERVES: appends each fill to its own event log
               and shadow book. The four real sources keep being written by
               the legacy paths, UNTOUCHED. After each scan round,
               shadow_diff() replays the shadow book against the live
               tables and reports every mismatch. Zero trading-chain
               behavior change; any Ledger error is swallowed and logged
               (a bystander must never break the host path).
    primary -- record_fill() performs the single-transaction four-source
               write. NOT enabled in production this round: primary is for
               tests and the future cutover, which requires shadow running
               clean for the agreed window + Travis acceptance first.

LIMIT_MAKER PRICE LESSON (two production incidents): Binance LIMIT_MAKER
orders return stopPrice as the truthy STRING '0.00'. The fill price MUST
always come from the event's own `price` field -- never from an order
object's stopPrice, and never truthiness-tested against price fields.

Shadow bookkeeping notes:
  * bootstrap_shadow() snapshots the CURRENT live portfolio into the
    shadow book once (kv-guarded, idempotent), so the two books start
    aligned mid-flight. Events with no bootstrap yet are refused
    ("awaiting_bootstrap") to keep ordering sound.
  * record_fill hooks live next to the legacy writes (add_position /
    close_position / reconciler oco_fill booking) and fire only in
    shadow mode, post-success, never raising into the host path.
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keys / constants
# ---------------------------------------------------------------------------

MODE_KEY = "ledger:mode"
STATS_KEY = "ledger:shadow:stats"
BOOTSTRAP_TS_KEY = "ledger:shadow:bootstrap_ts"

VALID_MODES = ("off", "shadow", "primary")
DEFAULT_MODE = "shadow"

#: quantity below this is "no position" (matches portfolio dust semantics)
EPS_QTY = 1e-9
#: fuzzy trade-row matching tolerances (BUY rows have no client_order_id)
FUZZY_TS_WINDOW_S = 300.0
FUZZY_QTY_REL = 0.01
FUZZY_PRICE_REL = 0.005
#: a "pending" (legacy drift-cleanup lagging) mismatch older than this
#: many consecutive rounds is promoted to a true diff
PENDING_MAX_ROUNDS = 2


class LedgerError(Exception):
    """Raised only on the PRIMARY path: the atomic write failed and was
    rolled back. Shadow/observe paths never raise."""


# ---------------------------------------------------------------------------
# Mode flag (kv-backed, kill-switch)
# ---------------------------------------------------------------------------

def get_mode(db=None) -> str:
    """Current Ledger mode. Defaults to 'shadow' (safe observer)."""
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    mode = db.kv_get(MODE_KEY, DEFAULT_MODE)
    if mode not in VALID_MODES:
        logger.warning(
            "ledger: invalid mode %r in kv — falling back to %r",
            mode, DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return mode


def set_mode(db, mode: str) -> str:
    """Set the Ledger mode flag (rollback lever). Returns the new mode."""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid ledger mode {mode!r}; expected one of {VALID_MODES}")
    db.kv_set(MODE_KEY, mode)
    logger.warning("ledger: mode set to %r", mode)
    return mode


# ---------------------------------------------------------------------------
# Event ingress
# ---------------------------------------------------------------------------

def _norm_event(event: Dict) -> Dict:
    ev = dict(event or {})
    ev["type"] = str(ev.get("type", "")).upper()
    ev["symbol"] = str(ev.get("symbol", "")).replace("/", "")
    ev["order_id"] = ev.get("order_id") or None
    if ev["order_id"] is not None:
        ev["order_id"] = str(ev["order_id"])
    ev["qty"] = float(ev.get("qty") or 0)
    ev["price"] = float(ev.get("price") or 0)
    ev["ts"] = float(ev.get("ts") or time.time())
    ev["source"] = str(ev.get("source") or "unknown")
    return ev


def _valid(ev: Dict) -> Optional[str]:
    if ev["type"] not in ("BUY", "SELL"):
        return f"bad type {ev['type']!r}"
    if not ev["symbol"]:
        return "empty symbol"
    if ev["qty"] <= 0:
        return f"bad qty {ev['qty']}"
    if ev["price"] <= 0:
        return f"bad price {ev['price']}"
    return None


def record_fill(
    event: Dict,
    observe_only: bool = False,
    db=None,
    round_id: Optional[str] = None,
) -> Dict:
    """Single ingress for fill events.

    observe_only=True is what the legacy-path hooks use: it forces pure
    shadow behavior (no four-source writes) and can never raise, so the
    host trading path is never endangered regardless of mode.

    Returns a status dict; never raises in shadow/observe paths.
    On the primary path a failure raises LedgerError AFTER rollback.
    """
    ev = _norm_event(event)
    reason = _valid(ev)
    if reason:
        logger.warning("ledger: rejected event (%s): %s", reason, ev)
        return {"status": "rejected", "reason": reason}

    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()

    mode = get_mode(db)
    if mode == "off":
        return {"status": "off", "mode": mode}
    if observe_only and mode != "shadow":
        # observe hooks only live in shadow mode; in primary the refactored
        # callers own the writes -- a hook here would double-book.
        return {"status": "noop", "mode": mode}

    # Ingress idempotency: one order_id may be booked exactly once. This
    # mirrors trade_add's INSERT OR IGNORE dedup, but at the Ledger layer so
    # BOTH books (shadow book and, later, the four sources) stay exact.
    if ev["order_id"] is not None:
        try:
            row = db._get_conn().execute(
                "SELECT id FROM ledger_events WHERE order_id = ? LIMIT 1",
                (ev["order_id"],),
            ).fetchone()
        except Exception:
            row = None
        if row is not None:
            return {"status": "duplicate", "order_id": ev["order_id"]}

    if mode == "shadow":
        try:
            if db.kv_get(BOOTSTRAP_TS_KEY) is None:
                # Events must never land before the shadow book has a
                # baseline; otherwise bootstrap would double-count them.
                logger.warning(
                    "ledger: shadow event for %s refused (no bootstrap yet)",
                    ev["symbol"],
                )
                return {"status": "awaiting_bootstrap", "symbol": ev["symbol"]}
            _apply_shadow(db, ev, round_id)
            return {"status": "ok", "mode": "shadow"}
        except Exception:
            # Bystander rule: a shadow failure must never break the host.
            logger.warning("ledger: shadow event failed for %s", ev["symbol"],
                           exc_info=True)
            return {"status": "shadow_error", "symbol": ev["symbol"]}

    # mode == "primary"
    return _apply_primary(db, ev, round_id)


# ---------------------------------------------------------------------------
# Shadow book
# ---------------------------------------------------------------------------

def _apply_shadow(db, ev: Dict, round_id: Optional[str]) -> None:
    """Append the event + update the shadow book in ONE transaction."""
    payload = {
        k: ev.get(k)
        for k in ("entry_id", "exit_reason", "pnl", "full_close", "fees")
        if ev.get(k) is not None
    }
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO ledger_events
               (ts, type, symbol, qty, price, order_id, source, pnl,
                exit_reason, deduct_cash, payload_json, round_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ev["ts"], ev["type"], ev["symbol"], ev["qty"], ev["price"],
                ev["order_id"], ev["source"], ev.get("pnl"),
                ev.get("exit_reason"),
                1 if ev.get("deduct_cash") else 0,
                json.dumps(payload) if payload else None,
                round_id,
            ),
        )
        if ev["type"] == "BUY":
            row = conn.execute(
                "SELECT net_qty, cost_basis, cash_delta "
                "FROM ledger_shadow_positions WHERE symbol = ?",
                (ev["symbol"],),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO ledger_shadow_positions
                       (symbol, net_qty, avg_entry_price, cost_basis,
                        cash_delta, opened_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ev["symbol"], ev["qty"], ev["price"],
                        ev["qty"] * ev["price"],
                        -ev["qty"] * ev["price"] if ev.get("deduct_cash") else 0.0,
                        ev["ts"], ev["ts"],
                    ),
                )
            else:
                new_qty = float(row["net_qty"]) + ev["qty"]
                new_cost = float(row["cost_basis"]) + ev["qty"] * ev["price"]
                cash_delta = float(row["cash_delta"] or 0) + (
                    -ev["qty"] * ev["price"] if ev.get("deduct_cash") else 0.0
                )
                conn.execute(
                    """UPDATE ledger_shadow_positions
                       SET net_qty = ?, avg_entry_price = ?, cost_basis = ?,
                           cash_delta = ?, updated_at = ?
                       WHERE symbol = ?""",
                    (
                        new_qty,
                        (new_cost / new_qty) if new_qty > 0 else ev["price"],
                        new_cost, cash_delta, ev["ts"], ev["symbol"],
                    ),
                )
        else:  # SELL
            row = conn.execute(
                "SELECT net_qty FROM ledger_shadow_positions "
                "WHERE symbol = ?",
                (ev["symbol"],),
            ).fetchone()
            if row is None:
                # SELL for a position the shadow book never opened (e.g.
                # pre-bootstrap legacy). Record the event; the diff layer
                # reports the book divergence instead of guessing here.
                return
            new_qty = float(row["net_qty"]) - ev["qty"]
            proceeds = ev["qty"] * ev["price"]
            if new_qty <= EPS_QTY:
                conn.execute(
                    "DELETE FROM ledger_shadow_positions WHERE symbol = ?",
                    (ev["symbol"],),
                )
            else:
                conn.execute(
                    """UPDATE ledger_shadow_positions
                       SET net_qty = ?, cash_delta = cash_delta + ?,
                           updated_at = ?
                       WHERE symbol = ?""",
                    (new_qty, proceeds, ev["ts"], ev["symbol"]),
                )


def bootstrap_shadow(db=None) -> float:
    """Snapshot the current live portfolio into the shadow book (once).

    Idempotent: guarded by kv 'ledger:shadow:bootstrap_ts'. Returns the
    bootstrap timestamp. Call this at deploy time, before the first
    shadow event lands (scan step also self-bootstraps as a net).
    """
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    existing = db.kv_get(BOOTSTRAP_TS_KEY)
    if existing is not None:
        return float(existing)
    ts = time.time()
    with db.transaction() as conn:
        live = db.portfolio_get_all()
        for sym, pos in live.items():
            qty = float(pos.get("quantity") or 0)
            entry = float(pos.get("entry_price") or 0)
            if qty <= EPS_QTY:
                continue
            conn.execute(
                """INSERT INTO ledger_shadow_positions
                   (symbol, net_qty, avg_entry_price, cost_basis,
                    cash_delta, opened_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?)
                   ON CONFLICT(symbol) DO NOTHING""",
                (sym, qty, entry, qty * entry, ts, ts),
            )
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (BOOTSTRAP_TS_KEY, json.dumps(ts), time.time()),
        )
    logger.warning(
        "ledger: shadow bootstrap done (ts=%.0f, %d live positions copied)",
        ts, len(live),
    )
    return ts


# ---------------------------------------------------------------------------
# Shadow diff (runs after each scan round)
# ---------------------------------------------------------------------------

def _last_sell_ts(conn, symbol: str) -> float:
    row = conn.execute(
        "SELECT MAX(ts) FROM ledger_events WHERE symbol = ? AND type = 'SELL'",
        (symbol,),
    ).fetchone()
    return float(row[0] or 0) if row else 0.0


def _fuzzy_trade_row(conn, ev: Dict) -> bool:
    qty_tol = max(abs(ev["qty"]) * FUZZY_QTY_REL, 1e-9)
    price_tol = max(abs(ev["price"]) * FUZZY_PRICE_REL, 1e-9)
    row = conn.execute(
        """SELECT id FROM trades
           WHERE symbol = ? AND side = ?
             AND timestamp BETWEEN ? AND ?
             AND ABS(qty - ?) <= ?
             AND ABS(price - ?) <= ?
           LIMIT 1""",
        (
            ev["symbol"], ev["type"],
            ev["ts"] - FUZZY_TS_WINDOW_S, ev["ts"] + FUZZY_TS_WINDOW_S,
            ev["qty"], qty_tol, ev["price"], price_tol,
        ),
    ).fetchone()
    return row is not None


def _positions_diff(db, conn, pending_ages: Dict[str, int]) -> tuple:
    """Compare shadow net qty vs live portfolio rows.

    Returns (true_diffs, pending). pending = live row not yet touched by
    legacy drift cleanup after a newer shadow SELL event (cross-round
    ordering); pending older than PENDING_MAX_ROUNDS promotes to true.
    """
    true_diffs: List[Dict] = []
    pending: List[Dict] = []
    shadow = {
        r["symbol"]: float(r["net_qty"] or 0)
        for r in conn.execute(
            "SELECT symbol, net_qty FROM ledger_shadow_positions"
        ).fetchall()
    }
    live = {
        sym: float(pos.get("quantity") or 0)
        for sym, pos in db.portfolio_get_all().items()
    }
    for sym in sorted(set(shadow) | set(live)):
        sh_qty = shadow.get(sym, 0.0)
        lv_qty = live.get(sym, 0.0)
        if abs(sh_qty - lv_qty) <= max(EPS_QTY, abs(lv_qty) * 0.005):
            pending_ages.pop(sym, None)
            continue
        row = db.portfolio_get(sym)
        live_updated = float((row or {}).get("updated_at") or 0)
        last_sell = _last_sell_ts(conn, sym)
        if last_sell > 0 and live_updated > 0 and last_sell > live_updated:
            age = pending_ages.get(sym, 0) + 1
            pending_ages[sym] = age
            if age <= PENDING_MAX_ROUNDS:
                pending.append({"symbol": sym, "age_rounds": age,
                                "shadow_qty": sh_qty, "live_qty": lv_qty})
                continue
        true_diffs.append({
            "kind": "position_qty", "symbol": sym,
            "shadow_qty": sh_qty, "live_qty": lv_qty,
        })
        pending_ages.pop(sym, None)
    return true_diffs, pending


def _traces_diff(conn, bootstrap_ts: float) -> List[Dict]:
    """Every post-bootstrap event must be traceable to a trades row."""
    diffs: List[Dict] = []
    events = conn.execute(
        """SELECT ts, type, symbol, qty, price, order_id FROM ledger_events
           WHERE ts >= ? AND type IN ('BUY', 'SELL') ORDER BY ts""",
        (bootstrap_ts,),
    ).fetchall()
    for ev in events:
        ev = dict(ev)
        if ev["order_id"]:
            row = conn.execute(
                "SELECT id FROM trades WHERE client_order_id = ? LIMIT 1",
                (ev["order_id"],),
            ).fetchone()
            if row is None:
                diffs.append({
                    "kind": "trades_missing", "symbol": ev["symbol"],
                    "order_id": ev["order_id"], "side": ev["type"],
                })
        else:
            # BUY rows are written without client_order_id (legacy
            # add_position trade_add) -- match on qty/price/time window.
            if not _fuzzy_trade_row(conn, ev):
                diffs.append({
                    "kind": "trades_missing_fuzzy", "symbol": ev["symbol"],
                    "side": ev["type"], "qty": ev["qty"], "price": ev["price"],
                })
    return diffs


def _reverse_unmatched(conn, bootstrap_ts: float) -> List[Dict]:
    """INFO tier: trades rows since bootstrap with no shadow event
    (writes that bypassed the Ledger hooks -- blind spots for cutover)."""
    rows = conn.execute(
        "SELECT id, symbol, side, qty, price, timestamp, client_order_id "
        "FROM trades WHERE timestamp >= ?",
        (bootstrap_ts,),
    ).fetchall()
    unmatched: List[Dict] = []
    for r in rows:
        r = dict(r)
        if r["client_order_id"]:
            ev = conn.execute(
                "SELECT id FROM ledger_events WHERE order_id = ? LIMIT 1",
                (r["client_order_id"],),
            ).fetchone()
        else:
            qty_tol = max(abs(r["qty"]) * FUZZY_QTY_REL, 1e-9)
            price_tol = max(abs(r["price"]) * FUZZY_PRICE_REL, 1e-9)
            ev = conn.execute(
                """SELECT id FROM ledger_events
                   WHERE symbol = ? AND type = ?
                     AND ts BETWEEN ? AND ?
                     AND ABS(qty - ?) <= ? AND ABS(price - ?) <= ?
                   LIMIT 1""",
                (
                    r["symbol"], r["side"],
                    r["timestamp"] - FUZZY_TS_WINDOW_S,
                    r["timestamp"] + FUZZY_TS_WINDOW_S,
                    r["qty"], qty_tol, r["price"], price_tol,
                ),
            ).fetchone()
        if ev is None:
            unmatched.append({
                "trades_id": r["id"], "symbol": r["symbol"], "side": r["side"],
            })
    return unmatched


def shadow_diff(db=None, round_id: Optional[str] = None, emit: bool = True) -> Dict:
    """Compare the shadow book against the live four sources.

    Writes one audit row (LEDGER_SHADOW_DIFF) and one alert ONLY when a
    true diff exists; clean rounds just advance the stats counter. Never
    raises (cron-step safe).
    """
    try:
        if db is None:
            from src.state_db import get_state_db
            db = get_state_db()
        if get_mode(db) == "off":
            return {"status": "off"}
        bootstrap_ts = bootstrap_shadow(db)

        conn = db._get_conn()
        stats = db.kv_get(STATS_KEY) or {}
        pending_ages: Dict[str, int] = {
            str(k): int(v) for k, v in (stats.get("pending_ages") or {}).items()
        }

        true_diffs, pending = _positions_diff(db, conn, pending_ages)
        true_diffs.extend(_traces_diff(conn, bootstrap_ts))

        rounds = int(stats.get("rounds") or 0) + 1
        clean = not true_diffs and not pending
        consecutive_clean = int(stats.get("consecutive_clean") or 0) + 1 if clean else 0

        info = {
            "unmatched_trades_rows": _reverse_unmatched(conn, bootstrap_ts)[:10],
            "outcomes_open_live_cutoff": conn.execute(
                "SELECT COUNT(*) FROM trade_outcomes WHERE status = 'open' "
                "AND entry_time >= ?",
                (bootstrap_ts,),
            ).fetchone()[0],
            "shadow_open": conn.execute(
                "SELECT COUNT(*) FROM ledger_shadow_positions"
            ).fetchone()[0],
        }

        db.kv_set(STATS_KEY, {
            "rounds": rounds,
            "last_round_ts": time.time(),
            "last_diff_count": len(true_diffs),
            "consecutive_clean": consecutive_clean,
            "pending_ages": pending_ages,
        })

        report = {
            "status": "ok",
            "round": rounds,
            "round_id": round_id,
            "bootstrap_ts": bootstrap_ts,
            "true_diffs": true_diffs,
            "pending": pending,
            "clean": clean,
            "consecutive_clean": consecutive_clean,
            "info": info,
        }

        if true_diffs:
            logger.error(
                "LEDGER SHADOW DIFF (round %d): %d true diff(s): %s",
                rounds, len(true_diffs),
                json.dumps(true_diffs, ensure_ascii=False)[:500],
            )
            try:
                db.audit_log(
                    "LEDGER_SHADOW_DIFF",
                    {"round": rounds, "diffs": true_diffs},
                    source="ledger",
                )
            except Exception:
                logger.warning("ledger: diff audit write failed", exc_info=True)
            if emit:
                try:
                    from src.live_alerts import emit as emit_alert
                    emit_alert("LEDGER_SHADOW_DIFF", "PORTFOLIO", {
                        "round": rounds,
                        "diffs": true_diffs[:20],
                        "pending": pending[:20],
                        "ts": time.time(),
                    })
                except Exception:
                    logger.warning("ledger: diff alert emit failed", exc_info=True)
        elif pending:
            logger.info(
                "ledger shadow: round %d pending (%s) — legacy drift cleanup "
                "lagging, not counted as diff",
                rounds,
                ", ".join(p["symbol"] for p in pending),
            )
        else:
            logger.info(
                "ledger shadow: round %d clean (consecutive %d)",
                rounds, consecutive_clean,
            )
        return report
    except Exception:
        logger.warning("ledger: shadow_diff failed", exc_info=True)
        return {"status": "error"}


def get_stats(db=None) -> Dict:
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    return db.kv_get(STATS_KEY) or {}


def reset_shadow(db=None) -> None:
    """Drop the shadow book + events + stats (test / restart helper).
    Does NOT touch the mode flag or the live four sources."""
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    with db.transaction() as conn:
        conn.execute("DELETE FROM ledger_events")
        conn.execute("DELETE FROM ledger_shadow_positions")
        conn.execute("DELETE FROM kv WHERE key IN (?, ?)", (STATS_KEY, BOOTSTRAP_TS_KEY))
    logger.warning("ledger: shadow book reset")


# ---------------------------------------------------------------------------
# PRIMARY path: single-transaction four-source write
# ---------------------------------------------------------------------------

def _kv_get_raw(conn, key: str):
    row = conn.execute(
        "SELECT value FROM kv WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def _kv_upsert_raw(conn, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (key, json.dumps(value), time.time()),
    )


def _primary_apply_cash(db, conn, ev: Dict) -> float:
    """Adjust kv cash_balance inside the transaction. Returns new cash."""
    raw = _kv_get_raw(conn, "cash_balance")
    try:
        cash = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        cash = 0.0
    delta = ev["qty"] * ev["price"]
    if ev["type"] == "BUY":
        if not ev.get("deduct_cash"):
            return cash  # executor path syncs real balance instead
        cash -= delta
    else:
        cash += delta
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES ('cash_balance', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (str(cash), time.time()),
    )
    return cash


def _primary_write_portfolio(db, conn, ev: Dict) -> Dict:
    """Upsert/reduce/remove the portfolio row. Returns what was applied."""
    sym = ev["symbol"]
    row = db.portfolio_get(sym)
    now = time.time()
    if ev["type"] == "BUY":
        if row is None:
            conn.execute(
                """INSERT INTO portfolio
                   (symbol, quantity, entry_price, strategy, opened_at,
                    updated_at, stop_loss, take_profit, invest_pct)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0)""",
                (sym, ev["qty"], ev["price"],
                 str(ev.get("strategy") or "unknown"), now, now),
            )
            return {"action": "insert", "quantity": ev["qty"]}
        old_qty = float(row.get("quantity") or 0)
        old_entry = float(row.get("entry_price") or 0)
        new_qty = old_qty + ev["qty"]
        new_entry = (
            (old_qty * old_entry + ev["qty"] * ev["price"]) / new_qty
            if new_qty > 0 else ev["price"]
        )
        conn.execute(
            """UPDATE portfolio
               SET quantity = ?, entry_price = ?, updated_at = ?
               WHERE symbol = ?""",
            (new_qty, new_entry, now, sym),
        )
        return {"action": "merge", "quantity": new_qty}
    # SELL
    if row is None:
        return {"action": "no_position"}
    old_qty = float(row.get("quantity") or 0)
    new_qty = old_qty - ev["qty"]
    if new_qty <= EPS_QTY:
        conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
        return {"action": "remove", "quantity": 0}
    conn.execute(
        "UPDATE portfolio SET quantity = ?, updated_at = ? WHERE symbol = ?",
        (new_qty, now, sym),
    )
    return {"action": "reduce", "quantity": new_qty}


def _primary_write_trades(conn, ev: Dict, pnl: Optional[float]) -> None:
    """trades row with client_order_id dedup (mirrors db.trade_add)."""
    conn.execute(
        "INSERT OR IGNORE INTO trades "
        "(symbol, side, qty, price, pnl, timestamp, client_order_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ev["symbol"], ev["type"], ev["qty"], ev["price"],
         pnl if pnl is not None else 0, ev["ts"], ev["order_id"]),
    )


def _primary_write_outcomes(db, conn, ev: Dict) -> Optional[Dict]:
    """trade_outcomes lifecycle inside the transaction.

    BUY: insert an open row if event carries an `outcome_entry` payload
    (learning hooks -- bandit, rolling stats -- stay with the caller;
    the Ledger owns only the durable lifecycle row).
    SELL: close the entry (by entry_id, else latest open) mirroring
    record_outcome's metric math. Returns the outcome dict or None.
    """
    sym = ev["symbol"]
    if ev["type"] == "BUY":
        payload = ev.get("outcome_entry")
        if not payload:
            return None
        now = time.time()
        cur = conn.execute(
            """INSERT INTO trade_outcomes
               (symbol, entry_time, entry_date, entry_price, qty, score,
                strategy, factors_json, context_json, status,
                peak_price, trough_price, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (
                sym, now,
                payload.get("entry_date") or time.strftime("%Y-%m-%d"),
                ev["price"], ev["qty"],
                float(payload.get("score") or 0),
                str(payload.get("strategy") or "unknown"),
                json.dumps(payload.get("factors") or {}),
                json.dumps(payload.get("context") or {}),
                ev["price"], ev["price"], now, now,
            ),
        )
        return {"entry_id": cur.lastrowid}
    # SELL: close
    if ev.get("entry_id"):
        row = conn.execute(
            "SELECT * FROM trade_outcomes WHERE id = ?", (ev["entry_id"],),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM trade_outcomes
               WHERE symbol = ? AND status = 'open'
               ORDER BY entry_time DESC LIMIT 1""",
            (sym,),
        ).fetchone()
    if row is None:
        return None
    row = dict(row)
    entry_price = float(row["entry_price"] or 0)
    qty = float(row["qty"] or 0)
    now = time.time()
    pnl_pct = ((ev["price"] - entry_price) / entry_price * 100
               if entry_price > 0 else 0)
    pnl_abs = (ev["price"] - entry_price) * qty if entry_price > 0 else 0
    fees_abs = entry_price * qty * 0.001 * 2  # same fee model as recorder
    net_abs = pnl_abs - fees_abs
    net_pct = (net_abs / (entry_price * qty) * 100) if entry_price * qty > 0 else 0
    peak = max(float(row.get("peak_price") or entry_price), ev["price"])
    trough = min(float(row.get("trough_price") or entry_price), ev["price"])
    conn.execute(
        """UPDATE trade_outcomes SET
             exit_time = ?, exit_price = ?, exit_reason = ?,
             pnl_pct = ?, pnl_absolute = ?, net_pnl_pct = ?,
             net_pnl_absolute = ?, time_held_hours = ?,
             max_profit_pct = ?, max_drawdown_pct = ?,
             peak_price = ?, trough_price = ?, is_win = ?,
             status = 'closed', updated_at = ?
           WHERE id = ?""",
        (
            now, ev["price"],
            str(ev.get("exit_reason") or "ledger"),
            round(pnl_pct, 4), round(pnl_abs, 6),
            round(net_pct, 4), round(net_abs, 6),
            round((now - float(row["entry_time"])) / 3600, 2),
            round((peak - entry_price) / entry_price * 100, 4) if entry_price > 0 else 0,
            round((trough - entry_price) / entry_price * 100, 4) if entry_price > 0 else 0,
            peak, trough, 1 if net_pct > 0 else 0,
            now, row["id"],
        ),
    )
    return {
        "id": row["id"], "symbol": sym, "status": "closed",
        "net_pnl_pct": round(net_pct, 2),
    }


def _primary_write_tracker(conn, ev: Dict) -> Optional[Dict]:
    """tp_sl_tracker kv inside the transaction.

    BUY: write the tracker state if the event carries one (same JSON
    shape as tp_sl_tracker.save_state).
    SELL full close: remove the tracker key (a closed position must not
    keep tracking state -- the INJ remnant class).
    """
    sym = ev["symbol"]
    key = f"tp_sl_tracker:{sym}"
    if ev["type"] == "BUY":
        payload = ev.get("tracker")
        if not payload:
            return None
        now = time.time()
        state = {
            "entry_price": ev["price"],
            "total_qty": ev["qty"],
            "tp_orders": payload.get("tp_orders") or [],
            "sl_order": payload.get("sl_order") or None,
            "tp_filled": [False] * len(payload.get("tp_orders") or []),
            "sl_moved_after_tp": 0,
            "created_at": now,
            "updated_at": now,
        }
        _kv_upsert_raw(conn, key, state)
        return {"action": "saved"}
    if ev.get("full_close"):
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        return {"action": "removed"}
    return None


def _apply_primary(db, ev: Dict, round_id: Optional[str]) -> Dict:
    """Single-transaction four-source write. All SQL runs on ONE
    connection inside BEGIN IMMEDIATE; any step failure rolls back
    everything. Raises LedgerError after rollback on failure.

    NOTE: StateDB helpers (trade_add / portfolio_set / kv_set / audit_log)
    each commit per call -- they MUST NOT be used inside this transaction
    or atomicity breaks. Writes here are raw SQL on the shared conn.
    """
    try:
        with db.transaction() as conn:
            portfolio_applied = _primary_write_portfolio(db, conn, ev)
            cash_after = _primary_apply_cash(db, conn, ev)

            pnl = ev.get("pnl")
            if pnl is None and ev["type"] == "SELL":
                row = conn.execute(
                    "SELECT AVG(price) FROM trades WHERE symbol = ? AND side = 'BUY'",
                    (ev["symbol"],),
                ).fetchone()
                avg_buy = float(row[0] or 0) if row else 0.0
                if avg_buy > 0:
                    pnl = (ev["price"] - avg_buy) * ev["qty"]

            _primary_write_trades(conn, ev, pnl)
            outcome = _primary_write_outcomes(db, conn, ev)
            tracker = _primary_write_tracker(conn, ev)
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, details, source) "
                "VALUES (?, 'LEDGER_FILL', ?, 'ledger')",
                (time.time(), json.dumps({
                    "type": ev["type"], "symbol": ev["symbol"],
                    "qty": ev["qty"], "price": ev["price"],
                    "order_id": ev["order_id"], "source": ev["source"],
                    "pnl": pnl, "round_id": round_id,
                    "applied": {
                        "portfolio": portfolio_applied,
                        "cash_after": cash_after,
                        "outcome": outcome,
                        "tracker": tracker,
                    },
                })),
            )
            # Primary mode keeps its own event log too (audit trail of
            # exactly which fills the Ledger owns once cut over).
            conn.execute(
                """INSERT INTO ledger_events
                   (ts, type, symbol, qty, price, order_id, source, pnl,
                    exit_reason, deduct_cash, payload_json, round_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'ledger.primary', ?, ?, ?, ?, ?)""",
                (
                    ev["ts"], ev["type"], ev["symbol"], ev["qty"], ev["price"],
                    ev["order_id"], pnl, ev.get("exit_reason"),
                    1 if ev.get("deduct_cash") else 0,
                    json.dumps({"entry_id": ev.get("entry_id")}
                               ) if ev.get("entry_id") else None,
                    round_id,
                ),
            )
        return {
            "status": "ok", "mode": "primary",
            "pnl": pnl, "outcome": outcome, "tracker": tracker,
        }
    except Exception as e:
        # transaction ctx already rolled back. Best-effort failure audit
        # OUTSIDE the rolled-back tx so ops can see it.
        try:
            db.audit_log(
                "LEDGER_FILL_FAILED",
                {
                    "type": ev["type"], "symbol": ev["symbol"],
                    "order_id": ev["order_id"], "source": ev["source"],
                    "error": str(e)[:300],
                },
                source="ledger",
            )
        except Exception:
            logger.warning("ledger: failure-audit write failed", exc_info=True)
        raise LedgerError(
            f"primary fill write failed for {ev['symbol']} "
            f"({ev['type']} order_id={ev['order_id']}): {e}"
        ) from e


# ---------------------------------------------------------------------------
# Compensator repairs (P2 order item 2): guardian / reconciler become pure
# DETECTORS — they no longer write state directly. Every state mutation they
# used to make is expressed as a repair instruction and funneled through
# record_repair() here, so the Ledger owns all compensator writes in one
# place (invariant checks land here once, not in N callers).
#
# Semantics-preserving by construction: each kind applies EXACTLY the write
# the compensator performed before (same keys, same values, same upsert
# semantics). The additions are one LEDGER_REPAIR audit row + one REPAIR
# ledger event (shadow visibility of state mutations that used to be
# invisible to the shadow book).
#
# Rollback lever: kv 'ledger:repairs' -> 0 makes callers fall back to their
# legacy direct writes (kept in place).
# ---------------------------------------------------------------------------

REPAIRS_KEY = "ledger:repairs"


def repairs_enabled(db=None) -> bool:
    """Compensator repairs funnel flag (default ON)."""
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    flag = db.kv_get(REPAIRS_KEY, 1)
    return bool(flag)


def _repair_tracker_state(conn, repair: Dict) -> Dict:
    """tp_sl_tracker:{sym} kv — byte-identical to tp_sl_tracker.save_state."""
    p = repair.get("payload") or {}
    sym = repair["symbol"]
    tp_orders = p.get("tp_orders") or []
    now = time.time()
    state = {
        "entry_price": float(p.get("entry") or 0),
        "total_qty": float(p.get("qty") or 0),
        "tp_orders": tp_orders,
        "sl_order": p.get("sl_order") or None,
        "tp_filled": [False] * len(tp_orders),
        "sl_moved_after_tp": 0,
        "created_at": now,
        "updated_at": now,
    }
    _kv_upsert_raw(conn, f"tp_sl_tracker:{sym}", state)
    return {"action": "tracker_state", "tp_count": len(tp_orders)}


def _repair_guard_audit(conn, repair: Dict) -> Dict:
    """audit_log row carrying the ORIGINAL action name (downstream greps
    and dashboards keep working) + REPAIR event for shadow visibility.
    No extra LEDGER_REPAIR row — that would double-count the same action."""
    p = repair.get("payload") or {}
    details = p.get("details")
    conn.execute(
        "INSERT INTO audit_log (timestamp, action, details, source) "
        "VALUES (?, ?, ?, ?)",
        (
            time.time(), str(p.get("action") or "GUARDIAN"),
            json.dumps(details) if not isinstance(details, str) else details,
            str(repair.get("source") or "protection_guardian"),
        ),
    )
    return {"action": "audit", "audit_action": p.get("action")}


def _repair_portfolio_fix(conn, repair: Dict) -> Dict:
    """portfolio row remove/set — same upsert semantics as StateDB
    (stop_loss/take_profit/invest_pct preserved via COALESCE)."""
    p = repair.get("payload") or {}
    sym = str(repair["symbol"]).replace("/", "")
    act = p.get("action")
    if act == "remove":
        conn.execute("DELETE FROM portfolio WHERE symbol = ?", (sym,))
        return {"action": "remove", "symbol": sym}
    data = p.get("data") or {}
    now = time.time()
    conn.execute(
        """INSERT INTO portfolio
           (symbol, quantity, entry_price, strategy, opened_at, updated_at,
            stop_loss, take_profit, invest_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             quantity=excluded.quantity,
             entry_price=excluded.entry_price,
             strategy=excluded.strategy,
             opened_at=excluded.opened_at,
             updated_at=excluded.updated_at,
             stop_loss=COALESCE(excluded.stop_loss, portfolio.stop_loss),
             take_profit=COALESCE(excluded.take_profit, portfolio.take_profit),
             invest_pct=COALESCE(excluded.invest_pct, portfolio.invest_pct)""",
        (
            sym, data.get("quantity", 0), data.get("entry_price", 0),
            data.get("strategy", ""), data.get("opened_at", now), now,
            data.get("stop_loss"), data.get("take_profit"),
            data.get("invest_pct", 0),
        ),
    )
    return {"action": "set", "symbol": sym, "quantity": data.get("quantity")}


# kinds whose apply fn needs the full repair dict (kv-only kinds are
# handled inline in record_repair)
_REPAIR_KINDS = {
    "tracker_state": _repair_tracker_state,
    "portfolio_fix": _repair_portfolio_fix,
    "guard_audit": _repair_guard_audit,
}

# audit/event routing (reconcile_report is payload-conditional):
#   audit: state mutations worth an audit row (swap_ts included — a swap
#     marks a healed position; breach_state excluded — throttle state,
#     no audit pre-P2 either; snapshot excluded — routine every round)
#   event: everything except the routine snapshot
_REPAIR_AUDIT_KINDS = {"tracker_state", "portfolio_fix", "swap_ts"}
_REPAIR_EVENT_KINDS = {"tracker_state", "portfolio_fix", "guard_audit",
                       "swap_ts", "breach_state"}


def record_repair(repair: Dict, db=None) -> Dict:
    """Single ingress for compensator repair instructions.

    repair = {
      "kind": tracker_state | swap_ts | breach_state | guard_audit |
              portfolio_fix | reconciler_snapshot | reconcile_report,
      "symbol": optional str,
      "source": "protection_guardian" | "portfolio_reconciler" | ...,
      "order_id": optional (carried onto the REPAIR event),
      "payload": kind-specific,
    }

    Routing table (per kind):
      tracker_state      kv tp_sl_tracker:{sym}   +audit +event
      swap_ts            kv gov:swap_ts:{sym}      +audit +event
      breach_state       kv tp_breach_state:{sym}   -audit +event  (throttle
                          state: high frequency, no audit today either)
      guard_audit        audit_log (original action name) +event
      portfolio_fix      portfolio row remove/set  +audit +event
      reconciler_snapshot kv reconcile_prev_positions  -audit -event (routine)
      reconcile_report   kv reconciler:report (+audit +event only when
                          payload["audit"] — actionable rounds)

    Atomic: kv/audit/event land in ONE transaction or not at all.
    Never silently degrades — raises on failure so the caller's legacy
    fallback (repairs flag off / funnel exception) takes over.
    """
    if db is None:
        from src.state_db import get_state_db
        db = get_state_db()
    kind = str((repair or {}).get("kind") or "")
    sym = (repair or {}).get("symbol")
    payload = (repair or {}).get("payload") or {}
    source = str((repair or {}).get("source") or "compensator")

    applied: Optional[Dict] = None
    with db.transaction() as conn:
        if kind == "swap_ts":
            _kv_upsert_raw(
                conn, f"gov:swap_ts:{sym}",
                payload.get("value") or {"ts": time.time()},
            )
            applied = {"action": "swap_ts", "symbol": sym}
        elif kind == "breach_state":
            _kv_upsert_raw(
                conn, f"tp_breach_state:{sym}", payload.get("state") or {}
            )
            applied = {"action": "breach_state", "symbol": sym}
        elif kind == "reconciler_snapshot":
            _kv_upsert_raw(conn, "reconcile_prev_positions",
                           payload.get("data") or {})
            applied = {"action": "reconciler_snapshot"}
        elif kind == "reconcile_report":
            report = payload.get("report") or {}
            _kv_upsert_raw(conn, "reconciler:report", report)
            applied = {"action": "reconcile_report"}
        elif kind in _REPAIR_KINDS:
            applied = _REPAIR_KINDS[kind](conn, repair)
        else:
            raise ValueError(f"unknown repair kind {kind!r}")

        if kind == "reconcile_report":
            wants_audit = wants_event = bool(payload.get("audit"))
        else:
            wants_audit = kind in _REPAIR_AUDIT_KINDS
            wants_event = kind in _REPAIR_EVENT_KINDS
        if wants_audit:
            conn.execute(
                "INSERT INTO audit_log (timestamp, action, details, source) "
                "VALUES (?, 'LEDGER_REPAIR', ?, 'ledger')",
                (time.time(), json.dumps({
                    "kind": kind, "symbol": sym, "source": source,
                    "applied": applied,
                })),
            )
        if wants_event:
            conn.execute(
                """INSERT INTO ledger_events
                   (ts, type, symbol, qty, price, order_id, source, pnl,
                    exit_reason, deduct_cash, payload_json, round_id)
                   VALUES (?, 'REPAIR', ?, 0, 0, ?, ?, NULL, NULL, NULL, ?, NULL)""",
                (
                    time.time(), str(sym or ""), repair.get("order_id"),
                    source, json.dumps({"kind": kind, "applied": applied,
                                        "payload": payload}),
                ),
            )
    logger.info(
        "ledger repair applied: kind=%s symbol=%s source=%s -> %s",
        kind, sym, source, applied,
    )
    return {"status": "ok", "kind": kind, "applied": applied}


# ---------------------------------------------------------------------------
# CLI: python -m src.ledger <bootstrap|report|stats|set-mode X|reset>
# ---------------------------------------------------------------------------

def _cli() -> None:  # pragma: no cover - manual ops entry
    import sys
    from src.state_db import get_state_db

    db = get_state_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "bootstrap":
        print(f"bootstrap_ts={bootstrap_shadow(db):.0f}")
    elif cmd == "report":
        print(json.dumps(shadow_diff(db), indent=2, ensure_ascii=False, default=str))
    elif cmd == "stats":
        print(json.dumps(get_stats(db), indent=2, ensure_ascii=False, default=str))
        print("mode:", get_mode(db))
    elif cmd == "set-mode":
        print("mode ->", set_mode(db, sys.argv[2]))
    elif cmd == "reset":
        reset_shadow(db)
        print("shadow book reset")
    elif cmd == "set-repairs":
        db.kv_set("ledger:repairs", 1 if sys.argv[2] == "1" else 0)
        print("repairs flag ->", db.kv_get("ledger:repairs"))
    else:
        print("usage: python -m src.ledger "
              "[bootstrap|report|stats|set-mode <off|shadow|primary>|reset|"
              "set-repairs <0|1>]")


if __name__ == "__main__":  # pragma: no cover
    _cli()
