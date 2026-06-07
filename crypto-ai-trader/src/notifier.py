"""
Signal Notifier - writes signals to local JSON + pending notifications file
Notifications are pushed to WorkBuddy chat by the automation system
"""
import json
import os
from datetime import datetime
from pathlib import Path

SIGNALS_DIR = Path(__file__).parent.parent / "signals"
SIGNALS_FILE = SIGNALS_DIR / "pending.json"
NOTIFICATIONS_FILE = SIGNALS_DIR / "pending_notifications.json"


def _ensure_signals_dir():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def _append_notification(msg_type: str, title: str, body: str):
    """Append to pending notifications file for WorkBuddy automation to pick up."""
    _ensure_signals_dir()
    notifications = []
    if NOTIFICATIONS_FILE.exists():
        try:
            with open(NOTIFICATIONS_FILE, "r") as f:
                notifications = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            notifications = []

    notifications.append({
        "id": f"notif_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        "timestamp": datetime.now().isoformat(),
        "type": msg_type,
        "title": title,
        "body": body,
        "pushed": False
    })

    with open(NOTIFICATIONS_FILE, "w") as f:
        json.dump(notifications, f, indent=2, ensure_ascii=False)


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

    # Load existing signals
    signals = []
    if SIGNALS_FILE.exists():
        with open(SIGNALS_FILE, "r") as f:
            signals = json.load(f)

    signals.append(signal)

    with open(SIGNALS_FILE, "w") as f:
        json.dump(signals, f, indent=2)

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
    """Send a text message — logged + queued for WorkBuddy push."""
    _ensure_signals_dir()

    # Save to messages.json (history)
    msg_file = SIGNALS_DIR / "messages.json"
    messages = []
    if msg_file.exists():
        with open(msg_file, "r") as f:
            messages = json.load(f)
    messages.append({
        "id": f"msg_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "timestamp": datetime.now().isoformat(),
        "title": title,
        "body": body,
        "notified": False
    })
    with open(msg_file, "w") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False)

    # Queue for WorkBuddy push
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
