"""
Tests for backtest module — targeting the 525 missed lines.
"""

# ── Data Classes ──────────────────────────────────────────────────────


class TestBacktestDataClasses:
    def test_position_dataclass(self):
        from src.backtest import Position

        pos = Position(
            symbol="BTCUSDT",
            entry_price=50000.0,
            entry_bar=10,
            entry_time=1000000,
            quantity=0.1,
            usdt_cost=5000.0,
            atr=1000.0,
            sl_price=49000.0,
            tp1_price=51000.0,
            tp1_size=0.04,
            tp2_price=52000.0,
            tp2_size=0.04,
            tp3_price=53000.0,
            tp3_size=0.02,
        )
        assert pos.symbol == "BTCUSDT"
        assert pos.entry_price == 50000.0
        assert pos.trailing_activated is False
        assert pos.tp1_hit is False

    def test_closed_trade_dataclass(self):
        from src.backtest import ClosedTrade

        ct = ClosedTrade(
            symbol="BTCUSDT",
            entry_price=50000.0,
            exit_price=55000.0,
            pnl_pct=10.0,
            pnl_usdt=500.0,
            reason="tp1",
            holding_bars=5,
            entry_time=1000000,
            exit_time=1000100,
        )
        assert ct.symbol == "BTCUSDT"
        assert ct.pnl_pct == 10.0


# ── calculate_score ───────────────────────────────────────────────────


class TestCalculateScore:
    def test_base_score(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0}
        score = calculate_score(a_1h, None, None)
        assert isinstance(score, (int, float))

    def test_rsi_oversold(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 20, "macd_histogram": 0}
        score = calculate_score(a_1h, None, None)
        assert score > 40  # base + RSI bonus

    def test_rsi_overbought(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 80, "macd_histogram": 0}
        score = calculate_score(a_1h, None, None)
        assert score < 40  # base - RSI penalty

    def test_macd_positive(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 5}
        score = calculate_score(a_1h, None, None)
        assert score > 40  # base + MACD bonus

    def test_macd_negative(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": -5}
        score = calculate_score(a_1h, None, None)
        assert score < 40  # base - MACD penalty

    def test_volume_surge(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0}
        score = calculate_score(a_1h, None, None, volume_surge=True)
        assert score > 40  # base + volume bonus

    def test_bollinger_below_lower(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0, "current_price": 90, "bb_lower": 100}
        score = calculate_score(a_1h, None, None)
        assert score > 40  # base + BB bonus

    def test_vwap_above(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0, "current_price": 110, "vwap": 100}
        score = calculate_score(a_1h, None, None)
        assert score > 40  # base + VWAP bonus

    def test_ma_alignment(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0, "ma7": 110, "ma25": 105, "ma99": 100}
        score = calculate_score(a_1h, None, None)
        assert score > 40  # base + MA bonus

    def test_volatility_normal(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0, "volatility_pct": 5}
        score = calculate_score(a_1h, None, None)
        assert isinstance(score, (int, float))

    def test_with_4h_data(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0}
        a_4h = {"macd_histogram": 5}
        score = calculate_score(a_1h, a_4h, None)
        assert score > 40  # base + 4h MACD bonus

    def test_with_1d_data(self):
        from src.backtest import calculate_score

        a_1h = {"rsi": 50, "macd_histogram": 0}
        a_1d = {"macd_histogram": 5}
        score = calculate_score(a_1h, None, a_1d)
        assert score > 40  # base + 1d MACD bonus


# ── BacktestEngine ────────────────────────────────────────────────────


class TestBacktestEngine:
    def test_import(self):
        from src.backtest import BacktestEngine

        assert BacktestEngine is not None

    def test_has_run_method(self):
        from src.backtest import BacktestEngine

        assert hasattr(BacktestEngine, "run")
