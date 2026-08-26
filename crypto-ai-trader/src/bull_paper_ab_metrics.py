"""P0-C: A/B daily metrics + comparison. Computes per-group stats from
paper_bull_positions / paper_bull_trades and snapshots them into
paper_bull_ab_daily. Crypto is 24/7 so Sharpe uses sqrt(365) on daily returns."""
from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


HOURS_MS = 3600 * 1000


def _closed_trades(db, group: str, days: int = 30) -> List[Dict]:
    since = int((time.time() - days * 86400) * 1000)
    with db._get_conn() as c:
        rows = c.execute(
            """SELECT * FROM paper_bull_positions
               WHERE status='closed' AND COALESCE(ab_group,'A')=?
                 AND exit_time >= ? ORDER BY exit_time""",
            (group, since),
        ).fetchall()
    return [dict(r) for r in rows]


def _daily_returns(db, group: str, days: int = 30) -> List[float]:
    """Build daily return series from per-day equity snapshots; fall back to
    realized PnL / start_cash if no snapshots exist."""
    with db._get_conn() as c:
        rows = c.execute(
            """SELECT snapshot_date, equity FROM paper_bull_ab_daily
               WHERE ab_group=? ORDER BY snapshot_date""",
            (group,),
        ).fetchall()
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

    # SL sweep = SL exits within 8h of entry (stop run before thesis played out)
    sl_sweeps = 0
    for t in trades:
        if t.get("exit_time") and t.get("entry_time") and t.get("notes", ""):
            held_h = (t["exit_time"] - t["entry_time"]) / HOURS_MS
            if held_h < 8 and ("SL" in (t.get("notes") or "") or "SL" in str(t.get("exit_price"))):
                sl_sweeps += 1
    # more robust: use trades table details for B_ATR_SL / SL_HIT under 8h
    with db._get_conn() as c:
        rr = c.execute(
            """SELECT p.entry_time, p.exit_time, t.details
               FROM paper_bull_positions p
               JOIN paper_bull_trades t ON t.position_id=p.id
               WHERE p.status='closed' AND COALESCE(p.ab_group,'A')=?
                 AND t.action='SELL' AND t.details LIKE '%SL%'""",
            (group,),
        ).fetchall()
    sl_sweeps = sum(
        1 for r in rr
        if r["entry_time"] and r["exit_time"]
        and (r["exit_time"] - r["entry_time"]) / HOURS_MS < 8
    )
    sl_sweep_rate = sl_sweeps / len(trades) if trades else 0.0

    # equity + MaxDD
    with db._get_conn() as c:
        cash_row = c.execute(
            "SELECT value FROM paper_bull_state WHERE key=?",
            ("cash_balance" if group == "A" else f"cash_balance_{group}",),
        ).fetchone()
    cash = float(cash_row["value"]) if cash_row else 0.0
    mv = 0.0
    if prices:
        with db._get_conn() as c:
            opens = c.execute(
                "SELECT symbol, quantity, entry_price FROM paper_bull_positions "
                "WHERE status='open' AND COALESCE(ab_group,'A')=?",
                (group,),
            ).fetchall()
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

    n_open = 0
    with db._get_conn() as c:
        n_open = c.execute(
            "SELECT count(*) FROM paper_bull_positions WHERE status='open' "
            "AND COALESCE(ab_group,'A')=?", (group,)).fetchone()[0]

    return {
        "cash": cash, "market_value": mv, "equity": equity,
        "total_return": (equity - start_cash) / start_cash if start_cash else 0,
        "n_trades": len(trades), "n_wins": len(wins), "win_rate": win_rate,
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "profit_factor": pf if pf != float("inf") else 99.99,
        "avg_hold_hours": avg_hold, "median_hold_hours": med_hold,
        "sl_sweep_count": sl_sweeps, "sl_sweep_rate": sl_sweep_rate,
        "max_drawdown": max_dd, "sharpe": sharpe,
        "n_open": n_open,
    }


def snapshot_daily(db, prices: Dict[str, float], a_start: float, b_start: float,
                   kelly_f: float = 0.0, kelly_tstat: float = 0.0,
                   grid_active: int = 0, exploration: int = 0,
                   whipsaw: int = 0):
    """Persist today's A/B snapshot (one row per group)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for group, sc in (("A", a_start), ("B", b_start)):
        st = compute_group_stats(db, group, sc, prices=prices)
        with db._get_conn() as c:
            c.execute(
                """INSERT INTO paper_bull_ab_daily
                   (snapshot_date, ab_group, start_cash, cash, market_value,
                    equity, total_return, daily_return,
                    n_trades, n_wins, win_rate, gross_profit, gross_loss,
                    profit_factor, sharpe, max_drawdown,
                    avg_hold_hours, median_hold_hours,
                    sl_sweep_count, sl_sweep_rate, whipsaw_count,
                    kelly_f, kelly_tstat, grid_active_count,
                    exploration_count, n_open)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_date, ab_group) DO UPDATE SET
                     cash=excluded.cash, market_value=excluded.market_value,
                     equity=excluded.equity, total_return=excluded.total_return,
                     n_trades=excluded.n_trades, n_wins=excluded.n_wins,
                     win_rate=excluded.win_rate, gross_profit=excluded.gross_profit,
                     gross_loss=excluded.gross_loss,
                     profit_factor=excluded.profit_factor, sharpe=excluded.sharpe,
                     max_drawdown=excluded.max_drawdown,
                     avg_hold_hours=excluded.avg_hold_hours,
                     median_hold_hours=excluded.median_hold_hours,
                     sl_sweep_count=excluded.sl_sweep_count,
                     sl_sweep_rate=excluded.sl_sweep_rate,
                     whipsaw_count=excluded.whipsaw_count,
                     kelly_f=excluded.kelly_f, kelly_tstat=excluded.kelly_tstat,
                     grid_active_count=excluded.grid_active_count,
                     exploration_count=excluded.exploration_count,
                     n_open=excluded.n_open""",
                (today, group, sc, st["cash"], st["market_value"], st["equity"],
                 st["total_return"], 0.0,
                 st["n_trades"], st["n_wins"], st["win_rate"],
                 st["gross_profit"], st["gross_loss"], st["profit_factor"],
                 st["sharpe"], st["max_drawdown"],
                 st["avg_hold_hours"], st["median_hold_hours"],
                 st["sl_sweep_count"], st["sl_sweep_rate"], whipsaw,
                 kelly_f, kelly_tstat, grid_active, exploration, st["n_open"]),
            )
            c.commit()
    return today


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
        f"   {'SL被掃(8h內)':14}{a['sl_sweep_count']:>16}{b['sl_sweep_count']:>18}",
        f"   {'開倉中':14}{a['n_open']:>16}{b['n_open']:>18}",
    ]
    return "\n".join(lines)
