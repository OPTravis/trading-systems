"""
Trade Journal - Decision memory and reflection system.

Records trade decisions to human-readable markdown and SQLite (state.db).
Provides reflection on past trades and generates lessons from closed positions.

Storage:
- Markdown: data/trade_journal.md (append-only human-readable log)
- SQLite: state.db decisions table (structured, queryable)
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TradeJournal:
    """Decision memory and reflection system for trade decisions.

    Writes to:
    - data/trade_journal.md: Append-only markdown log of all trades
    - state.db decisions table: Structured, queryable records
    """

    def __init__(self, data_dir: str = None):
        """Initialize TradeJournal.

        Args:
            data_dir: Path to data directory (default: ~/crypto-ai-trader/data)
        """
        if data_dir is None:
            self.data_dir = Path(__file__).parent.parent / "data"
        else:
            self.data_dir = Path(data_dir)

        self.journal_path = self.data_dir / "trade_journal.md"

        # Initialize markdown journal if it doesn't exist
        if not self.journal_path.exists():
            self.journal_path.write_text("# Trade Journal\n\n", encoding="utf-8")

    def _get_db(self):
        """Get StateDB singleton."""
        from src.state_db import get_state_db
        return get_state_db()

    def record_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        score: float,
        reasons: List[str],
        signals: List[str],
        strategy: str,
    ) -> str:
        """Record a trade execution to both markdown journal and SQLite.

        Args:
            symbol: Trading pair symbol (e.g., "BTCUSDT")
            side: "BUY" or "SELL"
            price: Execution price
            qty: Quantity traded
            score: Signal score (0-100)
            reasons: List of reasons for the trade
            signals: List of technical signals that triggered
            strategy: Strategy name that generated the trade

        Returns:
            Confirmation message
        """
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M")

        # Format reasons and signals as lists
        reasons_str = ", ".join(reasons) if reasons else "N/A"
        signals_str = ", ".join(signals) if signals else "N/A"

        # Append to markdown journal
        entry = f"""
## {timestamp} | {symbol} | {side} | score={score:.1f}
- Price: ${price:,.6f}
- Quantity: {qty}
- Reasons: [{reasons_str}]
- Signals: [{signals_str}]
- Strategy: {strategy}

"""
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(entry)

        # Write to SQLite
        try:
            db = self._get_db()
            db.decision_add(
                symbol=symbol,
                type="trade",
                decision=side,
                score=score,
                price=price,
                qty=qty,
                side=side,
                strategy=strategy,
                reasons=reasons,
                signals=signals,
            )
        except Exception as e:
            logger.warning(f"Failed to record trade to state.db: {e}")

        msg = f"📝 Journal recorded: {side} {symbol} @ ${price:,.2f} | {qty} units | Strategy: {strategy}"
        logger.info(msg)
        print(msg)

        return msg

    def record_decision(
        self,
        symbol: str,
        decision: str,
        score: float,
        bear_result=None,
        research: str = "",
    ) -> str:
        """Record a trading decision (buy/sell/hold) to SQLite + markdown.

        Args:
            symbol: Trading pair symbol
            decision: Decision made (e.g., "BUY", "SELL", "HOLD", "SKIP", "BLOCKED", "VETOED")
            score: Signal score
            bear_result: BearResult object, dict, or None (optional, None before bear analysis)
            research: Research summary

        Returns:
            Confirmation message
        """
        # Append to markdown journal
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d %H:%M")
        bear_info = ""
        if bear_result is not None:
            if hasattr(bear_result, "bear_score"):
                bear_info = f" | bear_score={bear_result.bear_score:.0f} veto={bear_result.veto}"
            elif isinstance(bear_result, dict):
                bear_info = f" | bear_score={bear_result.get('bear_score', 0):.0f}"

        # Ensure research is a string (may be MagicMock in tests)
        research_str = research if isinstance(research, str) else str(research) if research else ""
        entry = f"\n## {timestamp} | {symbol} | {decision} | score={score:.1f}{bear_info}\n"
        if research_str:
            entry += f"- Research: {research_str[:200]}\n"

        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(entry)

        # Write to SQLite
        try:
            db = self._get_db()
            db.decision_add(
                symbol=symbol,
                type="decision",
                decision=decision,
                score=score,
                bear_result=bear_result,
                research=research,
            )
        except Exception as e:
            logger.warning(f"Failed to record decision to state.db: {e}")

        msg = f"📋 Decision recorded: {symbol} -> {decision} (score={score:.1f})"
        logger.info(msg)
        print(msg)

        return msg

    def get_lessons(self, symbol: str = None, limit: int = 5) -> List[str]:
        """Get lessons from closed trades with significant PnL.

        Scans state.db for trades with |pnl| > 3%.

        Args:
            symbol: Filter by specific symbol (optional)
            limit: Maximum number of lessons to return

        Returns:
            List of reflection strings
        """
        lessons = []
        try:
            db = self._get_db()
            rows = db.decisions_get_lessons(symbol=symbol, limit=limit)
            for row in rows:
                lesson = self._generate_lesson_from_row(row)
                lessons.append(lesson)
        except Exception as e:
            logger.warning(f"Failed to get lessons from state.db: {e}")
        return lessons

    def get_trade_history(self, symbol: str = None, limit: int = 10) -> List[Dict]:
        """Get recent trade history from SQLite.

        Args:
            symbol: Filter by specific symbol (optional)
            limit: Maximum number of trades to return

        Returns:
            List of trade dictionaries
        """
        try:
            db = self._get_db()
            return db.decisions_get_history(symbol=symbol, type="trade", limit=limit)
        except Exception as e:
            logger.warning(f"Failed to get trade history from state.db: {e}")
            return []

    def reflect(
        self,
        symbol: str,
        pnl_pct: float,
        entry_price: float,
        exit_price: float,
        hold_duration: float,
    ) -> str:
        """Generate a reflection string for a closed trade.

        Args:
            symbol: Trading pair symbol
            pnl_pct: Percentage PnL (e.g., 5.2 for +5.2%)
            entry_price: Original entry price
            exit_price: Exit/close price
            hold_duration: How long position was held (in hours)

        Returns:
            Reflection string with analysis of the trade
        """
        reflection = f"Trade Reflection for {symbol}:\n"
        reflection += f"- PnL: {pnl_pct:+.2f}% (${entry_price:,.2f} -> ${exit_price:,.2f})\n"
        reflection += f"- Hold Duration: {hold_duration:.1f} hours\n"

        if pnl_pct > 0:
            reflection += f"- Outcome: PROFITABLE TRADE\n"
            reflection += f"- Takeaway: Good entry timing, captured {pnl_pct:.1f}% gain\n"
        else:
            reflection += f"- Outcome: LOSING TRADE\n"
            reflection += f"- Takeaway: Lost {abs(pnl_pct):.1f}%, review entry conditions and stop-loss\n"

        if hold_duration < 1:
            reflection += "- Note: Very short hold (< 1h), potential scalp trade\n"
        elif hold_duration < 24:
            reflection += "- Note: Short-term swing trade\n"
        elif hold_duration < 72:
            reflection += "- Note: Medium-term position\n"
        else:
            reflection += "- Note: Long-term hold (> 3 days)\n"

        return reflection

    def _generate_lesson_from_row(self, row: Dict) -> str:
        """Generate a lesson string from a DB row.

        Args:
            row: Decision row from state.db

        Returns:
            Lesson string
        """
        symbol = row.get("symbol", "UNKNOWN")
        side = row.get("side", row.get("decision", "UNKNOWN"))
        entry_price = row.get("price", 0)
        exit_price = row.get("exit_price", 0)
        pnl_pct = row.get("pnl_pct", 0)
        strategy = row.get("strategy", "unknown")

        # Parse JSON fields
        signals = row.get("signals", "[]")
        reasons = row.get("reasons", "[]")
        if isinstance(signals, str):
            try:
                signals = json.loads(signals)
            except json.JSONDecodeError:
                logger.error("Failed to parse signals JSON from trade journal", exc_info=True)
                signals = []
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except json.JSONDecodeError:
                logger.error("Failed to parse reasons JSON from trade journal", exc_info=True)
                reasons = []

        lesson = f"Trade Review - {symbol} {side} (Strategy: {strategy}):\n"
        lesson += f"- Entry: ${entry_price:,.2f}, Exit: ${exit_price:,.2f}\n"
        lesson += f"- PnL: {pnl_pct:+.2f}%\n"

        if pnl_pct > 0:
            lesson += f"- WIN: Signals [{', '.join(signals[:3])}]\n"
            lesson += f"- What worked: {', '.join(reasons[:2])}\n"
            lesson += f"- Consider: This pattern worked, watch for similar setups\n"
        else:
            lesson += f"- LOSS: Signals [{', '.join(signals[:3])}]\n"
            lesson += f"- What failed: {', '.join(reasons[:2])}\n"
            lesson += f"- Improvement: Review entry timing and risk management\n"

        return lesson
