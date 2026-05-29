"""
Feishu Notifier - Send trade notifications via Feishu Webhook

Stock-AI-Trader notification module for sending alerts, trade signals,
daily reports, and earnings notifications to Feishu.
"""

import os
import logging
import requests
from typing import Dict, List, Optional
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    TRADE = "trade"


# Emoji mapping for alert levels
_LEVEL_EMOJI = {
    AlertLevel.INFO: "ℹ️",
    AlertLevel.WARNING: "⚠️",
    AlertLevel.CRITICAL: "🚨",
    AlertLevel.TRADE: "🚀",
}


class FeishuNotifier:
    """
    Send notifications to Feishu via Webhook.

    Usage:
        notifier = FeishuNotifier()
        notifier.send_alert("Market Open", "US market is now open", AlertLevel.INFO)
        notifier.send_trade_signal(signal_dict)
        notifier.send_daily_report(report_dict)
        notifier.send_earnings_alert("AAPL", "2026-07-30")
    """

    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = (
            webhook_url
            or os.environ.get("FEISHU_WEBHOOK_URL", "")
        )
        self._enabled = bool(self.webhook_url)
        if not self._enabled:
            logger.warning(
                "FeishuNotifier: No FEISHU_WEBHOOK_URL configured. "
                "Notifications will be logged but not sent."
            )

    def _send(self, text: str) -> bool:
        """Send raw text message to Feishu webhook."""
        if not self._enabled:
            logger.info("[Feishu DISABLED] %s", text[:200])
            return False

        try:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
            resp = requests.post(
                self.webhook_url, json=payload, timeout=10
            )
            if resp.status_code != 200:
                logger.error(
                    "Feishu webhook error: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except Exception as e:
            logger.error("Failed to send Feishu message: %s", e)
            return False

    def _send_card(self, title: str, elements: List[Dict]) -> bool:
        """Send interactive card message to Feishu."""
        if not self._enabled:
            logger.info("[Feishu DISABLED] Card: %s", title)
            return False

        try:
            payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {
                            "tag": "plain_text",
                            "content": title,
                        },
                        "template": "blue",
                    },
                    "elements": elements,
                },
            }
            resp = requests.post(
                self.webhook_url, json=payload, timeout=10
            )
            if resp.status_code != 200:
                logger.error(
                    "Feishu card error: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except Exception as e:
            logger.error("Failed to send Feishu card: %s", e)
            return False

    # ── Alert Methods ──────────────────────────────────────────────────

    def send_alert(
        self,
        title: str,
        message: str,
        level: AlertLevel = AlertLevel.INFO,
    ) -> bool:
        """
        Send a general alert notification.

        Args:
            title: Alert title.
            message: Alert body text.
            level: Severity level (info/warning/critical/trade).

        Returns:
            True if sent successfully.
        """
        emoji = _LEVEL_EMOJI.get(level, "ℹ️")
        text = (
            f"{emoji} [{level.value.upper()}] {title}\n"
            f"\n{message}\n"
            f"\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return self._send(text)

    # ── Trade Signal ──────────────────────────────────────────────────

    def send_trade_signal(self, signal: Dict) -> bool:
        """
        Send a trade signal notification.

        Args:
            signal: Dict with keys:
                - symbol: Stock ticker
                - action: BUY/SELL
                - price: Entry/exit price
                - strategy: Strategy name
                - strength: Signal strength 0-1
                - stop_loss: Stop-loss price (optional)
                - metadata: Extra info dict (optional)

        Returns:
            True if sent successfully.
        """
        symbol = signal.get("symbol", "???")
        action = signal.get("action", "HOLD")
        price = signal.get("price", 0)
        strategy = signal.get("strategy", "unknown")
        strength = signal.get("strength", 0)
        stop_loss = signal.get("stop_loss")

        action_emoji = "📈" if action == "BUY" else "📉" if action == "SELL" else "➡️"

        lines = [
            f"🎯 交易信號 - {symbol}",
            "",
            f"操作: {action_emoji} {action}",
            f"價格: ${price:.2f}",
            f"策略: {strategy}",
            f"強度: {'█' * int(strength * 10)}{'░' * (10 - int(strength * 10))} {strength:.0%}",
        ]

        if stop_loss:
            lines.append(f"止損: ${stop_loss:.2f}")

        metadata = signal.get("metadata", {})
        if metadata:
            lines.append("")
            for k, v in metadata.items():
                lines.append(f"  {k}: {v}")

        lines.extend([
            "",
            "─" * 30,
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))

    # ── Daily Report ──────────────────────────────────────────────────

    def send_daily_report(self, report: Dict) -> bool:
        """
        Send a daily portfolio report.

        Args:
            report: Dict with keys:
                - date: Report date
                - total_return_pct: Total return %
                - daily_pnl: Daily P&L $
                - total_trades: Number of trades
                - win_rate: Win rate %
                - positions: List of position dicts
                - top_gainers: List of top gaining symbols
                - risk_status: Risk manager status dict

        Returns:
            True if sent successfully.
        """
        date = report.get("date", datetime.now().strftime("%Y-%m-%d"))
        total_return = report.get("total_return_pct", 0)
        daily_pnl = report.get("daily_pnl", 0)
        total_trades = report.get("total_trades", 0)
        win_rate = report.get("win_rate", 0)
        positions = report.get("positions", [])
        risk_status = report.get("risk_status", {})

        pnl_emoji = "🟢" if daily_pnl >= 0 else "🔴"

        lines = [
            f"📊 每日報告 {date}",
            "═" * 30,
            "",
            f"總收益: {total_return:+.2f}%",
            f"日盈虧: {pnl_emoji} ${daily_pnl:+,.2f}",
            f"交易次數: {total_trades}",
            f"勝率: {win_rate:.1f}%",
        ]

        if positions:
            lines.extend(["", "💼 持倉明細:"])
            for pos in positions[:10]:
                symbol = pos.get("symbol", "?")
                qty = pos.get("quantity", 0)
                entry = pos.get("entry_price", 0)
                current = pos.get("current_price", 0)
                pnl_pct = pos.get("pnl_pct", 0)
                pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
                lines.append(
                    f"  {pnl_icon} {symbol}: {qty}股 "
                    f"@ ${entry:.2f} → ${current:.2f} "
                    f"({pnl_pct:+.2f}%)"
                )

        if risk_status:
            lines.extend(["", "🛡 風控狀態:"])
            for k, v in risk_status.items():
                lines.append(f"  {k}: {v}")

        lines.extend([
            "",
            "═" * 30,
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))

    # ── Earnings Alert ────────────────────────────────────────────────

    def send_earnings_alert(
        self,
        symbol: str,
        earnings_date: str,
        estimated_eps: Optional[float] = None,
        actual_eps: Optional[float] = None,
    ) -> bool:
        """
        Send an earnings event alert.

        Args:
            symbol: Stock ticker.
            earnings_date: Date of earnings release.
            estimated_eps: Estimated EPS (optional).
            actual_eps: Actual EPS, if reported (optional).

        Returns:
            True if sent successfully.
        """
        if actual_eps is not None and estimated_eps is not None:
            # Earnings reported
            surprise_pct = (
                ((actual_eps - estimated_eps) / abs(estimated_eps) * 100)
                if estimated_eps != 0
                else 0
            )
            beat_miss = "超預期 ✅" if surprise_pct > 0 else "低於預期 ❌"

            lines = [
                f"📋 財報發布 - {symbol}",
                "",
                f"日期: {earnings_date}",
                f"預估EPS: ${estimated_eps:.2f}",
                f"實際EPS: ${actual_eps:.2f}",
                f"驚喜: {surprise_pct:+.1f}% ({beat_miss})",
            ]
        else:
            # Upcoming earnings
            lines = [
                f"📅 財報提醒 - {symbol}",
                "",
                f"財報日期: {earnings_date}",
                "⚠️ 請注意倉位風險，建議在財報前減倉或設定較寬止損",
            ]
            if estimated_eps is not None:
                lines.append(f"預估EPS: ${estimated_eps:.2f}")

        lines.extend([
            "",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))

    # ── Trade Execution ───────────────────────────────────────────────

    def send_trade_executed(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: int,
        strategy: str,
        order_id: Optional[str] = None,
    ) -> bool:
        """Send trade execution confirmation."""
        action_emoji = "🟢" if action == "BUY" else "🔴"
        total_cost = price * quantity

        lines = [
            f"🚀 訂單已執行",
            "",
            f"操作: {action_emoji} {action}",
            f"股票: {symbol}",
            f"價格: ${price:.2f}",
            f"數量: {quantity}股",
            f"總額: ${total_cost:,.2f}",
            f"策略: {strategy}",
        ]
        if order_id:
            lines.append(f"訂單ID: {order_id}")

        lines.extend([
            "",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))

    # ── Risk Alert ────────────────────────────────────────────────────

    def send_risk_alert(
        self,
        alert_type: str,
        details: Dict,
    ) -> bool:
        """
        Send a risk management alert.

        Args:
            alert_type: Type of risk alert (e.g., "drawdown", "pdt", "earnings_blackout").
            details: Dict with alert-specific details.

        Returns:
            True if sent successfully.
        """
        lines = [
            f"🛡 風控警報 - {alert_type.upper()}",
            "",
        ]

        for k, v in details.items():
            lines.append(f"  {k}: {v}")

        lines.extend([
            "",
            "⚠️ 請檢查系統狀態",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))

    # ── System Status ─────────────────────────────────────────────────

    def send_system_status(self, status: Dict) -> bool:
        """Send system health status."""
        overall = status.get("overall", "UNKNOWN")
        status_emoji = {
            "OK": "🟢",
            "WARNING": "🟡",
            "CRITICAL": "🔴",
        }.get(overall, "⚪")

        lines = [
            f"{status_emoji} 系統狀態: {overall}",
            "",
        ]

        checks = status.get("checks", {})
        for check_name, check_result in checks.items():
            check_status = check_result.get("status", "UNKNOWN")
            emoji = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "❌"}.get(
                check_status, "❓"
            )
            lines.append(f"  {emoji} {check_name}: {check_status}")
            msg = check_result.get("message", "")
            if msg:
                lines.append(f"      {msg}")

        lines.extend([
            "",
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        return self._send("\n".join(lines))
