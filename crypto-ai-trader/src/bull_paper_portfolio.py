"""
BULL Paper Portfolio — isolated position tracking for Phase 2 paper trading.

Per investment advisor requirement (2026-08-25):
  "Paper trading 倉位隔離（core_positions 表同 live trade_outcomes 完全分開）"

This module provides:
  - paper_core_positions: tracks BULL core lots (entry, add, exit)
  - paper_sat_positions: tracks satellite trades
  - paper_portfolio_state: cash balance and high-water mark
  - paper_bull_trades: trade log for BULL paper only

NONE of these tables touch:
  - trade_outcomes (live)
  - portfolio / portfolio_get_all() (live positions)
  - core_positions used by live system

All BULL paper P&L stays inside these tables.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    id: str
    symbol: str
    side: str               # 'core' or 'satellite'
    quantity: float
    entry_price: float
    entry_time: int
    stop_loss: float = 0.0
    take_profit: float = 0.0
    atr_entry: float = 0.0
    tier: int = 1
    status: str = "open"    # open / closed
    exit_price: float = 0.0
    exit_time: int = 0
    realized_pnl: float = 0.0
    fees: float = 0.0
    notes: str = ""


class BullPaperPortfolio:
    """Isolated paper portfolio for BULL regime strategy."""

    def __init__(self, db, start_cash: float = 400.0):
        self.db = db
        self._start_cash = start_cash
        self._ensure_tables()

    def _ensure_tables(self):
        with self.db._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS paper_bull_positions (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time INTEGER NOT NULL,
                    stop_loss REAL DEFAULT 0,
                    take_profit REAL DEFAULT 0,
                    atr_entry REAL DEFAULT 0,
                    tier INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'open',
                    exit_price REAL DEFAULT 0,
                    exit_time INTEGER DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    fees REAL DEFAULT 0,
                    notes TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pbp_symbol ON paper_bull_positions(symbol);
                CREATE INDEX IF NOT EXISTS idx_pbp_side ON paper_bull_positions(side);
                CREATE INDEX IF NOT EXISTS idx_pbp_status ON paper_bull_positions(status);

                CREATE TABLE IF NOT EXISTS paper_bull_trades (
                    id TEXT PRIMARY KEY,
                    position_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL DEFAULT 0,
                    notional REAL DEFAULT 0,
                    timestamp INTEGER NOT NULL,
                    details TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pbt_time ON paper_bull_trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_pbt_symbol ON paper_bull_trades(symbol);

                CREATE TABLE IF NOT EXISTS paper_bull_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            """)
            conn.commit()

        # Init cash if not present
        if self._get_state("cash_balance") is None:
            self._set_state("cash_balance", str(self._start_cash))
        if self._get_state("start_cash") is None:
            self._set_state("start_cash", str(self._start_cash))
        if self._get_state("start_ts") is None:
            self._set_state("start_ts", str(int(time.time() * 1000)))

    # ── State KV ──────────────────────────────────────────────────────────
    def _get_state(self, key: str) -> Optional[str]:
        with self.db._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM paper_bull_state WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def _set_state(self, key: str, value: str):
        with self.db._get_conn() as conn:
            conn.execute(
                """INSERT INTO paper_bull_state (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, int(time.time() * 1000)),
            )
            conn.commit()

    # ── Cash ──────────────────────────────────────────────────────────────
    @property
    def cash(self) -> float:
        return float(self._get_state("cash_balance") or "0")

    @property
    def start_cash(self) -> float:
        return float(self._get_state("start_cash") or str(self._start_cash))

    def _update_cash(self, delta: float):
        new_cash = self.cash + delta
        self._set_state("cash_balance", f"{new_cash:.8f}")

    # ── Positions ─────────────────────────────────────────────────────────
    def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        atr_entry: float = 0.0,
        tier: int = 1,
        fee_rate: float = 0.001,
        notes: str = "",
    ) -> PaperPosition:
        """Open a new paper position. Deducts cost + fee from cash."""
        notional = quantity * price
        fee = notional * fee_rate
        total_cost = notional + fee

        if total_cost > self.cash:
            raise ValueError(
                f"Insufficient paper cash: need ${total_cost:.2f}, have ${self.cash:.2f}"
            )

        pos = PaperPosition(
            id=f"paper_{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=price,
            entry_time=int(time.time() * 1000),
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr_entry=atr_entry,
            tier=tier,
            fees=fee,
            notes=notes,
        )

        with self.db._get_conn() as conn:
            conn.execute(
                """INSERT INTO paper_bull_positions
                   (id, symbol, side, quantity, entry_price, entry_time,
                    stop_loss, take_profit, atr_entry, tier, status, fees, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (pos.id, pos.symbol, pos.side, pos.quantity, pos.entry_price,
                 pos.entry_time, pos.stop_loss, pos.take_profit, pos.atr_entry,
                 pos.tier, pos.fees, pos.notes),
            )
            conn.execute(
                """INSERT INTO paper_bull_trades
                   (id, position_id, symbol, side, action, quantity, price, fee, notional, timestamp, details)
                   VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?)""",
                (f"trade_{uuid.uuid4().hex[:12]}", pos.id, symbol, side,
                 quantity, price, fee, notional, pos.entry_time, notes),
            )
            conn.commit()

        self._update_cash(-total_cost)
        logger.info(
            f"[PAPER_BULL] OPEN {side} {symbol} qty={quantity:.6f} @ ${price:.4f} "
            f"fee=${fee:.4f} | cash remaining ${self.cash:.2f}"
        )
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        quantity: Optional[float] = None,
        fee_rate: float = 0.001,
        reason: str = "",
    ) -> Optional[PaperPosition]:
        """Close (fully or partially) a paper position."""
        with self.db._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM paper_bull_positions WHERE id = ? AND status = 'open'",
                (position_id,),
            ).fetchone()
            if not row:
                return None
            pos = PaperPosition(**dict(row))

            close_qty = quantity if quantity else pos.quantity
            if close_qty > pos.quantity + 1e-12:
                close_qty = pos.quantity

            notional = close_qty * exit_price
            fee = notional * fee_rate
            proceeds = notional - fee
            cost_basis = close_qty * pos.entry_price
            pnl = proceeds - cost_basis

            remaining = pos.quantity - close_qty

            if remaining < 1e-12:
                # Full close
                total_fees = pos.fees + fee
                total_pnl = (exit_price - pos.entry_price) * pos.quantity - total_fees
                conn.execute(
                    """UPDATE paper_bull_positions
                       SET status='closed', exit_price=?, exit_time=?,
                           realized_pnl=?, fees=?
                       WHERE id=?""",
                    (exit_price, int(time.time() * 1000), total_pnl, total_fees, pos.id),
                )
            else:
                # Partial close — update remaining qty, realize proportional P&L
                total_fees = pos.fees + fee
                realized_pnl = pnl
                conn.execute(
                    """UPDATE paper_bull_positions
                       SET quantity=?, fees=?, notes=notes || ?
                       WHERE id=?""",
                    (remaining, total_fees,
                     f" | partial close {close_qty:.6f} @ ${exit_price:.4f} pnl=${pnl:.2f}",
                     pos.id),
                )
                # Re-fetch for return
                row = conn.execute(
                    "SELECT * FROM paper_bull_positions WHERE id = ?", (position_id,)
                ).fetchone()
                pos = PaperPosition(**dict(row))
                pos.realized_pnl = realized_pnl

            conn.execute(
                """INSERT INTO paper_bull_trades
                   (id, position_id, symbol, side, action, quantity, price, fee, notional, timestamp, details)
                   VALUES (?, ?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?)""",
                (f"trade_{uuid.uuid4().hex[:12]}", pos.id, pos.symbol, pos.side,
                 close_qty, exit_price, fee, notional, int(time.time() * 1000), reason),
            )
            conn.commit()

        self._update_cash(proceeds)
        logger.info(
            f"[PAPER_BULL] CLOSE {pos.side} {pos.symbol} qty={close_qty:.6f} "
            f"@ ${exit_price:.4f} pnl=${pnl:.2f} fee=${fee:.4f} | cash ${self.cash:.2f}"
        )
        return pos

    def update_stops(
        self, position_id: str, stop_loss: float = 0.0, take_profit: float = 0.0
    ):
        """Update SL/TP on an open position."""
        with self.db._get_conn() as conn:
            conn.execute(
                "UPDATE paper_bull_positions SET stop_loss=?, take_profit=? WHERE id=?",
                (stop_loss, take_profit, position_id),
            )
            conn.commit()

    def get_open_positions(self, side: Optional[str] = None) -> List[Dict]:
        q = "SELECT * FROM paper_bull_positions WHERE status = 'open'"
        params: list = []
        if side:
            q += " AND side = ?"
            params.append(side)
        q += " ORDER BY entry_time DESC"
        with self.db._get_conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_all_positions(self, limit: int = 100) -> List[Dict]:
        with self.db._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bull_positions ORDER BY entry_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        with self.db._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bull_trades ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def portfolio_value(self, prices: Dict[str, float]) -> Dict[str, Any]:
        """Calculate total paper portfolio value.

        Args:
            prices: {symbol: current_price} for held symbols.
        """
        positions = self.get_open_positions()
        market_value = 0.0
        core_mv = 0.0
        sat_mv = 0.0
        total_cost = 0.0

        for p in positions:
            px = prices.get(p["symbol"], p["entry_price"])
            mv = p["quantity"] * px
            cost = p["quantity"] * p["entry_price"]
            market_value += mv
            total_cost += cost
            if p["side"] == "core":
                core_mv += mv
            else:
                sat_mv += mv

        total = self.cash + market_value
        return {
            "total_value": total,
            "cash": self.cash,
            "market_value": market_value,
            "core_mv": core_mv,
            "sat_mv": sat_mv,
            "total_cost": total_cost,
            "unrealized_pnl": market_value - total_cost,
            "unrealized_pnl_pct": (
                (market_value - total_cost) / total_cost if total_cost > 0 else 0.0
            ),
            "total_return": (total - self.start_cash) / self.start_cash,
            "position_count": len(positions),
            "core_count": len([p for p in positions if p["side"] == "core"]),
            "sat_count": len([p for p in positions if p["side"] == "satellite"]),
        }

    def reset(self):
        """Nuke all paper BULL data. Use with caution."""
        with self.db._get_conn() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS paper_bull_positions;
                DROP TABLE IF EXISTS paper_bull_trades;
                DROP TABLE IF EXISTS paper_bull_state;
            """)
            conn.commit()
        self._ensure_tables()
        logger.warning("[PAPER_BULL] Portfolio reset complete")
