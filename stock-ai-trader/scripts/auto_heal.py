#!/usr/bin/env python3
"""
Auto Heal - Self-healing script for stock-ai-trader.

Performs automated recovery actions:
- Reset stuck circuit breakers
- Reconnect broker if disconnected
- Clear stale cache files
- Restart failed components

Usage:
    python scripts/auto_heal.py
    python scripts/auto_heal.py --dry-run
    python scripts/auto_heal.py --notify
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class AutoHealer:
    """Self-healing system for the trading bot."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.actions_taken = []

    def heal_all(self) -> list[str]:
        """Run all healing actions and return list of actions taken."""
        self._heal_circuit_breakers()
        self._heal_broker_connection()
        self._heal_stale_cache()
        self._heal_stale_locks()
        return self.actions_taken

    def _log_action(self, action: str):
        """Log an action taken."""
        prefix = "[DRY RUN] " if self.dry_run else ""
        logger.info("%s%s", prefix, action)
        self.actions_taken.append(action)

    # ── Circuit Breaker Reset ─────────────────────────────────────────

    def _heal_circuit_breakers(self):
        """Reset circuit breakers that may be stuck."""
        try:
            state_file = PROJECT_ROOT / "data" / "state" / "circuit_breaker.json"
            if not state_file.exists():
                return

            import json
            with open(state_file) as f:
                state = json.load(f)

            # Check if any breaker has been tripped for more than 1 hour
            tripped_at = state.get("tripped_at")
            if tripped_at:
                tripped_time = datetime.fromisoformat(tripped_at)
                if datetime.now() - tripped_time > timedelta(hours=1):
                    self._log_action(
                        f"Reset circuit breaker (was tripped at {tripped_at})"
                    )
                    if not self.dry_run:
                        state["tripped"] = False
                        state["tripped_at"] = None
                        state["reset_at"] = datetime.now().isoformat()
                        with open(state_file, "w") as f:
                            json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning("Circuit breaker heal failed: %s", e)

    # ── Broker Reconnection ───────────────────────────────────────────

    def _heal_broker_connection(self):
        """Check and reset broker connection state."""
        try:
            state_file = PROJECT_ROOT / "data" / "state" / "broker_state.json"
            if not state_file.exists():
                return

            import json
            with open(state_file) as f:
                state = json.load(f)

            # Check if broker has been disconnected for more than 5 minutes
            last_connected = state.get("last_connected")
            if last_connected:
                last_time = datetime.fromisoformat(last_connected)
                if datetime.now() - last_time > timedelta(minutes=5):
                    if state.get("status") != "connected":
                        self._log_action(
                            f"Reset broker connection state "
                            f"(disconnected since {last_connected})"
                        )
                        if not self.dry_run:
                            state["status"] = "pending_reconnect"
                            state["reconnect_requested_at"] = (
                                datetime.now().isoformat()
                            )
                            with open(state_file, "w") as f:
                                json.dump(state, f, indent=2)
        except Exception as e:
            logger.warning("Broker heal failed: %s", e)

    # ── Stale Cache Cleanup ───────────────────────────────────────────

    def _heal_stale_cache(self):
        """Remove cache files older than 7 days."""
        try:
            cache_dir = PROJECT_ROOT / "data" / "cache"
            if not cache_dir.exists():
                return

            cutoff = datetime.now().timestamp() - (7 * 24 * 3600)
            removed = 0

            for cache_file in cache_dir.glob("*"):
                if cache_file.is_file():
                    if cache_file.stat().st_mtime < cutoff:
                        self._log_action(f"Remove stale cache: {cache_file.name}")
                        if not self.dry_run:
                            cache_file.unlink()
                        removed += 1

            if removed > 0:
                logger.info("Cleaned up %d stale cache files", removed)
        except Exception as e:
            logger.warning("Cache cleanup failed: %s", e)

    # ── Stale Lock Cleanup ────────────────────────────────────────────

    def _heal_stale_locks(self):
        """Remove stale lock files."""
        try:
            lock_dir = PROJECT_ROOT / "data" / "locks"
            if not lock_dir.exists():
                return

            cutoff = datetime.now().timestamp() - (30 * 60)  # 30 minutes

            for lock_file in lock_dir.glob("*.lock"):
                if lock_file.is_file():
                    if lock_file.stat().st_mtime < cutoff:
                        self._log_action(f"Remove stale lock: {lock_file.name}")
                        if not self.dry_run:
                            lock_file.unlink()
        except Exception as e:
            logger.warning("Lock cleanup failed: %s", e)


def main():
    parser = argparse.ArgumentParser(description="Auto-heal trading system")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show actions without executing",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Send results to Feishu",
    )
    args = parser.parse_args()

    healer = AutoHealer(dry_run=args.dry_run)
    actions = healer.heal_all()

    if actions:
        print(f"\n{'='*50}")
        print(f"  Auto-Heal Summary ({'DRY RUN' if args.dry_run else 'EXECUTED'})")
        print(f"  Actions: {len(actions)}")
        print(f"{'='*50}")
        for action in actions:
            print(f"  • {action}")
        print()

        if args.notify:
            try:
                from src.notifier import FeishuNotifier, AlertLevel
                notifier = FeishuNotifier()
                notifier.send_alert(
                    "Auto-Heal Executed",
                    f"Actions taken:\n" + "\n".join(f"• {a}" for a in actions),
                    AlertLevel.INFO,
                )
            except Exception as e:
                logger.error("Failed to send notification: %s", e)
    else:
        logger.info("No healing actions needed")


if __name__ == "__main__":
    main()
