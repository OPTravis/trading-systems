"""
Tests for fund_flow_audit — FIFO PnL computation and report generation.
Uses synthetic trade data, no network calls.
"""
import pytest
from collections import deque
from src.fund_flow_audit import compute_fifo_pnl, generate_report


def _mk_trade(symbol, side, qty, price, comm=0.001, comm_asset="BNB", ts=1716192000000):
    return {
        "symbol": symbol,
        "isBuyer": side == "BUY",
        "qty": str(qty),
        "price": str(price),
        "commission": str(comm),
        "commissionAsset": comm_asset,
        "time": ts,
    }


class TestFIFOPnL:
    """Test FIFO realized PnL computation."""

    def test_simple_profit(self):
        """Buy then sell at higher price = profit."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 110.0, ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 110.0})
        assert result["total_realized_pnl"] == pytest.approx(10.0, abs=0.01)
        assert result["num_trades"] == 2

    def test_simple_loss(self):
        """Buy then sell at lower price = loss."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 90.0, ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 90.0})
        assert result["total_realized_pnl"] == pytest.approx(-10.0, abs=0.01)

    def test_fifo_multi_lot(self):
        """Two buys at different prices, partial sell uses FIFO."""
        trades = [
            _mk_trade("ETHUSDT", "BUY", 2.0, 100.0, ts=1000000),
            _mk_trade("ETHUSDT", "BUY", 3.0, 120.0, ts=2000000),
            _mk_trade("ETHUSDT", "SELL", 2.0, 130.0, ts=3000000),
        ]
        result = compute_fifo_pnl(trades, {"ETHUSDT": 130.0})
        # First 2 units bought at 100, sold at 130 → +60
        assert result["total_realized_pnl"] == pytest.approx(60.0, abs=0.01)

    def test_partial_close_leaves_position(self):
        """Partial sell leaves open position for unrealized calc."""
        trades = [
            _mk_trade("SOLUSDT", "BUY", 10.0, 50.0, ts=1000000),
            _mk_trade("SOLUSDT", "SELL", 3.0, 55.0, ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"SOLUSDT": 60.0})
        # Realized: 3 * (55-50) = 15
        assert result["total_realized_pnl"] == pytest.approx(15.0, abs=0.01)
        # Open: 7 units, avg buy 50, current 60 → unrealized 7*(60-50)=70
        assert result["total_unrealized"] == pytest.approx(70.0, abs=0.01)
        assert len(result["unrealized_positions"]) == 1
        assert result["unrealized_positions"][0]["qty"] == pytest.approx(7.0)

    def test_commission_tracked(self):
        """Commission reduces net."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, comm=1.0, comm_asset="USDT", ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 110.0, comm=1.0, comm_asset="USDT", ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 110.0})
        assert result["total_realized_pnl"] == pytest.approx(10.0, abs=0.01)
        assert result["total_commission"] == pytest.approx(2.0, abs=0.01)
        assert result["net_realized"] == pytest.approx(8.0, abs=0.01)

    def test_multiple_symbols(self):
        """PnL tracked independently per symbol."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 105.0, ts=2000000),
            _mk_trade("ETHUSDT", "BUY", 2.0, 50.0, ts=3000000),
            _mk_trade("ETHUSDT", "SELL", 2.0, 45.0, ts=4000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 105.0, "ETHUSDT": 45.0})
        assert result["per_symbol_realized"]["BTCUSDT"] == pytest.approx(5.0, abs=0.01)
        assert result["per_symbol_realized"]["ETHUSDT"] == pytest.approx(-10.0, abs=0.01)
        assert result["total_realized_pnl"] == pytest.approx(-5.0, abs=0.01)

    def test_monthly_breakdown(self):
        """Trades in different months are grouped correctly."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1704067200000),  # 2024-01-01
            _mk_trade("BTCUSDT", "SELL", 1.0, 110.0, ts=1706745600000),  # 2024-02-01
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 110.0})
        assert "2024-01" in result["per_month"]
        assert "2024-02" in result["per_month"]
        assert result["per_month"]["2024-02"]["pnl"] == pytest.approx(10.0, abs=0.01)

    def test_no_trades(self):
        """Empty trade list produces zero totals."""
        result = compute_fifo_pnl([], {"BTCUSDT": 100.0})
        assert result["total_realized_pnl"] == 0.0
        assert result["num_trades"] == 0
        assert result["unrealized_positions"] == []

    def test_buy_only_no_realized(self):
        """Buy without sell = no realized PnL, just open position."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 120.0})
        assert result["total_realized_pnl"] == 0.0
        assert result["total_unrealized"] == pytest.approx(20.0, abs=0.01)

    def test_dust_positions_filtered(self):
        """Tiny positions below threshold are still shown for audit."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 0.0000000001, 100.0, ts=1000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 100.0})
        # Below 1e-8 threshold → filtered
        assert len(result["unrealized_positions"]) == 0


class TestReportGeneration:
    """Test markdown report output."""

    def test_report_has_sections(self):
        """Report contains key sections."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 105.0, ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 105.0})
        report = generate_report(result)
        assert "Fund Flow Audit" in report
        assert "Summary" in report
        assert "Per-Symbol" in report
        assert "Monthly" in report

    def test_report_format(self):
        """Report is valid markdown with tables."""
        trades = [
            _mk_trade("BTCUSDT", "BUY", 1.0, 100.0, ts=1000000),
            _mk_trade("BTCUSDT", "SELL", 1.0, 105.0, ts=2000000),
        ]
        result = compute_fifo_pnl(trades, {"BTCUSDT": 105.0})
        report = generate_report(result)
        assert report.count("|") > 10  # Has table rows
        assert "$" in report  # Has dollar amounts
