#!/usr/bin/env python3
"""
Data Source Health Dashboard for crypto-ai-trader
Quick overview of all data sources and their health status.
Run manually or via cron for monitoring.
"""

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / "crypto-ai-trader"))

from src.state_db import get_state_db
from src.binance_client import BinanceClient

DATA_DIR = Path.home() / "crypto-ai-trader" / "data"
DB_PATH = DATA_DIR / "state.db"


def get_db_stats() -> dict:
    """Get SQLite database statistics."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        stats = {}
        for table in ["portfolio", "trailing_stop", "risk_guard", "trades", "kv"]:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]

        # DB file size
        db_size = DB_PATH.stat().st_size

        conn.close()
        return {
            "ok": True,
            "tables": stats,
            "db_size_kb": round(db_size / 1024, 1),
            "db_path": str(DB_PATH),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_portfolio_summary() -> dict:
    """Get current portfolio summary from SQLite."""
    try:
        db = get_state_db()
        positions = db.portfolio_get_all()

        total_value = 0
        pos_list = []
        for sym, data in positions.items():
            try:
                client = BinanceClient(testnet=False)
                stats = client.get_24hr_stats(sym)
                price = float(stats.get("last_price", 0))
                value = data["quantity"] * price
                total_value += value
                pos_list.append({
                    "symbol": sym,
                    "qty": round(data["quantity"], 4),
                    "entry": round(data["entry_price"], 6),
                    "price": round(price, 6),
                    "value": round(value, 2),
                    "pnl_pct": round(((price - data["entry_price"]) / data["entry_price"]) * 100, 2) if data["entry_price"] > 0 else 0,
                })
            except Exception:
                pass

        return {
            "ok": True,
            "count": len(positions),
            "total_value": round(total_value, 2),
            "positions": pos_list,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_risk_status() -> dict:
    """Get risk management status."""
    try:
        db = get_state_db()

        # Trailing stops
        ts = db.ts_get_all()
        active_ts = [s for s, d in ts.items() if d.get("activated")]

        # Loss guard
        rg = db.risk_get()

        return {
            "ok": True,
            "trailing_stops": {
                "total": len(ts),
                "active": len(active_ts),
                "symbols": list(ts.keys()),
            },
            "loss_guard": {
                "streak": rg.get("streak", 0),
                "last_reset": rg.get("last_reset"),
            },
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_recent_trades(limit: int = 5) -> dict:
    """Get recent trade history from trade_outcomes (has real PnL data)."""
    try:
        db = get_state_db()
        conn = db._get_conn()
        rows = conn.execute(
            """SELECT symbol, entry_price, exit_price, net_pnl_pct, strategy, exit_reason, status
               FROM trade_outcomes ORDER BY entry_time DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        trades = [
            {"symbol": r[0], "entry_price": r[1], "exit_price": r[2],
             "pnl": r[3], "strategy": r[4], "exit_reason": r[5], "status": r[6]}
            for r in rows
        ]
        return {
            "ok": True,
            "count": len(trades),
            "trades": trades,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_trade_outcomes_summary() -> dict:
    """Get trade outcomes summary stats."""
    try:
        db = get_state_db()
        conn = db._get_conn()
        row = conn.execute("""
            SELECT COUNT(*), SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END),
                   SUM(net_pnl_absolute), AVG(net_pnl_pct),
                   MAX(net_pnl_pct), MIN(net_pnl_pct)
            FROM trade_outcomes WHERE status='closed'
        """).fetchone()
        total, wins, total_pnl, avg_pnl, best, worst = row
        return {
            "ok": True,
            "total": total or 0,
            "wins": wins or 0,
            "losses": (total or 0) - (wins or 0),
            "win_rate": (wins / total * 100) if total else 0,
            "total_pnl": total_pnl or 0,
            "avg_pnl": avg_pnl or 0,
            "best": best or 0,
            "worst": worst or 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_strategy_breakdown() -> dict:
    """Get per-strategy performance."""
    try:
        db = get_state_db()
        conn = db._get_conn()
        rows = conn.execute("""
            SELECT strategy, COUNT(*), AVG(net_pnl_pct),
                   SUM(CASE WHEN is_win=1 THEN 1 ELSE 0 END)
            FROM trade_outcomes WHERE status='closed'
            GROUP BY strategy
        """).fetchall()
        strategies = []
        for r in rows:
            strategies.append({
                "name": r[0], "trades": r[1],
                "avg_pnl": r[2] or 0,
                "win_rate": (r[3] / r[1] * 100) if r[1] > 0 else 0,
            })
        return {"ok": True, "strategies": strategies}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_factor_weights() -> dict:
    """Get current factor weights (learned or default)."""
    try:
        from src.online_learner import DEFAULT_WEIGHTS
        db = get_state_db()
        conn = db._get_conn()
        row = conn.execute("SELECT value FROM kv WHERE key='learned_factor_weights'").fetchone()
        if row:
            weights = json.loads(row[0])
            source = "learned"
        else:
            weights = DEFAULT_WEIGHTS
            source = "default"
        return {"ok": True, "source": source, "weights": weights}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_paper_trading_status() -> dict:
    """Get paper trading status."""
    try:
        db = get_state_db()
        conn = db._get_conn()
        trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        portfolio = conn.execute("SELECT COUNT(*) FROM paper_portfolio").fetchone()[0]
        closed = conn.execute("SELECT COUNT(*) FROM paper_trades WHERE status='filled'").fetchone()[0]
        return {"ok": True, "trades": trades, "closed": closed, "portfolio": portfolio}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_kv_highlights() -> dict:
    """Get key KV store values."""
    try:
        db = get_state_db()
        conn = db._get_conn()
        highlights = {}
        for key in ["cash_balance", "circuit_breaker:state", "daily_loss_breaker:state",
                     "hmm_regime", "strategy_weights"]:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if row:
                try:
                    highlights[key] = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    highlights[key] = row[0]
        return {"ok": True, "highlights": highlights}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def generate_dashboard() -> str:
    """Generate a formatted dashboard report."""
    db_stats = get_db_stats()
    portfolio = get_portfolio_summary()
    risk = get_risk_status()
    trades = get_recent_trades()
    outcomes = get_trade_outcomes_summary()
    strategies = get_strategy_breakdown()
    factors = get_factor_weights()
    paper = get_paper_trading_status()
    kv = get_kv_highlights()

    lines = [
        "📊 Crypto Data Source Health Dashboard",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "═" * 50,
        "📁 DATABASE",
        "═" * 50,
    ]

    if db_stats["ok"]:
        lines.append(f"  Path: {db_stats['db_path']}")
        lines.append(f"  Size: {db_stats['db_size_kb']} KB")
        lines.append(f"  Tables:")
        for table, count in db_stats["tables"].items():
            lines.append(f"    • {table}: {count} rows")
    else:
        lines.append(f"  ❌ Error: {db_stats.get('error', 'Unknown')}")

    lines.extend([
        "",
        "═" * 50,
        "💼 PORTFOLIO",
        "═" * 50,
    ])

    if portfolio["ok"]:
        lines.append(f"  Positions: {portfolio['count']}")
        lines.append(f"  Total Value: ${portfolio['total_value']}")
        lines.append(f"  Holdings:")
        for pos in portfolio["positions"]:
            emoji = "🟢" if pos["pnl_pct"] >= 0 else "🔴"
            lines.append(
                f"    {emoji} {pos['symbol']}: {pos['qty']} @ ${pos['entry']} → ${pos['price']} | "
                f"Value: ${pos['value']} | PnL: {pos['pnl_pct']}%"
            )
    else:
        lines.append(f"  ❌ Error: {portfolio.get('error', 'Unknown')}")

    lines.extend([
        "",
        "═" * 50,
        "🛡️ RISK MANAGEMENT",
        "═" * 50,
    ])

    if risk["ok"]:
        ts = risk["trailing_stops"]
        lines.append(f"  Trailing Stops: {ts['total']} total, {ts['active']} active")
        if ts["symbols"]:
            lines.append(f"    Symbols: {', '.join(ts['symbols'])}")
        rg = risk["loss_guard"]
        lines.append(f"  Loss Streak: {rg['streak']}")
    else:
        lines.append(f"  ❌ Error: {risk.get('error', 'Unknown')}")

    lines.extend([
        "",
        "═" * 50,
        "📜 RECENT TRADES",
        "═" * 50,
    ])

    if trades["ok"]:
        if trades["trades"]:
            for t in trades["trades"]:
                pnl = t.get("pnl") or 0
                emoji = "🟢" if pnl >= 0 else "🔴"
                status = t.get("status", "?")
                entry = t.get("entry_price") or 0
                exit_p = t.get("exit_price") or 0
                reason = t.get("exit_reason") or ""
                lines.append(
                    f"  {emoji} {t.get('symbol', '?')} | "
                    f"Entry: ${entry:.4f} → Exit: ${exit_p:.4f} | "
                    f"PnL: {pnl:+.2f}% | {status} {reason}"
                )
        else:
            lines.append("  No trades recorded")
    else:
        lines.append(f"  ❌ Error: {trades.get('error', 'Unknown')}")

    # Trade Outcomes Summary
    lines.extend(["", "═" * 50, "📈 TRADE OUTCOMES SUMMARY", "═" * 50])
    if outcomes["ok"]:
        lines.append(f"  Total: {outcomes['total']} | Wins: {outcomes['wins']} | Losses: {outcomes['losses']}")
        lines.append(f"  Win Rate: {outcomes['win_rate']:.1f}%")
        lines.append(f"  Total PnL: ${outcomes['total_pnl']:.2f} | Avg: {outcomes['avg_pnl']:.2f}%")
        lines.append(f"  Best: {outcomes['best']:.2f}% | Worst: {outcomes['worst']:.2f}%")
    else:
        lines.append(f"  ❌ Error: {outcomes.get('error', 'Unknown')}")

    # Strategy Breakdown
    lines.extend(["", "═" * 50, "🎯 STRATEGY BREAKDOWN", "═" * 50])
    if strategies["ok"]:
        for s in strategies["strategies"]:
            emoji = "🟢" if s["avg_pnl"] >= 0 else "🔴"
            lines.append(f"  {emoji} {s['name']}: {s['trades']} trades, WR={s['win_rate']:.0f}%, Avg PnL={s['avg_pnl']:.2f}%")
    else:
        lines.append(f"  ❌ Error: {strategies.get('error', 'Unknown')}")

    # Factor Weights
    lines.extend(["", "═" * 50, "⚖️ FACTOR WEIGHTS", "═" * 50])
    if factors["ok"]:
        lines.append(f"  Source: {factors['source']}")
        for k, v in sorted(factors["weights"].items(), key=lambda x: -x[1]):
            lines.append(f"    {k}: {v}")
    else:
        lines.append(f"  ❌ Error: {factors.get('error', 'Unknown')}")

    # Paper Trading
    lines.extend(["", "═" * 50, "📝 PAPER TRADING", "═" * 50])
    if paper["ok"]:
        lines.append(f"  Trades: {paper['trades']} | Filled: {paper['closed']} | Portfolio: {paper['portfolio']} positions")
    else:
        lines.append(f"  ❌ Error: {paper.get('error', 'Unknown')}")

    # KV Highlights
    lines.extend(["", "═" * 50, "🔑 KEY VALUES", "═" * 50])
    if kv["ok"]:
        for key, val in kv["highlights"].items():
            if isinstance(val, dict):
                lines.append(f"  {key}:")
                for k2, v2 in val.items():
                    lines.append(f"    {k2}: {v2}")
            else:
                lines.append(f"  {key}: {val}")
    else:
        lines.append(f"  ❌ Error: {kv.get('error', 'Unknown')}")

    lines.extend(["", "═" * 50, "✅ Dashboard Complete", "═" * 50])
    return "\n".join(lines)


if __name__ == "__main__":
    print(generate_dashboard())
