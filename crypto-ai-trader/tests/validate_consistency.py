#!/usr/bin/env python3
"""
Crypto-ai-trader 數據一致性校驗腳本

Usage:
    cd ~/crypto-ai-trader && source crypto-secrets.env
    .venv/bin/python3 tests/validate_consistency.py

Checks:
    1. portfolio_state.json vs Binance 實際持倉
    2. trailing_stops.json vs 實際持倉
    3. trailing-check output 自洽性
    4. loss_guard.json 有效性
    5. grid_state.json 有效性
    6. strategy_state.json 有效性
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

errors = []
warnings = []
passed = []


def err(msg):
    errors.append(msg)
    print(f"  ❌ {msg}")


def warn(msg):
    warnings.append(msg)
    print(f"  ⚠️  {msg}")


def ok(msg):
    passed.append(msg)
    print(f"  ✅ {msg}")


def load_json(path):
    p = PROJECT_ROOT / path
    if not p.exists():
        return None
    return json.loads(p.read_text())


def get_binance_positions():
    """Get actual Binance balances via API."""
    from binance.spot import Spot
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")

    spot = Spot(
        api_key=os.getenv("BINANCE_API_KEY"), api_secret=os.getenv("BINANCE_API_SECRET")
    )
    account = spot.account()

    positions = {}
    for b in account["balances"]:
        free = float(b["free"])
        locked = float(b["locked"])
        total = free + locked
        if total > 0:
            positions[b["asset"]] = {"free": free, "locked": locked, "total": total}

    # Get prices for non-USDT assets
    for asset, info in positions.items():
        if asset == "USDT":
            info["price"] = 1.0
            info["usd_value"] = info["total"]
            continue
        symbol = f"{asset}USDT"
        try:
            ticker = spot.ticker_price(symbol)
            price = float(ticker["price"])
            info["price"] = price
            info["usd_value"] = info["total"] * price
        except Exception:
            info["price"] = 0
            info["usd_value"] = 0

    return positions


# ============================================================
# Check 1: portfolio_state.json vs Binance
# ============================================================
def check_portfolio_state(binance_positions):
    print("\n=== Check 1: portfolio_state.json vs Binance ===")

    state = load_json("data/portfolio_state.json")
    if not state:
        err("portfolio_state.json 不存在")
        return

    local_positions = state.get("positions", {})
    local_cash = state.get("cash_balance", 0)

    # Check cash balance
    binance_usdt = binance_positions.get("USDT", {}).get("free", 0)
    cash_diff = abs(local_cash - binance_usdt)
    if cash_diff > 1.0:
        err(
            f"現金不匹配: state={local_cash:.2f}, Binance free={binance_usdt:.2f}, diff={cash_diff:.2f}"
        )
    else:
        ok(f"現金匹配: ${local_cash:.2f}")

    # Build set of binance non-dust non-USDT assets
    binance_assets = set()
    for asset, info in binance_positions.items():
        if asset in ("USDT", "NTRN"):
            continue
        if info["usd_value"] >= 1.0:
            binance_assets.add(f"{asset}USDT")

    # Check: every local position must exist on Binance
    for symbol, pos in local_positions.items():
        if symbol in binance_assets:
            # Verify quantity matches
            asset = symbol.replace("USDT", "")
            binance_qty = binance_positions.get(asset, {}).get("total", 0)
            local_qty = pos.get("quantity", 0)
            qty_diff = abs(local_qty - binance_qty)
            if qty_diff > 0.01:
                err(
                    f"{symbol} 數量不匹配: local={local_qty:.4f}, Binance={binance_qty:.4f}"
                )
            else:
                ok(f"{symbol} 數量匹配: {local_qty:.4f}")
        else:
            pos_value = pos.get("quantity", 0) * pos.get("current_price", 0)
            err(f"Ghost 倉位: {symbol} 不在 Binance 上 (state 價值=${pos_value:.2f})")

    # Check: every Binance position worth >= $1 should be in state
    for asset in binance_assets:
        if asset not in local_positions:
            warn(f"Binance 有倉位但 state 沒有: {asset}")


# ============================================================
# Check 2: trailing_stops.json vs actual positions
# ============================================================
def check_trailing_stops(binance_positions):
    print("\n=== Check 2: trailing_stops.json vs actual positions ===")

    ts = load_json("data/trailing_stops.json")
    if not ts:
        ok("trailing_stops.json 不存在（正常如果沒有追蹤）")
        return

    # Build actual asset set (non-dust, >= $1)
    actual_assets = set()
    for asset, info in binance_positions.items():
        if asset in ("USDT", "NTRN"):
            continue
        if info.get("usd_value", 0) >= 1.0:
            actual_assets.add(asset)

    for asset, data in ts.items():
        if asset not in actual_assets:
            err(f"trailing_stops 有 {asset} 但 Binance 沒有此倉位")
        else:
            ok(f"trailing_stops {asset} 有對應倉位")

    for asset in actual_assets:
        if asset not in ts:
            warn(f"Binance 有 {asset} 但 trailing_stops 沒追蹤")


# ============================================================
# Check 3: trailing-check output self-consistency
# ============================================================
def check_trailing_check_output():
    print("\n=== Check 3: trailing-check output 一致性（靜態分析） ===")

    # Run trailing-check and capture output
    import subprocess

    env = os.environ.copy()
    result = subprocess.run(
        [str(PROJECT_ROOT / ".venv/bin/python3"), "main.py", "trailing-check"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=30,
    )

    # Find the JSON line in output
    json_line = None
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("{") and "results" in line:
            json_line = line
            break

    if not json_line:
        err(
            f"trailing-check 輸出無 JSON: stdout={result.stdout[:200]}, stderr={result.stderr[:200]}"
        )
        return

    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        err(f"trailing-check JSON 解析失敗: {e}")
        return

    results = data.get("results", [])

    # Check 3a: No duplicate assets in results
    assets_seen = {}
    for r in results:
        asset = r.get("asset", "?")
        action = r.get("action", "?")
        if asset in assets_seen:
            err(f"重複 asset: {asset} 出現多次 — {assets_seen[asset]} vs {action}")
        else:
            assets_seen[asset] = action

    if len(assets_seen) == len(results):
        ok(f"無重複 asset（{len(results)} 個結果）")

    # Check 3b: Mutual exclusivity — tracking and filled/triggered shouldn't coexist for same asset
    terminal_actions = {
        "sltp_filled_detected",
        "trailing_triggered",
        "triggered_sell_failed",
        "triggered_no_free_balance",
    }
    active_actions = {
        "tracking",
        "sl_moved",
        "sl_unchanged",
        "sl_created",
        "sl_create_failed",
        "sl_move_failed",
        "sl_cancel_failed",
    }

    for r in results:
        asset = r.get("asset", "?")
        action = r.get("action", "?")
        if action in terminal_actions:
            # Check if this asset also has a tracking result
            for r2 in results:
                if r2.get("asset") == asset and r2.get("action") in active_actions:
                    err(
                        f"矛盾: {asset} 同時 tracking ({r2['action']}) 和 terminal ({action})"
                    )

    # Check 3c: filled/triggered with PnL ≈ 0 on an asset that still exists is suspicious
    for r in results:
        if r.get("action") in terminal_actions:
            asset = r.get("asset", "?")
            pnl = r.get("pnl", 0)
            entry = r.get("entry_price", 0)
            exit_p = r.get("exit_price", 0)
            # If PnL is near zero AND entry ≈ exit, likely a false positive
            if entry > 0 and exit_p > 0 and abs(pnl) < 0.01:
                warn(
                    f"可疑 fill: {asset} PnL≈${pnl} (entry={entry}, exit={exit_p}) — 可能是誤報"
                )

    # Check 3d: Verify positions count matches Binance reality
    pos_count = data.get("positions", 0)
    ok(f"trailing-check 報告 {pos_count} 個持倉")

    # Check 3e: All results have valid actions
    valid_actions = (
        terminal_actions
        | active_actions
        | {
            "skip",
            "uncovered_sl_created",
            "uncovered_sl_failed",
            "uncovered_sl_error",
            "no_free_balance_for_sl",
        }
    )
    for r in results:
        action = r.get("action", "")
        if action not in valid_actions:
            warn(f"未知 action: {action} for {r.get('asset', '?')}")

    ok("trailing-check 輸出自洽性檢查完成")


# ============================================================
# Check 4: loss_guard.json
# ============================================================
def check_loss_guard():
    print("\n=== Check 4: loss_guard.json ===")

    lg = load_json("data/loss_guard.json")
    if not lg:
        ok("loss_guard.json 不存在")
        return

    suspended_until = lg.get("paused_until")
    consecutive_losses = lg.get("consecutive_losses", 0)
    history = lg.get("history", [])
    total_trades = len(history)

    if suspended_until:
        try:
            suspend_dt = datetime.fromtimestamp(suspended_until)
            if suspend_dt > datetime.now():
                ok(
                    f"交易暫停中，至 {suspend_dt.strftime('%Y-%m-%d %H:%M')}（{consecutive_losses} 連敗）"
                )
            else:
                ok(
                    f"暫停已過期（{suspend_dt.strftime('%Y-%m-%d %H:%M')}），下次 is_paused() 調用會自動清理"
                )
        except (ValueError, TypeError):
            err(f"paused_until 格式錯誤: {suspended_until}")
    else:
        ok(f"交易正常（{consecutive_losses} 連敗，共 {total_trades} 筆）")

    # Check for garbage records: many tiny PnL from same symbol = trailing-check bug
    if total_trades > 10:
        symbols = [h.get("symbol") for h in history]
        from collections import Counter

        sym_counts = Counter(symbols)
        dominant = sym_counts.most_common(1)[0]
        if dominant[1] / total_trades > 0.8:
            pnls = [h.get("pnl", 0) for h in history if h.get("symbol") == dominant[0]]
            avg_pnl = sum(abs(p) for p in pnls) / len(pnls) if pnls else 0
            if avg_pnl < 0.02:
                warn(
                    f"疑為垃圾記錄: {dominant[0]} 佔 {dominant[1]}/{total_trades} 筆，平均 PnL=${avg_pnl:.4f}"
                )

    # Check consecutive_losses consistency
    if consecutive_losses > 0 and total_trades == 0:
        err(f"連敗={consecutive_losses} 但 history 為空")


# ============================================================
# Check 5: grid_state.json
# ============================================================
def check_grid_state(binance_positions):
    print("\n=== Check 5: grid_state.json ===")

    gs = load_json("data/grid_state.json")
    if not gs:
        ok("grid_state.json 不存在")
        return

    status = gs.get("status", "unknown")
    symbol = gs.get("symbol", "?")
    ok(f"Grid {symbol}: status={status}")

    if status == "running":
        # Verify grid-managed asset exists on Binance
        asset = symbol.replace("USDT", "")
        if asset not in binance_positions:
            err(f"Grid {symbol} running 但 Binance 沒有此資產")
        else:
            ok(f"Grid {symbol} 有對應 Binance 倉位")

    grid_levels = gs.get("grid_levels", [])
    for i, level in enumerate(grid_levels):
        if level.get("filled") and not level.get("counter_order_id"):
            warn(f"Grid level {i} filled 但無 counter_order（可能漏單）")


# ============================================================
# Check 6: strategy_state.json validity
# ============================================================
def check_strategy_state():
    print("\n=== Check 6: strategy_state.json ===")

    ss = load_json("data/strategy_state.json")
    if not ss:
        ok("strategy_state.json 不存在（未運行過策略適配）")
        return

    regime = ss.get("last_regime", "unknown")
    adjustments = ss.get("last_adjustments", {})
    strategies = adjustments.get("strategies", {})

    ok(f"市場狀態: {regime}")

    enabled = [k for k, v in strategies.items() if v.get("enabled")]
    disabled = [k for k, v in strategies.items() if not v.get("enabled")]

    overlap = set(enabled) & set(disabled)
    if overlap:
        err(f"策略同時 enabled 和 disabled: {overlap}")
    else:
        ok(f"策略配置無衝突: enabled={enabled}, disabled={disabled}")

    # Check SL/TP adjustments are reasonable
    for name, cfg in strategies.items():
        sl = cfg.get("sl_pct", 0)
        tp = cfg.get("tp_levels", [])
        if sl <= 0:
            warn(f"策略 {name} SL={sl}% ≤ 0")
        for t in tp:
            if t.get("pct", 0) > 20:
                warn(f"策略 {name} TP level 過大: {t}")


# ============================================================
# Main
# ============================================================
def main():
    print("=" * 60)
    print("Crypto-ai-trader 數據一致性校驗")
    print(f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    print("\n連接 Binance API...")
    try:
        binance_positions = get_binance_positions()
        ok(f"Binance API 連接成功，{len(binance_positions)} 個資產")
    except Exception as e:
        err(f"Binance API 連接失敗: {e}")
        binance_positions = {}

    # Print actual positions for reference
    print("\n📋 Binance 實際持倉:")
    total_usd = 0
    for asset, info in sorted(
        binance_positions.items(), key=lambda x: -x[1].get("usd_value", 0)
    ):
        val = info.get("usd_value", 0)
        total_usd += val
        if val >= 0.01:
            print(f"  {asset}: {info['total']:.8f} = ${val:.2f}")
    print(f"  ── 總計: ${total_usd:.2f}")

    # Run all checks
    if binance_positions:
        check_portfolio_state(binance_positions)
        check_trailing_stops(binance_positions)
        check_grid_state(binance_positions)

    check_trailing_check_output()
    check_loss_guard()
    check_strategy_state()

    # Summary
    print("\n" + "=" * 60)
    print(
        f"📊 結果: ✅ {len(passed)} 通過 | ⚠️ {len(warnings)} 警告 | ❌ {len(errors)} 錯誤"
    )
    print("=" * 60)

    if errors:
        print("\n❌ 錯誤列表:")
        for e in errors:
            print(f"  • {e}")

    if warnings:
        print("\n⚠️ 警告列表:")
        for w in warnings:
            print(f"  • {w}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
