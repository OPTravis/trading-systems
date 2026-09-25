"""P4: PnLCalculator — single named implementation of every per-trade
pnl formula the system uses.

The Phase-1/z2 audit pinned SEVEN scattered per-trade pnl
implementations that differed in fee handling:

    portfolio.py:398          (price-entry)*qty          gross
    portfolio_pnl.py:32       (current-entry)*qty + pct  gross
    portfolio_reconciler.py   qty*(avg_px-entry_avg)     gross (weighted)
    cmd_trailing_check.py:575 (current-entry)*qty        gross
    cmd_trailing_check.py:1039 (exit-entry)*qty          gross
    paper_trader.py:543       (fill-entry)*qty - fee     net
    backtester.py:210         proceeds - entry_cost      net round-trip
    (+ bull_paper_portfolio.py close leg: proceeds - cost_basis, net)

P4 does NOT unify these into one formula — the differences are
deliberate (gross marks vs fee-adjusted economics; backtest ≈ live
reconciliation keeps its own fee rate). It gives each formula ONE
named implementation so the difference table is code, not convention.

Float-exactness contract: every function body is the ORIGINAL
expression, character-for-character, from the site it replaces —
`(exit - entry) * qty`, not `exit*qty - entry*qty`. Call sites keep
their own operand order, so migrated values are bit-identical.

Formula map (the difference table):
    gross_pnl(entry, exit, qty)         price-diff, no fees
    net_pnl(entry, exit, qty, fee)      price-diff minus ONE fee
    proceeds_net(px, qty, fee)          qty*px - fee (sell proceeds)
    pnl_pct(entry, exit)                percentage, entry<=0 -> 0.0
    weighted_entry(pairs)               SUM(q*p)/SUM(q) or None
"""

import logging
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


def gross_pnl(entry: float, exit: float, qty: float) -> float:
    """Gross price-diff pnl — (exit - entry) * qty.

    Callers: portfolio close, PnlMixin, trailing post-trade update,
    trailing SL/TP-fill detection, reconciler OCO booking
    (qty * (avg_px - entry_avg) — same expression, operands swapped;
    float multiplication is commutative so bits are identical)."""
    return (exit - entry) * qty


def net_pnl(entry: float, exit: float, qty: float, fee: float) -> float:
    """Net price-diff pnl — gross minus one fee leg.

    Callers: paper_trader _compute_sell_pnl (single-leg SELL fee),
    bull_paper_portfolio close leg (single-leg close fee; the bug#33
    entry-fee correction happens at the caller, above this formula)."""
    return (exit - entry) * qty - fee


def proceeds_net(px: float, qty: float, fee: float) -> float:
    """Net sale proceeds — qty*px - fee.

    Callers: backtester SELL leg, bull close leg proceeds. The
    round-trip economics (proceeds - entry_cost) stay at the caller:
    that combination is the backtest-specific cost-basis method, kept
    verbatim so backtest results are bit-identical."""
    return qty * px - fee


def pnl_pct(entry: float, exit: float) -> float:
    """Percentage pnl — 0.0 for non-positive entry (PnlMixin guard)."""
    return ((exit - entry) / entry) * 100 if entry > 0 else 0


def weighted_entry(pairs: Iterable[Tuple[float, float]]
                   ) -> Optional[float]:
    """Weighted-average price over (qty, price) pairs — None when the
    total qty is non-positive.

    Callers: reconciler _db_buy_avg (DB booked BUYs), entry_price
    get_avg_entry_price_from_db (FIFO lots). Iteration order is the
    caller's; SUM(q*p)/SUM(q) keeps the historical expression."""
    rows = list(pairs)  # two passes — generators would exhaust
    q = sum(qty for qty, _ in rows)
    if q <= 0:
        return None
    return sum(qty * price for qty, price in rows) / q
