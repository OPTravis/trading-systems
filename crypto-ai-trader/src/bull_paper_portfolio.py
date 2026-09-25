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

WO-0924-z2 P6-B1: all paper_bull SQL now lives in BullPaperStore
(src/bull_paper_store.py); this class keeps the domain math (pnl, fees,
cash accounting, bug#33 partial-close accumulation) and delegates
persistence to the store.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .bull_paper_store import (
    BullPaperStore,
    new_position_id,
    new_trade_id,
)

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
    hold_seconds: float = 0.0   # P0-A6
    slippage_bps: float = 0.0   # P0-A6


def _row_to_pos(row) -> PaperPosition:
    """Build PaperPosition from a DB row, tolerating extra columns added
    by later migrations (e.g. ab_group in P0-C)."""
    fields = {f.name for f in PaperPosition.__dataclass_fields__.values()}
    return PaperPosition(**{k: v for k, v in dict(row).items() if k in fields})


class BullPaperPortfolio:
    """Isolated paper portfolio for BULL regime strategy."""

    def __init__(self, db, start_cash: float = 400.0, group: str = "A"):
        self.db = db
        self._start_cash = start_cash
        self.group = group  # P0-C: "A" (control/baseline) or "B" (variant)
        self.store = BullPaperStore(db)
        self._ensure_tables()

    def _ensure_tables(self):
        self.store.ensure_tables()
        self._init_cash_state()

    def _init_cash_state(self):
        # Init cash if not present (per-group keyed for P0-C; group A keeps legacy "cash_balance")
        _ck = self._cash_key()
        if self._get_state(_ck) is None:
            self._set_state(_ck, str(self._start_cash))
        if self.group == "A" and self._get_state("start_cash") is None:
            self._set_state("start_cash", str(self._start_cash))
        if self._get_state(f"start_cash_{self.group}") is None:
            self._set_state(f"start_cash_{self.group}", str(self._start_cash))
        if self._get_state("start_ts") is None:
            self._set_state("start_ts", str(int(time.time() * 1000)))

    def _cash_key(self) -> str:
        return "cash_balance" if self.group == "A" else f"cash_balance_{self.group}"

    def _start_cash_key(self) -> str:
        return "start_cash" if self.group == "A" else f"start_cash_{self.group}"

    # ── State KV ──────────────────────────────────────────────────────────
    def _get_state(self, key: str) -> Optional[str]:
        return self.store.state_get(key)

    def _set_state(self, key: str, value: str):
        self.store.state_set(key, value)

    # ── Cash ──────────────────────────────────────────────────────────────
    @property
    def cash(self) -> float:
        return float(self._get_state(self._cash_key()) or "0")

    @property
    def start_cash(self) -> float:
        return float(self._get_state(self._start_cash_key()) or str(self._start_cash))

    def _update_cash(self, delta: float):
        new_cash = self.cash + delta
        self._set_state(self._cash_key(), f"{new_cash:.8f}")

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
            id=new_position_id(),
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

        with self.store.transaction():
            self.store.insert_position(
                pos_id=pos.id, symbol=pos.symbol, side=pos.side,
                quantity=pos.quantity, entry_price=pos.entry_price,
                entry_time=pos.entry_time, stop_loss=pos.stop_loss,
                take_profit=pos.take_profit, atr_entry=pos.atr_entry,
                tier=pos.tier, fees=pos.fees, notes=pos.notes,
                ab_group=self.group)
            self.store.insert_trade(
                trade_id=new_trade_id(), position_id=pos.id, symbol=symbol,
                side=side, action="BUY", quantity=quantity, price=price,
                fee=fee, notional=notional, timestamp=pos.entry_time,
                details=notes, ab_group=self.group)

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
        with self.store.transaction():
            row = self.store.get_open_position(position_id)
            if not row:
                return None
            pos = _row_to_pos(row)

            close_qty = quantity if quantity else pos.quantity
            if close_qty > pos.quantity + 1e-12:
                close_qty = pos.quantity

            notional = close_qty * exit_price
            fee = notional * fee_rate
            # P4: net formulas via the single implementation
            # (float-identical expressions)
            from src.pnl_calculator import net_pnl, proceeds_net
            proceeds = proceeds_net(exit_price, close_qty, fee)
            pnl = net_pnl(pos.entry_price, exit_price, close_qty, fee)

            remaining = pos.quantity - close_qty

            if remaining < 1e-12:
                # Full close
                # bug#33: total_pnl = accumulated partial-leg PnL + this leg's
                # PnL, minus the entry fee (paid at open, never inside any
                # leg's pnl). Old formula ((exit-entry)*remaining - ALL fees)
                # priced only the LAST leg yet subtracted every fee, so
                # earlier TP-leg gains were swallowed (ZKP 8/30: booked
                # $0.67 vs true ~$7.60).
                entry_fee = self.store.entry_buy_fee(pos.id) or 0.0
                total_fees = pos.fees + fee
                total_pnl = (pos.realized_pnl or 0.0) + pnl - entry_fee
                _exit_ms = int(time.time() * 1000)
                _hold_s = (_exit_ms - pos.entry_time) / 1000.0
                self.store.update_position_full_close(
                    pos.id, exit_price=exit_price, exit_time=_exit_ms,
                    realized_pnl=total_pnl, fees=total_fees,
                    hold_seconds=_hold_s)
            else:
                # Partial close — update remaining qty, realize proportional P&L
                # bug#33: ACCUMULATE realized_pnl on the position row (old code
                # only noted the leg in `notes`, so fired TP legs never showed
                # up in position.realized_pnl and full close under-reported).
                total_fees = pos.fees + fee
                realized_pnl = (pos.realized_pnl or 0.0) + pnl
                self.store.update_position_partial_close(
                    pos.id, remaining_qty=remaining, fees=total_fees,
                    realized_pnl=realized_pnl,
                    note_append=(
                        f" | partial close {close_qty:.6f} @ ${exit_price:.4f}"
                        f" pnl=${pnl:.2f}"))
                # Re-fetch for return
                row = self.store.get_position(position_id)
                pos = _row_to_pos(row)
                pos.realized_pnl = realized_pnl

            self.store.insert_trade(
                trade_id=new_trade_id(), position_id=pos.id,
                symbol=pos.symbol, side=pos.side, action="SELL",
                quantity=close_qty, price=exit_price, fee=fee,
                notional=notional, timestamp=int(time.time() * 1000),
                details=reason, ab_group=self.group)

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
        self.store.update_stops(position_id, stop_loss, take_profit)

    def get_open_positions(self, side: Optional[str] = None) -> List[Dict]:
        return self.store.get_open_positions(self.group, side=side)

    def get_all_positions(self, limit: int = 100) -> List[Dict]:
        return self.store.get_all_positions(self.group, limit)

    def get_trade_history(self, limit: int = 50) -> List[Dict]:
        return self.store.get_trade_history(self.group, limit)

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

    def close_satellites_for_symbol(self, symbol: str, exit_price: float, reason: str = "CORE_ROTATE") -> int:
        """P0-A6: when a core lot is rotated/closed, also close any open
        satellite position on the SAME symbol so core/sat stay in sync."""
        closed = 0
        rows = self.store.get_open_satellites_by_symbol(symbol, self.group)
        for r in rows:
            try:
                self.close_position(r["id"], exit_price, reason=reason)
                closed += 1
            except Exception as e:
                logger.warning(f"[PAPER_BULL] core-rotate sat close failed {symbol}: {e}")
        return closed

    def hold_time_stats(self, side: Optional[str] = None, days: int = 30) -> Dict[str, float]:
        """P0-A6: hold-time distribution (hours) for closed positions."""
        since_ms = int((time.time() - days * 86400) * 1000)
        rows = self.store.closed_positions_since(self.group, since_ms, side=side)
        secs = [r["hold_seconds"] for r in rows if r["hold_seconds"] and r["hold_seconds"] > 0]
        if not secs:
            return {"count": 0}
        secs_sorted = sorted(secs)
        n = len(secs_sorted)
        hours = [s / 3600.0 for s in secs_sorted]
        return {
            "count": n,
            "avg_hours": round(sum(hours) / n, 2),
            "median_hours": round(hours[n // 2], 2),
            "p10_hours": round(hours[max(0, n // 10)], 2),
            "p90_hours": round(hours[min(n - 1, n * 9 // 10)], 2),
            "min_hours": round(hours[0], 2),
            "max_hours": round(hours[-1], 2),
        }

    def reset(self):
        """Nuke all paper BULL data. Use with caution."""
        self.store.reset_core_tables()
        self._init_cash_state()  # re-seed cash keys (pre-migration behavior)
        logger.warning("[PAPER_BULL] Portfolio reset complete")
