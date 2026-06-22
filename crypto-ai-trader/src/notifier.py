"""
Signal Notifier - writes signals to local JSON + pending notifications file
Notifications are pushed to WorkBuddy chat by the automation system
"""
import fcntl
import json
import logging
import os
import tempfile
import time as _time_module
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNALS_DIR = Path(__file__).parent.parent / "signals"
SIGNALS_FILE = SIGNALS_DIR / "pending.json"
NOTIFICATIONS_FILE = SIGNALS_DIR / "pending_notifications.json"


def _ensure_signals_dir():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def _append_notification(msg_type: str, title: str, body: str, max_retries: int = 3):
    """Append to pending notifications file for WorkBuddy automation to pick up.

    P2-5: Retries up to max_retries times with exponential backoff (1s, 2s, 4s)
    to ensure critical notifications (SL failure, trailing trigger) are persisted.

    P0-fix: Uses atomic write (temp file + os.replace) to prevent JSON corruption
    from concurrent writers (scan + trailing-check + ensure-tp-sl).
    """
    _ensure_signals_dir()
    for attempt in range(max_retries):
        try:
            # Use file lock to serialize concurrent writers
            lock_path = str(NOTIFICATIONS_FILE) + ".lock"
            lock_fd = None
            try:
                # Create/open lock file
                lock_fd = open(lock_path, "w")
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)  # blocking exclusive lock

                notifications = []
                if NOTIFICATIONS_FILE.exists():
                    try:
                        with open(NOTIFICATIONS_FILE, "r") as f:
                            notifications = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        logger.warning("Corrupted notifications file, backing up and resetting")
                        backup = str(NOTIFICATIONS_FILE) + f".corrupt.{int(_time_module.time())}"
                        try:
                            NOTIFICATIONS_FILE.rename(backup)
                        except OSError:
                            pass
                        notifications = []

                notifications.append({
                    "id": f"notif_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                    "timestamp": datetime.now().isoformat(),
                    "type": msg_type,
                    "title": title,
                    "body": body,
                    "pushed": False
                })

                # Atomic write: temp file + rename
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(SIGNALS_FILE.parent), suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(notifications, f, indent=2, ensure_ascii=False)
                    os.replace(tmp_path, str(NOTIFICATIONS_FILE))
                except:
                    # Clean up temp file on failure
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                if lock_fd:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        lock_fd.close()
                    except Exception:
                        pass
            return  # success
        except (OSError, IOError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning(
                    f"Notification write failed (attempt {attempt+1}/{max_retries}): {e}, "
                    f"retrying in {wait}s"
                )
                _time_module.sleep(wait)
            else:
                logger.error(
                    f"Notification write failed after {max_retries} attempts: {e} — "
                    f"notification lost: [{msg_type}] {title}: {body}"
                )


def send_signal(signal_type: str, symbol: str, action: str, price: float,
                quantity: float = 0, reason: str = "", strategy: str = ""):
    """
    Write a signal to pending.json and push notification.

    Args:
        signal_type: 'BUY' or 'SELL'
        symbol: trading pair, e.g. 'BTCUSDT'
        action: 'OPEN' or 'CLOSE'
        price: entry/exit price
        quantity: quantity (0 for close orders)
        reason: signal trigger reason
        strategy: strategy name
    """
    _ensure_signals_dir()

    signal = {
        "id": f"{symbol}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "timestamp": datetime.now().isoformat(),
        "type": signal_type,
        "symbol": symbol,
        "action": action,
        "price": round(price, 4),
        "quantity": quantity,
        "reason": reason,
        "strategy": strategy,
        "notified": False
    }

    # Load existing signals (with lock)
    import fcntl
    lock_path = str(SIGNALS_FILE) + ".lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        signals = []
        if SIGNALS_FILE.exists():
            try:
                with open(SIGNALS_FILE, "r") as f:
                    signals = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                signals = []

        signals.append(signal)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(dir=str(SIGNALS_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(signals, f, indent=2)
            os.replace(tmp_path, str(SIGNALS_FILE))
        except:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

    # Push notification
    emoji = "🟢" if signal_type == "BUY" else "🔴"
    notif_body = f"{emoji} {signal_type} {symbol} @ ${price:.4f}"
    if quantity:
        notif_body += f" x{quantity}"
    if reason:
        notif_body += f"\n原因: {reason}"
    if strategy:
        notif_body += f"\n策略: {strategy}"

    _append_notification("signal", f"交易信号: {symbol}", notif_body)
    print(f"[SignalNotifier] Signal written: {signal_type} {symbol} @ {price}")


def send_message(title: str, body: str):
    """Send a text message — logged + queued for WorkBuddy push.

    P2-5: Retries file writes up to 3 times with exponential backoff
    to ensure critical notifications survive transient I/O failures.
    P0-fix: Atomic write + flock to prevent JSON corruption.
    """
    _ensure_signals_dir()

    msg_file = SIGNALS_DIR / "messages.json"
    for attempt in range(3):
        try:
            lock_path = str(msg_file) + ".lock"
            lock_fd = open(lock_path, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                messages = []
                if msg_file.exists():
                    try:
                        with open(msg_file, "r") as f:
                            messages = json.load(f)
                    except (json.JSONDecodeError, FileNotFoundError):
                        messages = []
                messages.append({
                    "id": f"msg_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    "timestamp": datetime.now().isoformat(),
                    "title": title,
                    "body": body,
                    "notified": False
                })
                fd, tmp_path = tempfile.mkstemp(dir=str(SIGNALS_DIR), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w") as f:
                        json.dump(messages, f, indent=2, ensure_ascii=False)
                    os.replace(tmp_path, str(msg_file))
                except:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            break
        except (OSError, IOError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(
                    f"Message write failed (attempt {attempt+1}/3): {e}, retrying in {wait}s"
                )
                _time_module.sleep(wait)
            else:
                logger.error(f"Message write failed after 3 attempts: {e}")

    # Queue for WorkBuddy push (has its own retry via _append_notification)
    _append_notification("message", title, body)


# Aliases for compatibility with existing code
notify_trade = send_signal
notify_signal = send_signal
notify = send_message


class FeishuNotifier:
    """Compatibility wrapper — queues notifications for WorkBuddy instead of Feishu."""

    def __init__(self, webhook_url: str = ""):
        pass  # no-op, kept for constructor compatibility

    def send_text(self, text: str):
        send_message(title="", body=text)

    def send(self, title: str, content: str):
        send_message(title=title, body=content)
