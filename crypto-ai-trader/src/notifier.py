"""
Feishu Notifier - Send trade notifications via Feishu Webhook
"""

import os
import logging
import yaml
import requests
from typing import Dict, List, Optional
from datetime import datetime

from src.app_secrets import GENERAL_SECRETS, CRYPTO_SECRETS, load_secret_file

logger = logging.getLogger(__name__)


def load_config():
    """Load trading config"""
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "risk_limits.yaml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f)
    return {}


class FeishuNotifier:
    """Send notifications to Feishu via Webhook"""

    def __init__(self, webhook_url: str = None):
        self.webhook_url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
        self._load_from_secrets()
        self.config = load_config()

    def _load_from_secrets(self):
        """Load from secrets file if not set"""
        if self.webhook_url:
            return

        for path in [CRYPTO_SECRETS, GENERAL_SECRETS, os.path.expanduser("~/.hermes/.env")]:
            secrets = load_secret_file(path)
            if not secrets:
                continue
            if "FEISHU_WEBHOOK_URL" in secrets:
                self.webhook_url = secrets["FEISHU_WEBHOOK_URL"]
                break

    def send_text(self, text: str, **kwargs) -> bool:
        """Send text message to Feishu"""
        if not self.webhook_url:
            logger.warning("No FEISHU_WEBHOOK_URL configured")
            return False

        try:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            if resp.status_code != 200:
                logger.error(f"Feishu webhook error: {resp.status_code} {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to send Feishu message: {e}")
            return False

    def send_market_scan(self, opportunities: List[Dict], gainers: List[Dict], losers: List[Dict]) -> bool:
        """Send market scan summary"""
        lines = ["📊 市場掃描結果\n"]

        if gainers:
            lines.append("📈 Top Gainers:")
            for g in gainers[:3]:
                lines.append(f"  {g['symbol']}: +{g['change_pct']:.2f}%")

        if losers:
            lines.append("\n📉 Top Losers:")
            for l in losers[:3]:
                lines.append(f"  {l['symbol']}: {l['change_pct']:.2f}%")

        if opportunities:
            lines.append(f"\n🎯 Opportunities ({len(opportunities)}):")
            for o in opportunities[:5]:
                lines.append(f"  {o['symbol']}: Score {o['score']:.0f}")

        return self.send_text("\n".join(lines))

    def get_strategy_config(self, strategy: str) -> Dict:
        """Get stop loss and take profit config for a strategy"""
        strategies = self.config.get("strategies", {})
        return strategies.get(strategy.lower(), self.config.get("default", {}))

    def send_opportunity_with_confirmation(
        self,
        symbol: str,
        current_price: float,
        strategy: str,
        score: int,
        signals: List[str],
        reason: str
    ) -> bool:
        """Send opportunity with confirmation request"""

        cfg = self.get_strategy_config(strategy)
        stop_loss_pct = cfg.get("stop_loss_pct", 4.0)  # FIX-11: Widened from 2.0→4.0%
        tp_levels = cfg.get("take_profit_levels", [])
        max_hold = cfg.get("max_hold_hours", 24)

        # Calculate prices
        stop_price = current_price * (1 - stop_loss_pct / 100)

        tp_lines = []
        total_tp_size = 0
        for i, tp in enumerate(tp_levels):
            tp_pct = tp["pct"]
            tp_size = tp["size_pct"]
            tp_price = current_price * (1 + tp_pct / 100)
            prefix = "├" if i < len(tp_levels) - 1 else "└"
            tp_lines.append(f"{prefix} TP{i+1}: +{tp_pct}% @ ${tp_price:.6f} (卖出 {tp_size}%)")
            total_tp_size += tp_size

        # Pad if needed
        while total_tp_size < 100:
            tp_lines.append(f"  └ 剩余 {(100 - total_tp_size)}% 自动市价卖出")
            total_tp_size = 100

        lines = [
            f"🎯 {symbol} (Score: {score})",
            "",
            f"策略: {strategy.upper()}",
            f"当前价格: ${current_price:.6f}",
            "",
            "📊 建议止盈:"
        ]
        lines.extend(tp_lines)

        lines.extend([
            "",
            f"🛡 止损: -{stop_loss_pct}% @ ${stop_price:.6f}",
            f"⏱ 最大持仓: {max_hold}小时",
            "",
            f"💡 信号: {reason}",
            "",
            "───" * 4,
            '回复 "YES [Symbol]" 确认下单',
            f"例如: YES {symbol}",
            "",
            "─" * 20,
            "⚠️ 自动止损触发时立即执行，无需确认",
            "⚠️ Testnet 测试中，请知悉"
        ])

        text = "\n".join(lines)
        return self.send_text(text)

    def send_market_scan(self, opportunities: List[Dict], gainers: List, losers: List) -> bool:
        """Send market scan results"""
        if not opportunities and not gainers and not losers:
            return False

        lines = [f"📊 市场扫描报告 {datetime.now().strftime('%H:%M:%S')}\n"]

        if gainers:
            lines.append("📈 涨幅榜:")
            for g in gainers[:5]:
                vol = f"${g.get('quote_volume', 0)/1e6:.1f}M" if g.get('quote_volume', 0) > 0 else "N/A"
                lines.append(f"  {g['symbol']}: +{g['change_pct']:.2f}% ({vol})")
            lines.append("")

        if losers:
            lines.append("📉 跌幅榜:")
            for l in losers[:5]:
                vol = f"${l.get('quote_volume', 0)/1e6:.1f}M" if l.get('quote_volume', 0) > 0 else "N/A"
                lines.append(f"  {l['symbol']}: {l['change_pct']:.2f}% ({vol})")
            lines.append("")

        if opportunities:
            lines.append(f"🎯 发现 {len(opportunities)} 个机会 (Top 5):")
            for opp in opportunities[:5]:
                lines.append(f"  {opp['symbol']} (Score: {opp['score']:.0f})")
                signals = opp.get('signals', [])[:2]
                if signals:
                    lines.append(f"    {' / '.join(signals)}")

        text = "\n".join(lines)
        return self.send_text(text)

    def send_trade_alert(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: float,
        strategy: str,
        order_id: str = None
    ) -> bool:
        """Send trade execution alert"""
        lines = [
            f"🚀 订单已执行",
            f"操作: {action}",
            f"币种: {symbol}",
            f"价格: ${price:.6f}",
            f"数量: {quantity:.4f}",
            f"策略: {strategy.upper()}"
        ]
        if order_id:
            lines.append(f"订单ID: {order_id}")

        text = "\n".join(lines)
        return self.send_text(text)

    def send_trade_confirmation(
        self,
        symbol: str,
        action: str,
        price: float,
        stop_loss: float,
        tp_levels: List[Dict],
        strategy: str
    ) -> bool:
        """Send trade confirmation with SL/TP levels"""
        lines = [
            f"✅ 订单确认 - {symbol}",
            f"操作: {action}",
            f"开仓价: ${price:.6f}",
            "",
            "🛡 止损:"
        ]

        cfg = self.get_strategy_config(strategy)
        stop_loss_pct = cfg.get("stop_loss_pct", 4.0)  # FIX-11: Widened from 2.0→4.0%
        lines.append(f"  -{stop_loss_pct}% @ ${stop_loss:.6f}")

        lines.extend(["", "📊 止盈:"])
        for i, tp in enumerate(tp_levels):
            lines.append(f"  TP{i+1}: +{tp['pct']}% @ ${tp['price']:.6f} (卖出 {tp['size_pct']}%)")

        text = "\n".join(lines)
        return self.send_text(text)

    def send_stop_loss_triggered(
        self,
        symbol: str,
        exit_price: float,
        pnl_pct: float,
        reason: str = "止损触发"
    ) -> bool:
        """Send stop loss triggered alert"""
        emoji = "🔴" if pnl_pct < 0 else "🟢"
        lines = [
            f"{emoji} 仓位平仓 - {symbol}",
            f"原因: {reason}",
            f"出场价: ${exit_price:.6f}",
            f"盈亏: {pnl_pct:.2f}%"
        ]
        text = "\n".join(lines)
        return self.send_text(text)

    def send_take_profit_triggered(
        self,
        symbol: str,
        tp_level: int,
        exit_price: float,
        remaining_size_pct: int,
        total_pnl_pct: float
    ) -> bool:
        """Send take profit triggered alert"""
        lines = [
            f"🎯 止盈触发 - {symbol}",
            f"TP{tp_level} 执行",
            f"出场价: ${exit_price:.6f}",
            f"剩余仓位: {remaining_size_pct}%",
            f"累计盈亏: {total_pnl_pct:.2f}%"
        ]
        text = "\n".join(lines)
        return self.send_text(text)

    def send_portfolio_update(self, portfolio_summary: Dict) -> bool:
        """Send portfolio update"""
        lines = [
            "💼 持仓更新\n",
            f"总价值: ${portfolio_summary.get('total_value', 0):.2f}",
            f"现金: ${portfolio_summary.get('cash', 0):.2f}",
            f"敞口: ${portfolio_summary.get('total_exposure', 0):.2f}",
            f"PnL: ${portfolio_summary.get('total_pnl', 0):.2f}",
            f"持仓数: {portfolio_summary.get('positions_count', 0)}"
        ]

        positions = portfolio_summary.get("positions", [])
        if positions:
            lines.append("\n📊 持仓明细:")
            for pos in positions:
                lines.append(
                    f"  {pos.get('symbol')}: {pos.get('quantity', 0):.4f} "
                    f"@ ${pos.get('entry_price', 0):.4f} "
                    f"(PnL: {pos.get('pnl_pct', 0):.2f}%)"
                )

        return self.send_text("\n".join(lines))

    def send_daily_report(self, report: Dict) -> bool:
        """Send daily report"""
        lines = [
            f"📈 每日报告 {report.get('date', datetime.now().strftime('%Y-%m-%d'))}\n",
            f"总收益: {report.get('total_return_pct', 0):.2f}%",
            f"交易次数: {report.get('total_trades', 0)}",
            f"胜率: {report.get('win_rate', 0):.1f}%",
            f"最佳策略: {report.get('best_strategy', 'N/A')}"
        ]

        return self.send_text("\n".join(lines))
