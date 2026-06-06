"""
Signal Notifier - writes signals to local JSON file for Travis to pick up
Replaces Feishu/email notifier
"""
import json
import os
from datetime import datetime
from pathlib import Path

SIGNALS_DIR = Path(__file__).parent.parent / "signals"
SIGNALS_FILE = SIGNALS_DIR / "pending.json"


def _ensure_signals_dir():
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)


def send_signal(signal_type: str, symbol: str, action: str, price: float,
                quantity: float = 0, reason: str = "", strategy: str = ""):
    """
    Write a signal to pending.json for Travis to pick up.
    
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

    print(f"[SignalNotifier] Signal written: {signal_type} {symbol} @ {price}")


def send_message(title: str, body: str):
    """Send a text message (written to a separate messages file)."""
    _ensure_signals_dir()
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
        json.dump(messages, f, indent=2)


# Aliases for compatibility with existing code
notify_trade = send_signal
notify_signal = send_signal
notify = send_message
