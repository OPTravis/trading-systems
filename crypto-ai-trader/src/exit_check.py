"""
WO-0931 (2026-09-30): strategy exit signal consumption — single-source engine.

Impact recap (full-codebase scan):
  * 6/6 strategies return SELL from their position branch (tp/sl/momentum
    reversal) but nothing consumes them — exits relied purely on resting
    exchange orders (entry OCO + guardian re-lists). No active market-exit
    path existed (QNT 9/30: TP +8% crossed, +9.7% peak gave back to +5.2%).
  * hold expiry (per-strategy max_hold_hours) had no execution point.
  * trailing-check had no schedule (P2, untouched here).

P1 scope (Leo approved 2026-09-30 10:48):
  * Deterministic exits AUTO: hold_expiry / take_profit / stop_loss.
  * momentum reversal: notify-only (P2 promotes after observation).
  * Kill-switch kv  exit:mode = auto | notify | off   (default: auto).
  * Guard rails: kv per-symbol cooldown, exchange-balance truth, OCO
    cancel-then-market-sell with safety-net re-SL on sell failure,
    booking reuses the WO-0928 event-anchored reconciler (orderId idempotent,
    governor loss-exit cooldown + outcome rows come along for free).

Layout: evaluate (pure read) -> decision dicts; execute (sell chain);
scan_exit_triggers (read-only fast check for reside_scan event_tick);
run_exit_step (orchestrator entry).
"""

import logging
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---- knobs (kv overridable, per-symbol beats global beats default) ----------
EXIT_MODE_KV = "exit:mode"            # auto | notify | off (default auto)
EXIT_COOLDOWN_S = 600.0               # per-symbol re-trigger guard (10 min)
EXIT_DEFAULTS = {
    "tp_pct": 8.0,                    # take-profit pct (>= entry), QNT case
    "sl_pct": 6.0,                    # stop-loss pct — tighter than the
                                      # resting OCO -15% safety, wide enough
                                      # not to fight normal noise
    "hold_hours": 48.0,               # bollinger-family default max hold
}
MOMENTUM_BAND_POS_MAX = 0.35          # bollinger P1-fix reversal threshold
# P2 (Leo 2026-09-30 11:18): momentum reversal promoted to auto — same
# tier as the deterministic kinds, observation week waived.
AUTO_KINDS = ("hold_expiry", "take_profit", "stop_loss",
              "momentum_reversal")


def _mode(db) -> str:
    try:
        m = (db.kv_get(EXIT_MODE_KV) or "auto").strip().lower()
        return m if m in ("auto", "notify", "off") else "auto"
    except Exception:
        return "auto"


def _param(db, key: str, symbol: str) -> float:
    for k in (f"exit:{symbol}:{key}", f"exit:{key}"):
        try:
            v = db.kv_get(k)
        except Exception:
            v = None
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return float(EXIT_DEFAULTS[key])


# ---- time anchors -----------------------------------------------------------
def _last_buy_ts(db, symbol: str) -> float:
    """Trades-table anchor (sync_from_binance refreshes portfolio.opened_at
    on rebuild — 0G 9/30 10:30 got a fresh opened_at after a 10:2x sync —
    so hold-expiry must anchor on the last real BUY, like the reconciler).
    Raw SQL first so a read-only sqlite3 connection (reside_scan event_tick)
    works too; StateDB method chain is the fallback."""
    try:
        row = db.execute(
            "SELECT MAX(timestamp) FROM trades "
            "WHERE symbol = ? AND side = 'BUY'", (symbol,)).fetchone()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    try:
        from src.portfolio_reconciler import _last_buy_ts_ms
        ms = _last_buy_ts_ms(db, symbol)
        if ms:
            return ms / 1000.0
    except Exception:
        pass
    return 0.0


def _opened_ts(position: Dict) -> float:
    raw = position.get("opened_at") or ""
    try:
        import datetime as _dt
        s = str(raw).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _held_hours(db, position: Dict, now: float) -> float:
    # Priority, not max: the trades BUY anchor is the true lifecycle start;
    # portfolio.opened_at gets REFRESHED by sync_from_binance rebuilds
    # (0G 9/30 10:30) so max() would reset the hold clock and the expiry
    # would never fire. opened_at is only the fallback when no BUY row
    # exists (e.g. pre-P0-2 rows).
    anchor = _last_buy_ts(db, position["symbol"])
    if anchor <= 0:
        anchor = _opened_ts(position)
    if anchor <= 0:
        return 0.0
    return max(0.0, (now - anchor) / 3600.0)


def _pnl_pct(position: Dict, price: float) -> float:
    entry = float(position.get("entry_price") or 0.0)
    if entry <= 0 or price <= 0:
        return 0.0
    return (price - entry) / entry * 100.0


def _band_position(client, symbol: str) -> Optional[float]:
    """Bollinger(20,2) band position from 1h klines — momentum reversal
    (notify-only in P1) mirrors the bollinger P1-fix exit branch."""
    try:
        klines = client.get_klines(symbol, interval="1h", limit=40) or []
        if len(klines) < 20:
            return None
        closes = [float(k["close"]) for k in klines[-20:]]
        mid = sum(closes) / len(closes)
        var = sum((c - mid) ** 2 for c in closes) / len(closes)
        sd = var ** 0.5
        upper, lower = mid + 2 * sd, mid - 2 * sd
        if upper <= lower:
            return None
        return (closes[-1] - lower) / (upper - lower)
    except Exception:
        return None


# ---- decision core (pure, shared with backtest — WO-1003-7) ------------------
def evaluate_one(pos, price, held_hours, *, tp_pct, sl_pct, hold_hours,
                 band_position=None, band_pos_max=MOMENTUM_BAND_POS_MAX,
                 include_momentum=True, now=0.0):
    """Single-position exit decision — THE one place exit semantics live.

    Pure: no db, no client, no clock. Live evaluate_exits() feeds it
    kv params / trades anchors / ticker prices; the backtest replay
    (BacktestEngine, WO-1003-7) feeds it bar closes and in-memory hold
    hours — so live and backtest exits can never drift apart again
    (P1-4 two-codebases gap closed).

    Priority order mirrors the live chain: hold > tp > sl > momentum.
    Returns a decision dict (same shape as live) or None.
    """
    qty = float(pos.get("quantity") or 0)
    sym = pos.get("symbol")
    if not sym or qty <= 0 or price <= 0:
        return None
    pnl_pct = _pnl_pct(pos, price)
    base = {
        "symbol": sym, "qty": qty,
        "entry_price": float(pos.get("entry_price") or 0),
        "price": price, "pnl_pct": round(pnl_pct, 2),
        "held_hours": round(held_hours, 1), "ts": now,
    }
    if held_hours >= hold_hours:
        return {**base, "kind": "hold_expiry", "auto": True, "sell_pct": 100,
                "reason": f"hold expiry {held_hours:.1f}h >= {hold_hours:.0f}h"}
    if pnl_pct >= tp_pct:
        return {**base, "kind": "take_profit", "auto": True, "sell_pct": 100,
                "reason": f"take profit {pnl_pct:.2f}% >= {tp_pct:.1f}%"}
    # WO-1011 (10/3 PENGU lesson): the StateDB trailing target (raised by
    # _step_trailing_check) is the tightest floor the strategy promised —
    # enforce it BEFORE the looser fixed -sl_pct line. PENGU 10/3: target
    # had trailed to 0.009725 (-5%) while price slid to 0.0088; the exit
    # chain never read the column, so the position settled at -14% through
    # the guardian-clamped OCO floor 0.008808 instead. Breach semantics:
    # price <= stop_loss, sign of pnl irrelevant (a trailed floor above
    # entry locks profit). Absent key (backtest replays, tests) -> None,
    # zero behavior change.
    t_sl = pos.get("stop_loss")
    if t_sl and price <= float(t_sl):
        return {**base, "kind": "stop_loss", "auto": True, "sell_pct": 100,
                "reason": (f"trailing stop breach {price:.6g} <= "
                           f"stop_loss {float(t_sl):.6g} "
                           f"(pnl {pnl_pct:+.2f}%)")}
    if pnl_pct <= -sl_pct:
        return {**base, "kind": "stop_loss", "auto": True, "sell_pct": 100,
                "reason": f"stop loss {pnl_pct:.2f}% <= -{sl_pct:.1f}%"}
    if include_momentum and pnl_pct > 0 and band_position is not None \
            and band_position < band_pos_max:
        sell_pct = 100 if band_position < 0.15 else 50  # bollinger tiers
        return {**base, "kind": "momentum_reversal", "auto": True,
                "sell_pct": sell_pct,
                "reason": (f"momentum reversal band_position="
                           f"{band_position:.2f} (< {band_pos_max}) "
                           f"pnl {pnl_pct:+.2f}%")}
    return None


# ---- evaluate (pure read) ---------------------------------------------------
def evaluate_exits(
    client, db, *, holdings: Optional[List[Dict]] = None,
    prices: Optional[Dict[str, float]] = None,
    now: Optional[float] = None, include_momentum: bool = True,
) -> List[Dict]:
    """Evaluate exit conditions on every open position. Returns decisions
    (never executes). Each decision: symbol/kind/auto/sell_pct/reason/..."""
    now = now or time.time()
    decisions: List[Dict] = []
    if holdings is None:
        # pure DB read (PortfolioManager.get_all_positions lazy-creates a
        # live client and batch-fetches prices — too heavy + API-hitting
        # for a decision pass that re-fetches price per symbol anyway)
        try:
            rows = db._get_conn().execute(
                "SELECT symbol, quantity, entry_price, strategy, opened_at, "
                "stop_loss FROM portfolio WHERE quantity > 0").fetchall()
            holdings = [
                {"symbol": r[0], "quantity": r[1], "entry_price": r[2],
                 "strategy": r[3], "opened_at": r[4],
                 # WO-1011: carry the trailing target so evaluate_one can
                 # enforce the StateDB floor (None/0 = no trailing state)
                 "stop_loss": r[5] if r[5] and float(r[5]) > 0 else None}
                for r in rows]
        except Exception:
            holdings = []
    for pos in holdings or []:
        sym = pos.get("symbol")
        qty = float(pos.get("quantity") or 0)
        if not sym or qty <= 0 or not sym.endswith("USDT"):
            continue
        price = (prices or {}).get(sym)
        if price is None:
            try:
                price = float(client.get_ticker_price(sym) or 0)
            except Exception:
                price = 0.0
        if price <= 0:
            continue
        # WO-1003-7: shell collects inputs; the pure core decides — the
        # exact same evaluate_one() the backtest replay calls.
        held_h = _held_hours(db, pos, now)
        dec = evaluate_one(
            pos, price, held_h,
            tp_pct=_param(db, "tp_pct", sym),
            sl_pct=_param(db, "sl_pct", sym),
            hold_hours=_param(db, "hold_hours", sym),
            band_position=_band_position(client, sym) if include_momentum else None,
            include_momentum=include_momentum, now=now,
        )
        if dec is not None:
            decisions.append(dec)
    return decisions


def scan_exit_triggers(db, holdings: List[str], prices: Dict[str, float],
                       *, now: Optional[float] = None) -> Optional[str]:
    """Read-only fast check for reside_scan event_tick (hold/tp/sl only —
    momentum needs klines and stays in the scan step). Returns a trigger
    reason string or None. Pure DB reads + caller-provided prices.

    WO-1004 (3) contract: `db` may be a raw sqlite3 connection (what
    reside_scan passes today — has .execute) OR a StateDB instance (the
    .execute AttributeError it used to raise was swallowed by the bare
    except, silently disabling the trigger path — the 13:20 0G miss
    lesson: silent excepts on the exit chain are observability holes).
    Both call shapes now work; anything else logs once and returns None.
    """
    now = now or time.time()
    conn = db
    if not callable(getattr(conn, "execute", None)):
        get_conn = getattr(db, "_get_conn", None)
        conn = get_conn() if callable(get_conn) else None
    if conn is None:
        logger.warning(
            "scan_exit_triggers: db param is neither a sqlite connection "
            "nor a StateDB (type=%s) — trigger check skipped",
            type(db).__name__)
        return None
    try:
        rows = conn.execute(
            "SELECT symbol, quantity, entry_price, opened_at, stop_loss "
            "FROM portfolio WHERE quantity > 0").fetchall()
    except Exception:
        logger.warning("scan_exit_triggers: portfolio read failed",
                       exc_info=True)
        return None
    for sym, qty, entry, opened_at, stop_loss in rows:
        if sym not in holdings or not sym.endswith("USDT"):
            continue
        price = prices.get(sym)
        if not price or not entry or entry <= 0:
            continue
        pnl_pct = (price - entry) / entry * 100.0
        pos = {"symbol": sym, "quantity": qty, "entry_price": entry,
               "opened_at": opened_at}
        held_h = _held_hours(db, pos, now)
        if held_h >= _param(db, "hold_hours", sym):
            return f"{sym} hold_expiry {held_h:.1f}h"
        if pnl_pct >= _param(db, "tp_pct", sym):
            return f"{sym} take_profit {pnl_pct:+.2f}%"
        # WO-1011: the event tick must see the StateDB trailing target too
        # — a pure floor breach (hold not expired, pnl above the fixed
        # -sl_pct line) used to leave the 10-min trigger path blind and
        # wait for a full gate-open round. Mirrors evaluate_one exactly.
        if stop_loss and float(stop_loss) > 0 and price <= float(stop_loss):
            return f"{sym} trailing_stop_breach {price:.6g} <= {float(stop_loss):.6g}"
        if pnl_pct <= -_param(db, "sl_pct", sym):
            return f"{sym} stop_loss {pnl_pct:+.2f}%"
    return None


# ---- notify -----------------------------------------------------------------
def _notify(db, notif_id: str, title: str, body: str) -> None:
    try:
        db.notification_outbox_add(notif_id, "exit_signal", title, body)
    except Exception:
        logger.warning("exit outbox insert failed (non-fatal)", exc_info=True)


# ---- execute ----------------------------------------------------------------
def _cancel_sell_legs(client, symbol: str) -> Dict:
    """Cancel every open SELL-side order (OCO legs included). Any leg that
    survives 3 attempts aborts the exit — the remaining OCO stays as the
    floor protection (never leave a position naked)."""
    try:
        orders = client.get_open_orders(symbol) or []
    except Exception:
        return {"ok": False, "aborted": "openOrders fetch failed", "cancelled": 0}
    legs = [o for o in orders
            if str(o.get("side", "")).upper() == "SELL"
            and str(o.get("status", "NEW")).upper() in ("NEW", "PARTIALLY_FILLED", "")]

    if not legs:
        return {"ok": True, "aborted": None, "cancelled": 0}

    for leg in legs:
        oid = leg.get("orderId") or leg.get("id")
        done = False
        for attempt in range(3):
            try:
                if client.cancel_order(symbol, oid):
                    done = True
                    break
            except Exception:
                logger.warning("exit cancel %s %s attempt %d failed",
                               symbol, oid, attempt + 1)
                time.sleep(1)
        if not done:
            return {"ok": False,
                    "aborted": f"cancel failed for order {oid} (OCO kept)",
                    "cancelled": 0}
    time.sleep(1)  # let the exchange unlock the balances (trailing-check precedent)
    return {"ok": True, "aborted": None, "cancelled": len(legs)}


def execute_exit(client, db, decision: Dict, *, now: Optional[float] = None,
                  bypass_cooldown: bool = False) -> Dict:
    """Auto-exit chain: cooldown gate -> cancel SELL legs -> market sell ->
    WO-0928 reconciler books the fill (orderId-anchored, governor cooldown +
    outcome included) -> kv cooldown + outbox notice. Any failure keeps the
    resting OCO floor (or re-lists an emergency SL) and alerts.

    bypass_cooldown=True is reserved for explicit human commands
    (control-panel forceexit): an operator staring at a crashing chart must
    not be told to wait 10 minutes."""
    now = now or time.time()
    sym = decision["symbol"]
    kind = decision["kind"]
    out = {"symbol": sym, "kind": kind, "status": "skipped"}

    # kv cooldown guard (double-trigger window across event/scan rounds)
    if not bypass_cooldown:
        last = 0.0
        try:
            last = float(db.kv_get(f"exit:{sym}:last_exit_ts") or 0)
        except Exception:
            pass
        if now - last < EXIT_COOLDOWN_S:
            out["status"] = "cooldown"
            return out

    # 0) dust pre-flight (WO-1009-③, 10/2): if the position's notional
    #    cannot clear minNotional, a market sell is impossible — and the
    #    legacy order (cancel legs first, then hit the dust guard) strips
    #    the position naked, the next guardian sweep rebuilds the OCO,
    #    and the next exit event tears it down again. 10/2 03:40-08:41
    #    PENGU ran that NAKED→TP_ONLY→OCO loop ~15 rounds while every
    #    sell aborted on "below tradable". Decision qty (not free
    #    balance) is the yardstick here: free is ~0 while a full-qty
    #    OCO locks the base; the post-cancel min(qty, free) check below
    #    still guards slice mismatches for tradable positions.
    try:
        _pf_filters = client.get_symbol_filters(sym) or {}
        _pf_min_notional = float(_pf_filters.get("minNotional") or 0)
        _pf_qty = float(decision["qty"])
        if (_pf_min_notional
                and _pf_qty * float(decision["price"]) < _pf_min_notional):
            out.update(
                status="aborted",
                detail=(f"dust pre-flight: qty {_pf_qty:.6f} × px "
                        f"{float(decision['price']):.6g} = "
                        f"{_pf_qty * float(decision['price']):.2f} < "
                        f"minNotional {_pf_min_notional:g} — resting legs "
                        f"kept, nothing cancelled"))
            # stable id (no timestamp): every round of a permanently
            # dust-locked exit produces the same notice, so the outbox
            # dedupes instead of paging once per loop iteration
            _notify(db, f"exit:{sym}:{kind}:dust_preflight",
                    f"⚠️ Exit skipped (dust) — {sym}",
                    f"{decision['reason']} — position notional below "
                    f"minNotional, cannot market-sell. Resting OCO kept "
                    f"as floor; resolution left to the dust reaper.")
            return out
    except Exception:
        logger.warning("exit dust pre-flight errored for %s (legacy "
                       "order: cancel first)", sym, exc_info=True)

    # 1) cancel resting SELL legs (abort keeps the OCO floor)
    cancel = _cancel_sell_legs(client, sym)
    if not cancel["ok"]:
        out.update(status="aborted", detail=cancel["aborted"])
        _notify(db, f"exit:{sym}:{kind}:{int(now)}",
                f"⚠️ Exit aborted — {sym}",
                f"{decision['reason']} — {cancel['aborted']}. "
                f"Resting OCO kept as floor. Manual check advised.")
        return out

    # 2) market sell (floor to stepSize; exchange free balance is truth)
    try:
        qty = float(decision["qty"])
        sell_pct = float(decision.get("sell_pct") or 100)
        filters = client.get_symbol_filters(sym) or {}
        step = float(filters.get("stepSize") or 0)
        if step > 0:
            import math as _m
            qty = _m.floor(qty * sell_pct / 100 / step) * step
        base_asset = sym[:-4]
        free = client.get_free_balance(base_asset) or 0.0
        qty = min(qty, free)
        min_notional = float(filters.get("minNotional") or 0)
        if qty <= 0 or (min_notional and qty * decision["price"] < min_notional):
            out.update(status="aborted",
                       detail=f"qty {qty:.6f} below tradable (free {free:.6f})")
            _notify(db, f"exit:{sym}:{kind}:{int(now)}",
                    f"⚠️ Exit aborted — {sym}",
                    f"{decision['reason']} — sell qty {qty:.6f} not tradable, "
                    f"no legs cancelled harmfully. Manual check advised.")
            return out
        res = client.place_market_sell(sym, qty)
        if not res:
            raise RuntimeError("market sell returned no result")
    except Exception as e:
        # safety net: re-list an emergency SL so the position is never naked
        try:
            sl_px = round(decision["price"] * 0.97, 6)
            client.place_stop_loss_market(sym, qty, sl_px)
            note = "emergency SL re-listed at -3%"
        except Exception:
            note = "CRITICAL: emergency SL re-list FAILED — naked position"
        out.update(status="failed", detail=f"market sell failed: {e}; {note}")
        _notify(db, f"exit:{sym}:{kind}:{int(now)}",
                f"🛑 Exit FAILED — {sym}",
                f"{decision['reason']} — market sell failed ({e}); {note}.")
        return out

    # 3) PARTIAL sells (sell_pct < 100, momentum tier < 0.15..0.35) cancelled
    #    an OCO that covered the FULL qty — re-list a breakeven floor SL for
    #    the remainder or it sits naked until the next guardian pass (60 min).
    #    Breakeven (entry * 0.995) is safe because momentum fires pnl > 0;
    #    if that sits too close to the last price, fall back to -5%.
    relist_note = ""
    if sell_pct < 100:
        import math as _m2
        step2 = float((client.get_symbol_filters(sym) or {}).get("stepSize") or 0)
        rem_qty = max(float(decision["qty"]) - qty, 0.0)
        if step2 > 0:
            rem_qty = _m2.floor(rem_qty / step2) * step2
        if rem_qty * decision["price"] >= max(min_notional, 0):
            try:
                entry = float(decision.get("entry_price") or 0)
                cand = entry * 0.995 if entry > 0 else 0.0
                if cand <= 0 or cand >= decision["price"] * 0.98:
                    cand = round(decision["price"] * 0.95, 6)
                if client.place_stop_loss_market(sym, rem_qty, round(cand, 6)):
                    relist_note = (f"; remainder {rem_qty:.6f} protected by "
                                   f"breakeven SL @ {cand:.6f}")
                else:
                    relist_note = "; CRITICAL: remainder SL re-list returned None"
            except Exception as e:
                relist_note = f"; CRITICAL: remainder SL re-list FAILED ({e})"
        else:
            relist_note = f"; remainder {rem_qty:.6f} below minNotional (dust)"

    # 4) book via the WO-0928 event-anchored reconciler (idempotent, full
    #    chain: trades + ledger_events + shadow + portfolio trim + governor
    #    loss-exit cooldown + outcome + outbox)
    try:
        from src.portfolio_reconciler import reconcile_exchange_fills
        reconcile_exchange_fills(client, db)
    except Exception:
        logger.warning("exit post-sell reconcile failed (next round heals)",
                       exc_info=True)

    # 5) stamp cooldown + strong notice
    try:
        db.kv_set(f"exit:{sym}:last_exit_ts", now)
    except Exception:
        pass
    out.update(status="ok", qty=qty)
    _notify(db, f"exit:{sym}:{kind}:{int(now)}",
            f"✅ Auto exit — {sym} ({kind})",
            f"{decision['reason']} — market sold {qty:.6f} @ ~{decision['price']}"
            f"{relist_note}. PnL {decision.get('pnl_pct', 0.0):+.2f}% after "
            f"{decision.get('held_hours', 0.0):.1f}h.")
    return out


# ---- orchestrator entry -----------------------------------------------------
def run_exit_step(client, db, *, now: Optional[float] = None) -> List[Dict]:
    """_step_exit_positions entry: mode gate -> evaluate -> auto kinds execute
    (unless mode=notify) -> notify kinds always notify only. Fail-open."""
    now = now or time.time()
    mode = _mode(db)
    if mode == "off":
        # WO-1004 (1): a silent skip here cost the 13:20 0G TP (+11.35% ->
        # +6.8% by the time a human found it). The kill-switch is honoured
        # — but never silently: warn every round, and surface WHAT the
        # switch is holding back (read-only evaluate, fail-open).
        logger.warning(
            "exit step: KILL-SWITCH exit:mode=%r — auto exits SKIPPED", mode)
        try:
            held_back = evaluate_exits(client, db, now=now)
            for d in held_back:
                logger.warning(
                    "exit step: would-exit BLOCKED by kill-switch: %s %s "
                    "pnl=%+.2f%% held=%.1fh — %s",
                    d["symbol"], d["kind"], d.get("pnl_pct", 0.0),
                    d.get("held_hours", 0.0), d.get("reason", ""))
        except Exception:
            logger.warning(
                "exit step: would-exit probe failed while mode=off "
                "(non-fatal)", exc_info=True)
        return []
    try:
        decisions = evaluate_exits(client, db, now=now)
    except Exception:
        logger.warning("exit evaluate failed (non-fatal)", exc_info=True)
        return []
    results = []
    for d in decisions:
        if d["auto"] and mode == "auto":
            r = execute_exit(client, db, d, now=now)
            results.append(r)
        else:
            if d["auto"] and mode == "notify":
                # WO-1004 (1): notify mode holding back an auto-kind exit
                # is decision-relevant state — warn, then notify as before.
                logger.warning(
                    "exit step: exit:mode=notify — auto kind %s %s held "
                    "back (pnl=%+.2f%% held=%.1fh)",
                    d["kind"], d["symbol"], d.get("pnl_pct", 0.0),
                    d.get("held_hours", 0.0))
            _notify(db, f"exit:{d['symbol']}:{d['kind']}:{int(now)}",
                    f"🔔 Exit signal ({'notify-only' if mode == 'notify' else d['kind']}) — {d['symbol']}",
                    f"{d['reason']} — pnl {d['pnl_pct']:+.2f}%, held {d['held_hours']}h. "
                    f"Auto-execution {'off (exit:mode=notify)' if mode == 'notify' else 'disabled for this kind'}.")
            results.append({"symbol": d["symbol"], "kind": d["kind"],
                            "status": "notified"})
        if results and results[-1].get("status") == "ok":
            logger.info("exit step: AUTO EXIT %s %s — %s",
                        d["symbol"], d["kind"], d["reason"])
    return results
