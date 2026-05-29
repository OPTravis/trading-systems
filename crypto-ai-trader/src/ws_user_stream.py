"""
WebSocket User Data Stream - Real-time balance and order updates.

Binance provides a user data stream via WebSocket that pushes:
- Account balance updates (when orders fill)
- Order status updates (NEW, FILLED, CANCELLED, etc.)
- Position changes

This avoids polling the REST API every few seconds.

Features:
- Auto-reconnect with exponential backoff (1s initial, 60s max, 2x multiplier)
- Connection health monitoring (stale connection detection)
- Non-blocking threaded operation
- Connection statistics tracking

Usage:
    from src.ws_user_stream import UserDataStream
    stream = UserDataStream(api_key, api_secret)
    stream.start(callback=handle_update)
    # ... later ...
    stream.stop()
"""
import json
import logging
import threading
import time
from typing import Callable, Dict, Optional

import websocket

logger = logging.getLogger(__name__)

# Binance WebSocket endpoints
SPOT_WS_BASE = "wss://stream.binance.com:9443/ws"

# Reconnection constants
RECONNECT_INITIAL_DELAY = 1.0   # Start with 1 second
RECONNECT_MAX_DELAY = 60.0      # Cap at 60 seconds
RECONNECT_MULTIPLIER = 2.0      # Double each time

# Health monitoring constants
HEALTH_CHECK_INTERVAL = 30      # Check connection health every 30s
STALE_CONNECTION_TIMEOUT = 120   # 2 minutes without data = stale


class ConnectionStats:
    """Track WebSocket connection statistics."""

    def __init__(self):
        self.total_connections = 0
        self.total_reconnections = 0
        self.total_messages_received = 0
        self.total_errors = 0
        self.last_connect_time: Optional[float] = None
        self.last_message_time: Optional[float] = None
        self.current_backoff_delay = RECONNECT_INITIAL_DELAY
        self.consecutive_failures = 0

    def reset_backoff(self):
        """Reset backoff after successful connection."""
        self.current_backoff_delay = RECONNECT_INITIAL_DELAY
        self.consecutive_failures = 0

    def increment_backoff(self) -> float:
        """Increment backoff delay and return new value."""
        self.consecutive_failures += 1
        self.current_backoff_delay = min(
            self.current_backoff_delay * RECONNECT_MULTIPLIER,
            RECONNECT_MAX_DELAY
        )
        return self.current_backoff_delay

    def record_connection(self):
        """Record a successful connection."""
        self.total_connections += 1
        self.last_connect_time = time.time()
        self.reset_backoff()

    def record_reconnection(self):
        """Record a reconnection attempt."""
        self.total_reconnections += 1

    def record_message(self):
        """Record receiving a message."""
        self.total_messages_received += 1
        self.last_message_time = time.time()

    def record_error(self):
        """Record an error."""
        self.total_errors += 1

    def is_stale(self) -> bool:
        """Check if connection appears stale (no messages received)."""
        if self.last_message_time is None:
            return False
        return (time.time() - self.last_message_time) > STALE_CONNECTION_TIMEOUT

    def get_stats(self) -> Dict:
        """Get current statistics."""
        return {
            "total_connections": self.total_connections,
            "total_reconnections": self.total_reconnections,
            "total_messages_received": self.total_messages_received,
            "total_errors": self.total_errors,
            "current_backoff_delay": self.current_backoff_delay,
            "consecutive_failures": self.consecutive_failures,
            "last_connect_time": self.last_connect_time,
            "last_message_time": self.last_message_time,
        }


class UserDataStream:
    """WebSocket user data stream for real-time account updates."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self._ws: Optional[websocket.WebSocketApp] = None
        self._listen_key: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._callback: Optional[Callable[[Dict], None]] = None
        self._last_ping = 0
        self._ping_interval = 60  # Send ping every 60s

        # Connection health monitoring
        self._stats = ConnectionStats()
        self._health_thread: Optional[threading.Thread] = None
        self._health_lock = threading.Lock()
        self._last_pong_time: Optional[float] = None

    def _get_listen_key(self) -> Optional[str]:
        """Get a listen key from Binance REST API."""
        import requests
        url = "https://testnet.binance.vision/api/v3/userDataStream" if self.testnet else "https://api3.binance.com/api/v3/userDataStream"
        try:
            resp = requests.post(
                url,
                headers={"X-MBX-APIKEY": self.api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                key = data.get("listenKey")
                logger.info("UserDataStream: got listen key")
                return key
            else:
                logger.error("UserDataStream: failed to get listen key: HTTP %d", resp.status_code)
        except Exception as e:
            logger.error("UserDataStream: error getting listen key: %s", e)
        return None

    def _keepalive_listen_key(self):
        """Keep the listen key alive (expires after 60 minutes)."""
        import requests
        url = "https://testnet.binance.vision/api/v3/userDataStream" if self.testnet else "https://api3.binance.com/api/v3/userDataStream"
        try:
            resp = requests.put(
                url,
                headers={"X-MBX-APIKEY": self.api_key},
                params={"listenKey": self._listen_key},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.debug("UserDataStream: listen key keepalive OK")
            else:
                logger.warning("UserDataStream: keepalive failed: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning("UserDataStream: keepalive error: %s", e)

    def _on_message(self, ws, message: str):
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(message)
            event_type = data.get("e", "")

            # Record message received for health monitoring
            with self._health_lock:
                self._stats.record_message()

            # Route by event type
            if event_type == "outboundAccountPosition":
                # Balance update
                logger.debug("UserDataStream: balance update for %d assets", len(data.get("B", [])))
            elif event_type == "executionReport":
                # Order update
                logger.debug(
                    "UserDataStream: order %s %s (status=%s)",
                    data.get("s"), data.get("c"), data.get("X"),
                )
            elif event_type == "balanceUpdate":
                # Single balance change
                logger.debug("UserDataStream: balance update %s %s", data.get("a"), data.get("d"))

            # Forward to callback
            if self._callback:
                self._callback(data)

        except json.JSONDecodeError:
            logger.warning("UserDataStream: invalid JSON: %s", message[:200])
        except Exception as e:
            logger.error("UserDataStream: message handling error: %s", e)

    def _on_error(self, ws, error):
        """Handle WebSocket error."""
        logger.error("UserDataStream: WebSocket error: %s", error)
        with self._health_lock:
            self._stats.record_error()

    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket close."""
        logger.warning("UserDataStream: connection closed: %s %s", close_status_code, close_msg)
        self._running = False

    def _on_open(self, ws):
        """Handle WebSocket open."""
        logger.info("UserDataStream: connection opened")
        with self._health_lock:
            self._stats.record_connection()
            self._last_pong_time = time.time()
        self._running = True

    def _run_websocket(self):
        """Run the WebSocket connection in a loop with exponential backoff.

        Exponential backoff strategy:
        - Initial delay: 1 second
        - Max delay: 60 seconds
        - Multiplier: 2x on each failure
        - Reset on successful connection
        """
        backoff_delay = RECONNECT_INITIAL_DELAY

        while self._running:
            try:
                # Get fresh listen key if needed
                if not self._listen_key:
                    self._listen_key = self._get_listen_key()
                    if not self._listen_key:
                        logger.warning("UserDataStream: failed to get listen key, retrying in %.1fs", backoff_delay)
                        time.sleep(backoff_delay)
                        with self._health_lock:
                            backoff_delay = self._stats.increment_backoff()
                        continue

                ws_url = f"{SPOT_WS_BASE}/{self._listen_key}"
                self._ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                logger.info("UserDataStream: connecting to %s...", ws_url[:60])
                # Run with ping to keep connection alive
                self._ws.run_forever(ping_interval=30, ping_timeout=10)

                # Connection closed - reconnect with backoff if still running
                if self._running:
                    logger.info(
                        "UserDataStream: reconnecting in %.1fs... (attempt %d, backoff: %.1fs)",
                        backoff_delay,
                        self._stats.consecutive_failures + 1,
                        backoff_delay,
                    )
                    with self._health_lock:
                        self._stats.record_reconnection()
                    time.sleep(backoff_delay)
                    with self._health_lock:
                        backoff_delay = self._stats.increment_backoff()
                    self._listen_key = None  # Get fresh key on reconnect

            except Exception as e:
                logger.error("UserDataStream: connection error: %s", e)
                with self._health_lock:
                    self._stats.record_error()
                time.sleep(backoff_delay)
                with self._health_lock:
                    backoff_delay = self._stats.increment_backoff()

    def _health_monitor_loop(self):
        """Background thread to monitor connection health.

        Checks for stale connections and logs periodic stats.
        """
        while self._running:
            time.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break

            with self._health_lock:
                stats = self._stats.get_stats()

            # Check for stale connection
            if stats["last_message_time"] is not None:
                time_since_msg = time.time() - stats["last_message_time"]
                if time_since_msg > STALE_CONNECTION_TIMEOUT:
                    logger.warning(
                        "UserDataStream HEALTH: No messages received for %.0fs (threshold: %ds). "
                        "Connection may be stale.",
                        time_since_msg,
                        STALE_CONNECTION_TIMEOUT,
                    )
                    # Force reconnect by closing current connection
                    self._force_reconnect()

            # Log periodic stats
            if stats["total_messages_received"] > 0:
                logger.debug(
                    "UserDataStream STATS: connections=%d, reconnections=%d, "
                    "messages=%d, errors=%d, backoff=%.1fs",
                    stats["total_connections"],
                    stats["total_reconnections"],
                    stats["total_messages_received"],
                    stats["total_errors"],
                    stats["current_backoff_delay"],
                )

    def _force_reconnect(self):
        """Force a reconnection by closing the current WebSocket."""
        try:
            if self._ws:
                logger.info("UserDataStream: forcing reconnection...")
                self._ws.close()
        except Exception as e:
            logger.debug("UserDataStream: error closing for reconnect: %s", e)

    def _keepalive_loop(self):
        """Background thread to keep listen key alive."""
        while self._running:
            time.sleep(1800)  # Every 30 minutes (key expires at 60)
            if self._listen_key and self._running:
                self._keepalive_listen_key()

    def start(self, callback: Optional[Callable[[Dict], None]] = None):
        """Start the user data stream.

        Args:
            callback: Function to call with each update dict
        """
        if self._running:
            logger.warning("UserDataStream: already running")
            return

        self._callback = callback
        self._running = True

        # Reset stats for fresh start
        with self._health_lock:
            self._stats = ConnectionStats()

        # Start WebSocket thread
        self._thread = threading.Thread(target=self._run_websocket, daemon=True)
        self._thread.start()

        # Start keepalive thread
        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

        # Start health monitor thread
        self._health_thread = threading.Thread(target=self._health_monitor_loop, daemon=True)
        self._health_thread.start()

        logger.info("UserDataStream: started")

    def stop(self):
        """Stop the user data stream."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                logger.warning("Error closing WebSocket connection", exc_info=True)
        self._ws = None
        logger.info("UserDataStream: stopped")

    def is_running(self) -> bool:
        """Check if the stream is currently running."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def get_stats(self) -> Dict:
        """Get current connection statistics.

        Returns:
            Dict with connection stats: total_connections, total_reconnections,
            total_messages_received, total_errors, current_backoff_delay,
            consecutive_failures, last_connect_time, last_message_time.
        """
        with self._health_lock:
            return self._stats.get_stats()


# Simple callback handler for integration
class BalanceChangeHandler:
    """Process balance updates and trigger actions."""

    def __init__(self, state_db=None):
        self.db = state_db
        self._last_balances: Dict[str, float] = {}

    def __call__(self, data: Dict):
        """Handle incoming WebSocket message."""
        event_type = data.get("e", "")

        if event_type == "outboundAccountPosition":
            # Full balance snapshot
            for bal in data.get("B", []):
                asset = bal.get("a", "")
                free = float(bal.get("f", 0))
                locked = float(bal.get("l", 0))
                total = free + locked

                # Detect changes
                prev = self._last_balances.get(asset, 0)
                if abs(total - prev) > 0.0001:
                    change = total - prev
                    logger.info("BalanceChange: %s %.6f (change: %+.6f)", asset, total, change)
                    self._last_balances[asset] = total

        elif event_type == "balanceUpdate":
            # Single balance delta
            asset = data.get("a", "")
            delta = float(data.get("d", 0))
            logger.info("BalanceChange: %s delta %+.6f", asset, delta)
