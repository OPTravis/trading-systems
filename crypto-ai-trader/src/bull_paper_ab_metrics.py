"""P0-C: A/B daily metrics + comparison. Computes per-group stats from
paper_bull_positions / paper_bull_trades and snapshots them into
paper_bull_ab_daily. Crypto is 24/7 so Sharpe uses sqrt(365) on daily returns.

WO-0924-z2 P6-B1: raw SQL moved to BullPaperStore; this module keeps the
analytics math (win/PF/hold distribution, SL-sweep windows, re-entry
churn, MaxDD, Sharpe) and the snapshot/report orchestration.
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .bull_paper_store import BullPaperStore

HOURS_MS = 3600 * 1000


def _closed_trades(db, group: str, days: int = 30) -> List[Dict]:
    since = int((time.time() - days * 86400) * 1000)
    return BullPaperStore(db).closed_positions_since(group, since)


def _daily_returns(db, group: str, days: int = 30) -> List[float]:
    """Build daily return series from per-day equity snapshots; fall back to
    realized PnL / start_cash if no snapshots exist."""
    rows = BullPaperStore(db).daily_equity_series(group)
    if len(rows) >= 2:
        rets = []
        for i in range(1, len(rows)):
            prev = rows[i - 1]["equity"]
            cur = rows[i]["equity"]
            if prev and prev > 0:
                rets.append((cur - prev) / prev)
        return rets[-days:]
    return []


def compute_group_stats(db, group: str, start_cash: float,
                        prices: Optional[Dict[str, float]] = None,
                        days: int = 30) -> Dict[str, Any]:
    store = BullPaperStore(db)
    trades = _closed_trades(db, group, days=days)
    wins = [t for t in trades if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("realized_pnl") or 0) <= 0]
    gross_profit = sum(t["realized_pnl"] for t in wins)
    gross_loss = abs(sum(t["realized_pnl"] for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0)
    win_rate = len(wins) / len(trades) if trades else 0.0

    holds_h = [t["hold_seconds"] / 3600 for t in trades
               if t.get("hold_seconds") and t["hold_seconds"] > 0]
    avg_hold = statistics.mean(holds_h) if holds_h else 0.0
    med_hold = statistics.median(holds_h) if holds_h else 0.0
    min_hold = min(holds_h) if holds_h else 0.0
    max_hold = max(holds_h) if holds_h else 0.0

    # SL sweep = SL exits within 8h of entry (stop run before thesis played out)
    sl_sweeps = 0
    for t in trades:
        if t.get("exit_time") and t.get("entry_time") and t.get("notes", ""):
            held_h = (t["exit_time"] - t["entry_time"]) / HOURS_MS
            if held_h < 8 and ("SL" in (t.get("notes") or "") or "SL" in str(t.get("exit_price"))):
                sl_sweeps += 1
    # more robust: use trades table details for B_ATR_SL / SL_HIT under 8h
    rr = store.sl_sweep_rows(group)
    sl_sweeps = sum(
        1 for r in rr
        if r["entry_time"] and r["exit_time"]
        and (r["exit_time"] - r["entry_time"]) / HOURS_MS < 8
    )
    sl_sweep_rate = sl_sweeps / len(trades) if trades else 0.0

    # equity + MaxDD
    cash_key = "cash_balance" if group == "A" else f"cash_balance_{group}"
    cash_val = store.state_get(cash_key)
    cash = float(cash_val) if cash_val else 0.0
    mv = 0.0
    if prices:
        opens = store.open_position_mv_rows(group)
        for o in opens:
            px = prices.get(o["symbol"], o["entry_price"])
            mv += o["quantity"] * px
    equity = cash + mv

    # MaxDD over realized equity curve (approximation using closed trades)
    realized = 0.0
    peak = start_cash
    max_dd = 0.0
    for t in trades:
        realized += t.get("realized_pnl") or 0
        cur_eq = start_cash + realized
        peak = max(peak, cur_eq)
        dd = (cur_eq - peak) / peak if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
    # include open MV in current DD
    if prices:
        cur_eq = equity
        peak = max(peak, cur_eq)
        max_dd = min(max_dd, (cur_eq - peak) / peak if peak > 0 else 0.0)

    # Sharpe from daily snapshots
    rets = _daily_returns(db, group, days)
    sharpe = 0.0
    if len(rets) >= 2:
        sd = statistics.pstdev(rets)
        if sd > 0:
            sharpe = (statistics.mean(rets) / sd) * math.sqrt(365)

    n_open = store.count_open_positions(group)

    # P0-C review: re-entry churn = SL close followed by re-open of same
    # symbol within 4h (P1 cooldown trigger if >3 over the 14d window)
    reentry_after_sl_count = 0
    REENTRY_WIN = 4 * HOURS_MS
    # only SL closes count (Leo 2026-08-26: "同一幣 SL 後 4h 內 re-open")
    for sym in store.distinct_position_symbols(group):
        for exit_time in store.sl_exit_times(group, sym):
            if store.reentry_buy_exists(group, sym, exit_time,
                                        exit_time + REENTRY_WIN):
                reentry_after_sl_count += 1

    # P0-C review: core SL count (BTC/SOL core thesis stops — high-signal events)
    core_sl = store.core_sl_count(group)

    return {
        "cash": cash, "market_value": mv, "equity": equity,
        "total_return": (equity - start_cash) / start_cash if start_cash else 0,
        "n_trades": len(trades), "n_wins": len(wins), "win_rate": win_rate,
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "profit_factor": pf if pf != float("inf") else 99.99,
        "avg_hold_hours": avg_hold, "median_hold_hours": med_hold,
        "min_hold_hours": min_hold, "max_hold_hours": max_hold,
        "sl_sweep_count": sl_sweeps, "sl_sweep_rate": sl_sweep_rate,
        "max_drawdown": max_dd, "sharpe": sharpe,
        "n_open": n_open,
        "reentry_after_sl_count": reentry_after_sl_count,
        "core_sl_count": core_sl,
    }


def snapshot_daily(db, prices: Dict[str, float], a_start: float, b_start: float,
                   kelly_f: float = 0.0, kelly_tstat: float = 0.0,
                   grid_active: int = 0, exploration: int = 0,
                   whipsaw: int = 0):
    """Persist today's A/B snapshot (one row per group)."""
    store = BullPaperStore(db)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for group, sc in (("A", a_start), ("B", b_start)):
        st = compute_group_stats(db, group, sc, prices=prices)
        store.upsert_ab_daily(
            snapshot_date=today, ab_group=group, start_cash=sc,
            cash=st["cash"], market_value=st["market_value"],
            equity=st["equity"], total_return=st["total_return"],
            n_trades=st["n_trades"], n_wins=st["n_wins"],
            win_rate=st["win_rate"], gross_profit=st["gross_profit"],
            gross_loss=st["gross_loss"], profit_factor=st["profit_factor"],
            sharpe=st["sharpe"], max_drawdown=st["max_drawdown"],
            avg_hold_hours=st["avg_hold_hours"],
            median_hold_hours=st["median_hold_hours"],
            min_hold_hours=st["min_hold_hours"],
            max_hold_hours=st["max_hold_hours"],
            sl_sweep_count=st["sl_sweep_count"],
            sl_sweep_rate=st["sl_sweep_rate"], whipsaw_count=whipsaw,
            kelly_f=kelly_f, kelly_tstat=kelly_tstat,
            grid_active_count=grid_active, exploration_count=exploration,
            n_open=st["n_open"],
            # WO-0924-z2 P6-B1: the 30-col INSERT previously supplied
            # only 28 bindings — snapshot_daily crashed on any DB with
            # the full schema (fresh deployments hit this immediately).
            reentry_after_sl_count=st["reentry_after_sl_count"],
            core_sl_count=st["core_sl_count"])
    return today


def b_activity_warning(db, b_start: float) -> str:
    """P0-C review: after 3+ days of B running, if B has 0 trades AND 0 open
    positions, flag that the B filters may be too strict (likely RVOL 1.2) so
    we don't wait 14 days to discover there's no comparison data."""
    store = BullPaperStore(db)
    # days since B cash initialised
    updated_at = store.state_key_updated_at("cash_balance_B")
    n_closed = store.count_positions("closed", "B")
    n_open = store.count_positions("open", "B")
    if not updated_at:
        return ""
    days = (time.time() * 1000 - updated_at) / 86400_000
    if days >= 3 and n_closed == 0 and n_open == 0:
        # pull reject breakdown to suggest the binding constraint
        rows = store.top_reject_fail_filters("B", limit=3)
        top = ", ".join(f"{r['fail_filter']}={r['c']}" for r in rows) or "n/a"
        return (f"⚠️ B 組跑咗 {days:.1f} 日但 0 筆交易、0 倉位——過濾可能過嚴，"
                f"主要 reject: {top}。建議討論是否將 RVOL 1.2 降到 1.0-1.1。")
    return ""


def verify_ab_isolation(db) -> Dict[str, Any]:
    """P0-C protocol: daily check that A and B sleeves never cross-contaminate.
    Returns a dict with ok(bool) and any anomalies. (Integrity probes live in
    BullPaperStore.verify_isolation; see there for the anomaly catalogue.)"""
    return BullPaperStore(db).verify_isolation()


def format_ab_report(db, a_start: float, b_start: float,
                     prices: Dict[str, float]) -> str:
    """Human-readable A vs B comparison block for the scan report."""
    a = compute_group_stats(db, "A", a_start, prices=prices)
    b = compute_group_stats(db, "B", b_start, prices=prices)

    def pct(x):
        return f"{x*100:+.1f}%" if x is not None else "n/a"

    def pf(x):
        return f"{x:.2f}" if x and x < 99 else "∞" if x and x >= 99 else "—"

    lines = [
        "🧪 P0-C A/B 引擎對比",
        f"   {'':14}{'A (baseline)':>16}{'B (ATR/R-multi)':>18}",
        f"   {'Equity':14}{a['equity']:>15.2f}{b['equity']:>18.2f}",
        f"   {'Return':14}{pct(a['total_return']):>16}{pct(b['total_return']):>18}",
        f"   {'MaxDD':14}{pct(a['max_drawdown']):>16}{pct(b['max_drawdown']):>18}",
        f"   {'Sharpe(年化)':14}{a['sharpe']:>16.2f}{b['sharpe']:>18.2f}",
        f"   {'Trades':14}{a['n_trades']:>16}{b['n_trades']:>18}",
        f"   {'Win rate':14}{pct(a['win_rate']):>16}{pct(b['win_rate']):>18}",
        f"   {'Profit factor':14}{pf(a['profit_factor']):>16}{pf(b['profit_factor']):>18}",
        f"   {'Avg hold':14}{a['avg_hold_hours']:>14.1f}h{b['avg_hold_hours']:>16.1f}h",
        f"   {'Hold range':14}{(str(round(a['min_hold_hours'],1))+'-'+str(round(a['max_hold_hours'],1))+'h'):>16}{(str(round(b['min_hold_hours'],1))+'-'+str(round(b['max_hold_hours'],1))+'h'):>18}",
        f"   {'SL被掃(8h內)':14}{a['sl_sweep_count']:>16}{b['sl_sweep_count']:>18}",
        f"   {'Re-entry churn':14}{a['reentry_after_sl_count']:>16}{b['reentry_after_sl_count']:>18}",
        f"   {'Core SL':14}{a['core_sl_count']:>16}{b['core_sl_count']:>18}",
        f"   {'開倉中':14}{a['n_open']:>16}{b['n_open']:>18}",
    ]
    _w = b_activity_warning(db, b_start)
    if _w:
        lines.append("")
        lines.append(_w)
    return "\n".join(lines)
