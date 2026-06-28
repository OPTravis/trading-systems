import logging

logger = logging.getLogger(__name__)

"""
Fund Flow Audit — Reconstruct complete PnL from Binance trade history.

Pulls ALL spot trades via Binance API, computes FIFO realized PnL per symbol
and per month, tracks commission costs, and reports open dust positions.

Usage:
    python -m src.fund_flow_audit [--json] [--since YYYY-MM-DD]
"""
import argparse
import os
import sys
import hmac
import hashlib
import time
import json
import requests
from collections import defaultdict, deque
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BASE_URL = "https://api.binance.com"

# Symbols to query (system active universe + historical)
SCAN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "NEARUSDT", "APTUSDT", "OPUSDT", "ARBUSDT", "INJUSDT", "SUIUSDT",
    "WLDUSDT", "UNIUSDT", "ALLOUSDT",
]

# BNB price estimate for commission conversion (updated lazily)
_BNB_PRICE_USDT = 600.0


def _sign(params: dict) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def _headers() -> dict:
    return {"X-MBX-APIKEY": API_KEY}


def _comm_to_usdt(comm: float, asset: str, trade_price: float) -> float:
    """Convert commission to USDT equivalent."""
    if asset == "USDT":
        return comm
    if asset == "BNB":
        return comm * _BNB_PRICE_USDT
    # Commission paid in the traded coin
    return comm * trade_price


def fetch_all_trades(since_ts: int = 0) -> list[dict]:
    """Fetch all spot trades from Binance."""
    start = since_ts if since_ts else 1716192000000  # default: 2024-05-20
    all_trades = []
    for sym in SCAN_SYMBOLS:
        ts = int(time.time() * 1000)
        qs = _sign({"symbol": sym, "startTime": start, "timestamp": ts, "limit": 1000})
        try:
            r = requests.get(
                f"{BASE_URL}/api/v3/myTrades?{qs}",
                headers=_headers(),
                timeout=15,
            )
            if r.status_code == 200 and r.json():
                all_trades.extend(r.json())
        except Exception as e:
            logger.warning("fund_flow_audit.fetch_all_trades: " + str(e))
    all_trades.sort(key=lambda x: x["time"])
    return all_trades


def get_current_prices() -> dict[str, float]:
    """Get current USDT prices for all symbols."""
    prices = {}
    try:
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price", timeout=10)
        if r.status_code == 200:
            for item in r.json():
                prices[item["symbol"]] = float(item["price"])
    except Exception as e:
        logger.warning("fund_flow_audit.get_current_prices: " + str(e))
    return prices


def compute_fifo_pnl(trades: list[dict], prices: dict[str, float]):
    """
    Compute FIFO realized PnL, commission, unrealized PnL, and monthly breakdown.

    Returns dict with:
        per_symbol_realized, per_month, total_commission,
        unrealized_positions, totals
    """
    positions = defaultdict(lambda: deque())  # sym -> deque[(qty, price)]
    per_symbol_realized = defaultdict(float)
    per_symbol_comm = defaultdict(float)
    per_month = defaultdict(lambda: {"pnl": 0.0, "comm": 0.0, "trades": 0})
    total_commission = 0.0

    for t in trades:
        sym = t["symbol"]
        qty = float(t["qty"])
        price = float(t["price"])
        comm = float(t["commission"])
        comm_asset = t["commissionAsset"]
        is_buy = t["isBuyer"]
        dt = datetime.fromtimestamp(t["time"] / 1000, tz=timezone.utc)
        month_key = dt.strftime("%Y-%m")

        comm_usdt = _comm_to_usdt(comm, comm_asset, price)
        total_commission += comm_usdt
        per_symbol_comm[sym] += comm_usdt
        per_month[month_key]["comm"] += comm_usdt
        per_month[month_key]["trades"] += 1

        if is_buy:
            positions[sym].append((qty, price))
        else:
            remaining = qty
            pnl = 0.0
            while remaining > 0 and positions[sym]:
                lot_qty, lot_price = positions[sym][0]
                if lot_qty <= remaining:
                    pnl += lot_qty * (price - lot_price)
                    remaining -= lot_qty
                    positions[sym].popleft()
                else:
                    pnl += remaining * (price - lot_price)
                    positions[sym][0] = (lot_qty - remaining, lot_price)
                    remaining = 0
            per_symbol_realized[sym] += pnl
            per_month[month_key]["pnl"] += pnl

    # Compute unrealized positions
    unrealized_positions = []
    total_unrealized = 0.0
    total_position_value = 0.0
    for sym, lots in positions.items():
        total_qty = sum(q for q, _ in lots)
        if total_qty < 1e-8:
            continue
        avg_buy = sum(q * p for q, p in lots) / total_qty if total_qty > 0 else 0
        cur_price = prices.get(sym, avg_buy)
        value = total_qty * cur_price
        unrealized = total_qty * (cur_price - avg_buy)
        cost = total_qty * avg_buy
        total_unrealized += unrealized
        total_position_value += value
        unrealized_positions.append({
            "symbol": sym,
            "qty": round(total_qty, 8),
            "avg_buy_price": round(avg_buy, 6),
            "current_price": round(cur_price, 6),
            "cost_usd": round(cost, 4),
            "value_usd": round(value, 4),
            "unrealized_pnl": round(unrealized, 4),
        })

    total_realized = sum(per_symbol_realized.values())

    return {
        "per_symbol_realized": dict(per_symbol_realized),
        "per_symbol_commission": dict(per_symbol_comm),
        "per_month": {k: dict(v) for k, v in per_month.items()},
        "total_commission": round(total_commission, 4),
        "total_realized_pnl": round(total_realized, 4),
        "net_realized": round(total_realized - total_commission, 4),
        "unrealized_positions": unrealized_positions,
        "total_unrealized": round(total_unrealized, 4),
        "total_position_value": round(total_position_value, 4),
        "num_trades": len(trades),
    }


def generate_report(audit_data: dict) -> str:
    """Generate a human-readable markdown report."""
    lines = []
    lines.append("# 💰 Fund Flow Audit Report")
    lines.append(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n")

    lines.append("## Summary\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total Trades | {audit_data['num_trades']} |")
    lines.append(f"| Realized PnL | ${audit_data['total_realized_pnl']:.2f} |")
    lines.append(f"| Commission | -${audit_data['total_commission']:.2f} |")
    lines.append(f"| Net Realized | ${audit_data['net_realized']:.2f} |")
    lines.append(f"| Unrealized PnL | ${audit_data['total_unrealized']:.2f} |")
    lines.append(f"| Open Position Value | ${audit_data['total_position_value']:.2f} |")
    lines.append("")

    lines.append("## Per-Symbol Realized PnL\n")
    lines.append("| Symbol | Realized PnL | Commission | Net |")
    lines.append("|--------|-------------|------------|-----|")
    for sym in sorted(audit_data["per_symbol_realized"].keys()):
        pnl = audit_data["per_symbol_realized"][sym]
        comm = audit_data["per_symbol_commission"].get(sym, 0)
        lines.append(f"| {sym} | ${pnl:.2f} | ${comm:.2f} | ${pnl - comm:.2f} |")
    lines.append("")

    lines.append("## Monthly Breakdown\n")
    lines.append("| Month | Trades | Realized PnL | Commission | Net |")
    lines.append("|-------|--------|-------------|------------|-----|")
    for month in sorted(audit_data["per_month"].keys()):
        m = audit_data["per_month"][month]
        net = m["pnl"] - m["comm"]
        lines.append(f"| {month} | {m['trades']} | ${m['pnl']:.2f} | ${m['comm']:.2f} | ${net:.2f} |")
    lines.append("")

    if audit_data["unrealized_positions"]:
        lines.append("## Open Positions (FIFO)\n")
        lines.append("| Symbol | Qty | Avg Buy | Current | Cost | Value | Unrealized |")
        lines.append("|--------|-----|---------|---------|------|-------|------------|")
        for pos in sorted(audit_data["unrealized_positions"],
                          key=lambda x: abs(x["unrealized_pnl"]), reverse=True):
            lines.append(
                f"| {pos['symbol']} | {pos['qty']:.6f} | ${pos['avg_buy_price']:.4f} | "
                f"${pos['current_price']:.4f} | ${pos['cost_usd']:.2f} | "
                f"${pos['value_usd']:.2f} | ${pos['unrealized_pnl']:.2f} |"
            )
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Fund Flow Audit")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of markdown")
    parser.add_argument("--since", type=str, default=None, help="Start date YYYY-MM-DD")
    args = parser.parse_args()

    if not API_KEY or not API_SECRET:
        print("ERROR: BINANCE_API_KEY/SECRET not set", file=sys.stderr)
        sys.exit(1)

    since_ts = 0
    if args.since:
        dt = datetime.strptime(args.since, "%Y-%m-%d")
        since_ts = int(dt.timestamp() * 1000)

    trades = fetch_all_trades(since_ts)
    if not trades:
        print("No trades found", file=sys.stderr)
        sys.exit(0)

    prices = get_current_prices()
    audit_data = compute_fifo_pnl(trades, prices)

    if args.json:
        print(json.dumps(audit_data, indent=2))
    else:
        print(generate_report(audit_data))


if __name__ == "__main__":
    main()
