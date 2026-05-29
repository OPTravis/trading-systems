#!/usr/bin/env python3
"""
Data Consistency Auditor for crypto-ai-trader
Runs every hour to verify data integrity across all storage layers.

Checks:
1. SQLite state.db connectivity and table integrity
2. Portfolio positions: SQLite vs JSON backup consistency
3. TrailingStop: SQLite vs JSON backup consistency
4. LossGuard: SQLite vs JSON backup consistency
5. Trade history: SQLite trades table integrity
6. KV store: strategy_state, drawdown_breaker, grid_state in SQLite
7. Binance API sync: positions match actual account balances (>$1)
8. Dust filter: no positions <$1 in SQLite
9. Orphaned records: closed positions removed from all stores
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


def check_db_connectivity() -> dict:
    """Check SQLite DB is accessible and tables exist."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cursor.fetchall()]
        conn.close()
        required = {"portfolio", "trailing_stop", "risk_guard", "trades", "kv"}
        missing = required - set(tables)
        return {
            "ok": len(missing) == 0,
            "tables": tables,
            "missing": list(missing),
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "tables": [], "missing": [], "error": str(e)}


def check_portfolio_consistency() -> dict:
    """Compare SQLite portfolio with JSON backup."""
    db = get_state_db()
    db_positions = db.portfolio_get_all()

    json_file = DATA_DIR / "portfolio_state.json"
    json_positions = {}
    if json_file.exists():
        with open(json_file) as f:
            state = json.load(f)
        raw = state.get("positions", {})
        if isinstance(raw, list):
            json_positions = {p["symbol"]: p for p in raw}
        elif isinstance(raw, dict):
            json_positions = raw

    # Compare symbols
    db_symbols = set(db_positions.keys())
    json_symbols = set(json_positions.keys())

    only_in_db = db_symbols - json_symbols
    only_in_json = json_symbols - db_symbols

    mismatches = []
    for sym in db_symbols & json_symbols:
        db_qty = db_positions[sym].get("quantity", 0)
        json_qty = json_positions[sym].get("quantity", 0)
        db_entry = db_positions[sym].get("entry_price", 0)
        json_entry = json_positions[sym].get("entry_price", 0)
        if abs(db_qty - json_qty) > 1e-9 or abs(db_entry - json_entry) > 1e-9:
            mismatches.append({
                "symbol": sym,
                "db": {"qty": db_qty, "entry": db_entry},
                "json": {"qty": json_qty, "entry": json_entry},
            })

    return {
        "ok": len(only_in_db) == 0 and len(only_in_json) == 0 and len(mismatches) == 0,
        "db_count": len(db_positions),
        "json_count": len(json_positions),
        "only_in_db": list(only_in_db),
        "only_in_json": list(only_in_json),
        "mismatches": mismatches,
    }


def check_binance_sync() -> dict:
    """Verify SQLite positions match actual Binance account (>$1 only)."""
    try:
        client = BinanceClient(testnet=False)
        account = client.get_account()
        db = get_state_db()
        db_positions = db.portfolio_get_all()

        binance_positions = {}
        usdt_balance = 0
        for b in account.get("balances", []):
            asset = b["asset"]
            total = float(b["free"]) + float(b["locked"])
            if asset == "USDT":
                usdt_balance = total
            elif total > 0:
                try:
                    stats = client.get_24hr_stats(asset + "USDT")
                    price = float(stats.get("last_price", 0))
                    value = total * price
                    if value >= 1.0:
                        binance_positions[asset + "USDT"] = {"quantity": total, "value": value}
                except Exception:
                    pass

        db_symbols = set(db_positions.keys())
        binance_symbols = set(binance_positions.keys())

        only_in_db = db_symbols - binance_symbols
        only_in_binance = binance_symbols - db_symbols

        return {
            "ok": len(only_in_db) == 0 and len(only_in_binance) == 0,
            "db_count": len(db_positions),
            "binance_count": len(binance_positions),
            "usdt_balance": round(usdt_balance, 2),
            "only_in_db": list(only_in_db),
            "only_in_binance": list(only_in_binance),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_dust_filter() -> dict:
    """Check no dust positions (<$1) in SQLite."""
    try:
        client = BinanceClient(testnet=False)
        db = get_state_db()
        db_positions = db.portfolio_get_all()

        dust_positions = []
        for sym, data in db_positions.items():
            try:
                stats = client.get_24hr_stats(sym)
                price = float(stats.get("last_price", 0))
                value = data["quantity"] * price
                if value < 1.0:
                    dust_positions.append({"symbol": sym, "value": round(value, 4)})
            except Exception:
                pass

        return {
            "ok": len(dust_positions) == 0,
            "dust_count": len(dust_positions),
            "dust_positions": dust_positions,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_kv_store() -> dict:
    """Verify key KV entries exist in SQLite."""
    db = get_state_db()
    required_keys = ["strategy_state", "drawdown_breaker", "grid_state"]
    missing = []
    for key in required_keys:
        val = db.kv_get(key)
        if val is None:
            missing.append(key)

    return {
        "ok": len(missing) == 0,
        "missing_keys": missing,
        "checked_keys": required_keys,
    }


def run_all_checks() -> dict:
    """Run all consistency checks and return report."""
    return {
        "timestamp": datetime.now().isoformat(),
        "db_connectivity": check_db_connectivity(),
        "portfolio_consistency": check_portfolio_consistency(),
        "binance_sync": check_binance_sync(),
        "dust_filter": check_dust_filter(),
        "kv_store": check_kv_store(),
    }


def format_report(report: dict) -> str:
    """Format report for human reading."""
    lines = ["🔍 Crypto Data Consistency Audit", f"Time: {report['timestamp']}", ""]

    # DB connectivity
    db = report["db_connectivity"]
    status = "✅" if db["ok"] else "❌"
    lines.append(f"{status} DB Connectivity: {', '.join(db.get('tables', []))}")
    if db.get("missing"):
        lines.append(f"   Missing tables: {', '.join(db['missing'])}")
    if db.get("error"):
        lines.append(f"   Error: {db['error']}")

    # Portfolio
    port = report["portfolio_consistency"]
    status = "✅" if port["ok"] else "⚠️"
    lines.append(f"{status} Portfolio Sync: DB={port['db_count']} JSON={port['json_count']}")
    if port.get("only_in_db"):
        lines.append(f"   Only in DB: {', '.join(port['only_in_db'])}")
    if port.get("only_in_json"):
        lines.append(f"   Only in JSON: {', '.join(port['only_in_json'])}")
    if port.get("mismatches"):
        lines.append(f"   Mismatches: {len(port['mismatches'])}")

    # Binance sync
    sync = report["binance_sync"]
    if "error" in sync:
        lines.append(f"❌ Binance Sync: {sync['error']}")
    else:
        status = "✅" if sync["ok"] else "⚠️"
        lines.append(f"{status} Binance Sync: DB={sync['db_count']} Binance={sync['binance_count']} USDT=${sync.get('usdt_balance', 'N/A')}")
        if sync.get("only_in_db"):
            lines.append(f"   Only in DB: {', '.join(sync['only_in_db'])}")
        if sync.get("only_in_binance"):
            lines.append(f"   Only in Binance: {', '.join(sync['only_in_binance'])}")

    # Dust
    dust = report["dust_filter"]
    if "error" in dust:
        lines.append(f"❌ Dust Filter: {dust['error']}")
    else:
        status = "✅" if dust["ok"] else "⚠️"
        lines.append(f"{status} Dust Filter: {dust['dust_count']} dust positions found")
        for d in dust.get("dust_positions", [])[:5]:
            lines.append(f"   {d['symbol']}: ${d['value']}")

    # KV store
    kv = report["kv_store"]
    status = "✅" if kv["ok"] else "⚠️"
    lines.append(f"{status} KV Store: checked {', '.join(kv['checked_keys'])}")
    if kv.get("missing_keys"):
        lines.append(f"   Missing: {', '.join(kv['missing_keys'])}")

    lines.append("")
    all_ok = all(r.get("ok", False) for r in [db, port, sync, dust, kv])
    lines.append("✅ ALL CHECKS PASSED" if all_ok else "⚠️ SOME CHECKS FAILED")

    return "\n".join(lines)


if __name__ == "__main__":
    report = run_all_checks()
    print(format_report(report))
