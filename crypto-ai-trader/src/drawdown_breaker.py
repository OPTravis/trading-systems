"""
Drawdown Breaker - Portfolio-level circuit breaker for maximum drawdown.

Hard stop at 10% portfolio drawdown from peak equity.
Once tripped, all new trades are blocked until manual reset.

STORAGE: SQLite state.db drawdown table (sole source of truth).
No JSON files are used; all state is persisted via StateDB.
"""
import logging
import time
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from src.exchange_client import ExchangeClient
from typing import Dict

logger = logging.getLogger(__name__)


class DrawdownBreaker:
    """Portfolio drawdown circuit breaker with 10% hard stop.

    All state is stored exclusively in the SQLite drawdown table via StateDB.
    """

    # Hard stop threshold
    HARD_STOP_PCT = 0.10  # 10%

    def __init__(self, binance_client: 'ExchangeClient' = None):
        self.client = binance_client
        self._state = self._load_state()

    def _load_state(self) -> Dict:
        """Load state from SQLite drawdown table (sole source of truth)."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            state = db.drawdown_get()
            logger.info("DrawdownBreaker: loaded from SQLite drawdown table")
            return state
        except Exception as e:
            logger.error(f"DrawdownBreaker: failed to load from SQLite: {e}")
            # Fail-safe defaults
            return {
                "high_watermark": 0.0,
                "current_drawdown_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "tripped_at": None,
                "tripped_count": 0,
                "reset_at": None,
                "history": [],
            }

    def _save_state(self):
        """Save state to SQLite drawdown table (sole source of truth)."""
        try:
            from src.state_db import get_state_db
            db = get_state_db()
            db.drawdown_set(self._state)
        except Exception as e:
            logger.error(f"DrawdownBreaker: SQLite save failed: {e}")

    def check_drawdown(self, current_balance: float) -> Dict:
        """Check if portfolio drawdown exceeds the 10% hard stop.

        Args:
            current_balance: Current total portfolio value in USDT

        Returns:
            {
                "tripped": bool,
                "drawdown_pct": float,
                "high_watermark": float,
                "action": str,  # "HOLD" / "TRIP" / "RESET" / "INIT"
                "reason": str,
            }
        """
        state = self._state
        hwm = state["high_watermark"]

        # Update high watermark
        if current_balance > hwm:
            # Sanity check: reject if equity looks inflated (>3x previous watermark)
            # This prevents phantom spikes from corrupting the watermark
            if hwm > 0 and current_balance > hwm * 3:
                logger.warning(
                    "DrawdownBreaker: rejected watermark update %.2f → %.2f (3x spike, likely phantom)",
                    hwm, current_balance
                )
                self._save_state()
                return {
                    "tripped": False,
                    "drawdown_pct": round(((hwm - current_balance) / hwm) * 100, 2) if hwm > 0 else 0,
                    "high_watermark": hwm,
                    "action": "HOLD",
                    "reason": f"Rejected phantom equity spike: {current_balance:.2f} > 3x watermark {hwm:.2f}",
                }
            if hwm > 0:
                # Record the peak before resetting
                state["history"].append({
                    "hwm": hwm,
                    "new_hwm": current_balance,
                    "timestamp": time.time(),
                })
                # Trim history to last 100 entries
                state["history"] = state["history"][-100:]
            state["high_watermark"] = current_balance
            state["current_drawdown_pct"] = 0.0
            state["reset_at"] = time.time()
            self._save_state()
            return {
                "tripped": False,
                "drawdown_pct": 0.0,
                "high_watermark": current_balance,
                "action": "RESET",
                "reason": "New high watermark established",
            }

        if hwm <= 0:
            # First run, set initial watermark
            state["high_watermark"] = current_balance
            self._save_state()
            return {
                "tripped": False,
                "drawdown_pct": 0.0,
                "high_watermark": current_balance,
                "action": "INIT",
                "reason": "Initial watermark set",
            }

        # Calculate drawdown
        drawdown = (hwm - current_balance) / hwm
        state["current_drawdown_pct"] = round(drawdown * 100, 2)

        if drawdown > state["max_drawdown_pct"]:
            state["max_drawdown_pct"] = drawdown

        # Check hard stop
        if drawdown >= self.HARD_STOP_PCT:
            if not state["tripped_at"]:
                state["tripped_at"] = time.time()
                state["tripped_count"] = state.get("tripped_count", 0) + 1
                self._save_state()
                # FIX-8: Send Feishu notification when breaker trips
                try:
                    from src.notifier import FeishuNotifier
                    notifier = FeishuNotifier()
                    notifier.send_text(
                        f"\U0001f6a8 DRAWDOWN BREAKER TRIPPED!\n"
                        f"Drawdown: {drawdown*100:.1f}% >= 10% limit\n"
                        f"High watermark: ${hwm:,.2f}\n"
                        f"Current balance: ${current_balance:,.2f}\n"
                        f"All new trades BLOCKED until manual reset."
                    )
                except Exception:
                    logger.error("DrawdownBreaker: failed to send trip notification", exc_info=True)
                return {
                    "tripped": True,
                    "drawdown_pct": round(drawdown * 100, 2),
                    "high_watermark": hwm,
                    "action": "TRIP",
                    "reason": f"Hard stop triggered: {drawdown*100:.1f}% drawdown >= 10% limit",
                }
            else:
                # Already tripped, stay tripped
                self._save_state()
                return {
                    "tripped": True,
                    "drawdown_pct": round(drawdown * 100, 2),
                    "high_watermark": hwm,
                    "action": "HOLD",
                    "reason": f"Breaker still tripped: {drawdown*100:.1f}% drawdown",
                }

        self._save_state()
        return {
            "tripped": False,
            "drawdown_pct": round(drawdown * 100, 2),
            "high_watermark": hwm,
            "action": "HOLD",
            "reason": f"Drawdown {drawdown*100:.1f}% within limit",
        }

    def reset(self, new_balance: float = None):
        """Manually reset the breaker (requires human confirmation).

        Only call this after investigating why drawdown occurred.
        Pass the current portfolio balance to set a correct watermark.
        """
        state = self._state
        if new_balance is not None and new_balance > 0:
            state["high_watermark"] = new_balance
        else:
            # No balance provided — keep existing watermark but clear tripped state
            pass
        state["current_drawdown_pct"] = 0.0
        state["tripped_at"] = None
        state["tripped_count"] = 0
        state["max_drawdown_pct"] = 0.0
        state["reset_at"] = time.time()
        self._save_state()
        logger.warning("DrawdownBreaker: MANUAL RESET at %.2f", state["high_watermark"])

    def get_status(self) -> Dict:
        """Return current drawdown status."""
        return {
            "high_watermark": self._state["high_watermark"],
            "current_drawdown_pct": self._state["current_drawdown_pct"],
            "max_drawdown_pct": round(self._state.get("max_drawdown_pct", 0) * 100, 2),
            "tripped": self._state["tripped_at"] is not None,
            "tripped_count": self._state.get("tripped_count", 0),
            "tripped_at": self._state["tripped_at"],
        }
