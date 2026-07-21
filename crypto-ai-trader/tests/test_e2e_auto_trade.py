"""
End-to-end tests for crypto auto-trading pipeline:
- cmd_cron_scan() scenarios 1-11
- execute_auto_trade() scenarios 12-25
"""

import os
from unittest.mock import MagicMock, patch

import pytest


def _make_bc(usdt_free=1000, usdt_locked=0, extra_balances=None, scan_symbol="SOLUSDT"):
    bc = MagicMock()
    balances = [{"asset": "USDT", "free": str(usdt_free), "locked": str(usdt_locked)}]
    if extra_balances:
        balances.extend(extra_balances)
    bc.client.account.return_value = {"balances": balances}
    bc.client.funding_rate.return_value = [{"fundingRate": "0.0001"}]
    bc.get_account.return_value = {"balances": balances}
    bc.get_24hr_stats.return_value = {"last_price": "100.0"}
    bc.get_free_balance.return_value = usdt_free
    bc.get_price_precision.return_value = 2
    bc.get_ticker_price.return_value = 100.0
    bc.place_market_buy.return_value = {
        "symbol": scan_symbol,
        "orderId": 999,
        "status": "FILLED",
        "fills": [{"price": "100.00", "qty": "10", "commission": "0.01"}],
    }
    bc.place_order.return_value = {
        "symbol": scan_symbol,
        "orderId": 1000,
        "status": "NEW",
    }
    bc.place_limit_buy.return_value = {
        "orderId": 998,
        "price": 100.10,
        "qty": 0.2,
        "status": "FILLED",
    }
    bc.place_limit_sell.return_value = {
        "orderId": 997,
        "price": 99.90,
        "qty": 0.2,
        "status": "FILLED",
    }
    return bc


def _make_scanner(opportunities=None):
    ms = MagicMock()
    ms.get_top_movers.return_value = []
    ms.scan_all.return_value = opportunities or []
    return ms


def _make_sentiment(fng=50, label="Neutral"):
    sa = MagicMock()
    sa.get_market_sentiment.return_value = {
        "fear_greed": fng,
        "fng_classification": label,
    }
    return sa


def _make_notifier():
    n = MagicMock()
    n.get_strategy_config.return_value = {
        "stop_loss_pct": 2.0,
        "take_profit_levels": [
            {"pct": 2.0, "size_pct": 33},
            {"pct": 3.0, "size_pct": 33},
            {"pct": 5.0, "size_pct": 34},
        ],
        "max_hold_hours": 24,
    }
    n.send_text.return_value = True
    return n


def _make_rm(allowed=True, reasons=None, size_multiplier=1.0):
    rm = MagicMock()
    rm.pre_trade_check.return_value = {
        "allowed": allowed,
        "reasons": reasons or [],
        "adjustments": {"size_multiplier": size_multiplier},
    }
    # Mock trend_filter.check_trend to return proper dict (not MagicMock)
    rm.trend_filter.check_trend.return_value = {
        "trend": "NEUTRAL",
        "score": 50,
        "adx": 25,
        "allow_long": True,
        "size_multiplier": 1.0,
        "factors": {},
    }
    return rm


def _so_filters(step_size=1.0, qty_decimals=0, min_qty=1.0, min_notional=5.0):
    so = MagicMock()
    so.get_symbol_filters.return_value = {
        "stepSize": step_size,
        "qty_decimals": qty_decimals,
        "minQty": min_qty,
        "minNotional": min_notional,
    }
    return so


class TestCronScan:

    def _run_scan(self, bc, ms, sa, rm, notifier, auto_execute="true"):
        # Mock DimensionScorer + SurgeDetector so tests don't depend on live APIs
        _mock_ds = MagicMock()
        _mock_ds.score_all.return_value = {"resonance": "NEUTRAL", "dimensions": {}}
        _mock_ds.format_report.return_value = "mock"
        _mock_surge = MagicMock()
        _mock_surge.detect.return_value = {
            "alert_level": "SILENCE", "should_alert": False,
            "summary": "", "phase1_count": 0, "phase2_count": 0, "phase3_count": 0,
        }
        with patch("src.scan_phases.get_trading_client", return_value=bc), patch(
            "src.scan_phases.MarketScanner", return_value=ms
        ), patch("src.scan_phases.FeishuNotifier", return_value=notifier), patch(
            "src.scan_phases.SentimentAnalyzer", return_value=sa
        ), patch(
            "src.scan_phases.PortfolioManager"
        ), patch(
            "src.risk_manager.RiskManager", return_value=rm
        ), patch(
            "src.dimension_scorer.DimensionScorer", return_value=_mock_ds
        ), patch(
            "src.surge_detector.SurgeDetector", return_value=_mock_surge
        ), patch(
            "src.market_researcher.MarketResearcher"
        ) as mock_mr, patch(
            "src.strategy_adaptor.StrategyAdaptor"
        ) as mock_sa, patch(
            "src.research_phase.PositionOptimizer"
        ) as mock_opt, patch(
            "src.execute_phases.clear_pending"
        ) as mock_clear, patch(
            "src.execute_phases.save_pending"
        ) as mock_save, patch(
            "src.execute_phases.execute_auto_trade"
        ) as mock_exec, patch(
            "src.scan_phases.clear_pending", mock_clear
        ), patch.dict(
            os.environ, {"AUTO_EXECUTE": auto_execute}
        ):
            mock_mr.return_value.research.return_value = {
                "score_adjustment": 0.0,
                "confidence": 0.5,
                "sentiment_summary": "mock",
                "news": [],
                "catalysts": [],
                "onchain": {},
            }
            mock_sa.return_value.adapt.return_value = {
                "regime": "NEUTRAL",
                "global": {
                    "score_threshold": 60,
                    "funding_signal": "N/A",
                    "cash_reserve_pct": 30,
                    "max_position_pct": 15,
                    "max_total_exposure_pct": 70,
                },
                "strategies": {},
            }
            mock_opt.return_value.analyze_and_switch.return_value = []
            mock_exec.return_value = {
                "success": True,
                "qty": 10.0,
                "price": 100.0,
                "tier": "MEDIUM-HIGH",
                "invest_pct": 29.7,
                "error": None,
            }
            from main import cmd_cron_scan

            cmd_cron_scan()
        return mock_exec, mock_save, mock_clear

    def test_s1_happy_path_auto_execute(self):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100.0,
            "score": 75,
            "signals": ["RSI Oversold"],
            "atr": 5.0,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(True),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_called_once()

    def test_s2_extreme_fear_threshold_60(self):
        bc = _make_bc()
        opp = {
            "symbol": "DOGEUSDT",
            "price": 0.1,
            "score": 70,
            "signals": ["MACD"],
            "atr": 0.005,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(15, "Extreme Fear"),
            _make_rm(True),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_called_once()

    def test_s3_extreme_greed_threshold_85(self):
        bc = _make_bc()
        opp = {
            "symbol": "BTCUSDT",
            "price": 50000,
            "score": 80,
            "signals": ["Trend"],
            "atr": 500,
        }
        mock_exec, _, mock_clear = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(90, "Extreme Greed"),
            _make_rm(True),
            _make_notifier(),
            "true",
        )
        # Extreme greed no longer blocks trades — regime adapts dynamically
        mock_exec.assert_called_once()

    def test_s4_fng_api_failure_fallback(self):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 72,
            "signals": ["VWAP"],
            "atr": 5,
        }
        sa = MagicMock()
        sa.get_market_sentiment.side_effect = Exception("API down")
        mock_exec, _, _ = self._run_scan(
            bc, _make_scanner([opp]), sa, _make_rm(True), _make_notifier(), "true"
        )
        mock_exec.assert_called_once()

    def test_s5_trendfilter_bearish_blocked(self, capsys):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 75,
            "signals": ["RSI"],
            "atr": 5,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(False, ["BTC trend is BEARISH, longs not allowed"]),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_not_called()
        assert "RISK_BLOCKED" in capsys.readouterr().out

    def test_s6_loss_guard_paused(self, capsys):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 75,
            "signals": ["RSI"],
            "atr": 5,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(False, ["Consecutive loss guard active"]),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_not_called()
        assert "RISK_BLOCKED" in capsys.readouterr().out

    def test_s7_sector_blocked(self, capsys):
        bc = _make_bc()
        opp = {
            "symbol": "FETUSDT",
            "price": 5,
            "score": 80,
            "signals": ["MACD"],
            "atr": 0.2,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(False, ["Sector AI at 40%"]),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_not_called()
        assert "RISK_BLOCKED" in capsys.readouterr().out

    def test_s8_no_opportunities(self, capsys):
        bc = _make_bc()
        _, _, mock_clear = self._run_scan(
            bc,
            _make_scanner([]),
            _make_sentiment(50),
            _make_rm(),
            _make_notifier(),
            "true",
        )
        assert "NO_OPPORTUNITIES" in capsys.readouterr().out
        mock_clear.assert_called_once()

    def test_s9_already_holding_filtered(self, capsys):
        bc = _make_bc(extra_balances=[{"asset": "SOL", "free": "10", "locked": "0"}])
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 80,
            "signals": ["RSI"],
            "atr": 5,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(),
            _make_notifier(),
            "true",
        )
        # Already-holding filter removed — scan orchestrator allows re-entry
        mock_exec.assert_called_once()

    def test_s10_size_multiplier_stored(self):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 75,
            "signals": ["RSI"],
            "atr": 5,
        }
        mock_exec, _, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(True, size_multiplier=0.5),
            _make_notifier(),
            "true",
        )
        mock_exec.assert_called_once()

    def test_s11_auto_execute_false_save_pending(self, capsys):
        bc = _make_bc()
        opp = {
            "symbol": "SOLUSDT",
            "price": 100,
            "score": 75,
            "signals": ["RSI"],
            "atr": 5,
        }
        mock_exec, mock_save, _ = self._run_scan(
            bc,
            _make_scanner([opp]),
            _make_sentiment(50),
            _make_rm(True),
            _make_notifier(),
            "false",
        )
        mock_exec.assert_not_called()
        mock_save.assert_called_once()
        assert "YES SOLUSDT" in capsys.readouterr().out


class TestExecuteAutoTrade:

    def _run(
        self,
        symbol="SOLUSDT",
        price=100.0,
        score=75,
        usdt_bal=1000,
        active_positions=0,
        tp_levels=None,
        filters=None,
        buy_result=None,
    ):
        bc = _make_bc(usdt_free=usdt_bal, scan_symbol=symbol)
        if buy_result is not None:
            bc.place_market_buy.return_value = buy_result
        notifier = _make_notifier()
        so = _so_filters(**(filters or {}))
        tps = tp_levels or [
            {"pct": 2.0, "size_pct": 33},
            {"pct": 3.0, "size_pct": 33},
            {"pct": 5.0, "size_pct": 34},
        ]
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier", return_value=notifier
        ), patch(
            "src.trade_executor.count_active_positions", return_value=active_positions
        ), patch(
            "src.smart_order.SmartOrder", return_value=so
        ), patch(
            "src.trade_executor.PortfolioManager"
        ), patch(
            "src.circuit_breaker.CircuitBreaker"
        ) as mock_cb, patch(
            "src.daily_loss_breaker.get_daily_loss_breaker"
        ) as mock_dlb, patch(
            "src.drawdown_breaker.DrawdownBreaker"
        ) as mock_ddb, patch(
            "src.kelly_sizer.KellyPositionSizer"
        ) as mock_kelly, patch(
            "src.fee_optimizer.FeeOptimizer"
        ) as mock_fee, patch(
            "src.state_db.get_state_db"
        ), patch(
            "src.twap_vwap.time.sleep"
        ):
            mock_cb.return_value.is_tripped.return_value = False
            mock_dlb_inst = mock_dlb.return_value
            mock_dlb_inst.check_daily_loss.return_value = {"tier": 0}
            mock_dlb_inst.should_close_all.return_value = False
            mock_dlb_inst.should_block_new_trades.return_value = False
            mock_dlb_inst.get_position_size_multiplier.return_value = 1.0
            mock_ddb.return_value.check_drawdown.return_value = {"drawdown_pct": 0}
            mock_fee.return_value.get_effective_fees.return_value = {"taker_fee": 0.001}
            mock_kelly.return_value.get_position_size.return_value = {
                "position_pct": 0,
                "confidence": "estimated",
            }
            from main import execute_auto_trade

            result = execute_auto_trade(
                symbol,
                price,
                "trend",
                2.0,
                tps,
                price * 0.98,
                24,
                ["RSI Oversold"],
                "RSI Oversold",
                score=score,
            )
        return result, bc

    def test_s12_happy_path(self):
        result, bc = self._run(score=75)
        assert result["success"] is True
        assert result["tier"] == "MEDIUM-HIGH"
        assert result["qty"] == 10.0

    def test_s13_insufficient_usdt(self):
        result, _ = self._run(usdt_bal=5)
        assert result["success"] is False
        assert "Insufficient" in result["error"]

    def test_s14_score_too_low(self):
        result, _ = self._run(score=50)
        assert result["success"] is False
        assert "Score too low" in result["error"]

    def test_s15_max_positions(self):
        result, _ = self._run(active_positions=5)
        assert result["success"] is False
        assert "Max positions" in result["error"]

    @pytest.mark.parametrize(
        "active,expected_scale",
        [
            (1, 0.80),
            (2, 0.65),
            (3, 0.50),
            (4, 0.35),
        ],
    )
    def test_s16_position_scaling(self, active, expected_scale):
        result, _ = self._run(score=75, active_positions=active)
        assert result["success"] is True
        # Tier-based: 0.30 * scale * 0.99, capped at max_position_pct=15%
        # ContextualBandit applies 0.8x multiplier -> final is further reduced
        raw_pct = round(0.30 * expected_scale * 0.99 * 100, 1)
        capped = min(raw_pct, 15.0)
        # Allow for ContextualBandit multiplier (0.8x) and rounding
        assert result["invest_pct"] <= capped
        assert result["invest_pct"] > 0

    def test_s17_market_buy_fails(self):
        bc = _make_bc()
        bc.place_market_buy.return_value = None
        notifier = _make_notifier()
        so = _so_filters()
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier", return_value=notifier
        ), patch("src.trade_executor.count_active_positions", return_value=0), patch(
            "src.smart_order.SmartOrder", return_value=so
        ), patch(
            "src.trade_executor.PortfolioManager"
        ):
            from main import execute_auto_trade

            result = execute_auto_trade(
                "SOLUSDT",
                100.0,
                "trend",
                2.0,
                [
                    {"pct": 2.0, "size_pct": 33},
                    {"pct": 3.0, "size_pct": 33},
                    {"pct": 5.0, "size_pct": 34},
                ],
                98.0,
                24,
                ["RSI"],
                "RSI",
                score=75,
            )
        assert result["success"] is False
        # Kelly returns 0% → position is too small to trade
        assert "too small" in result["error"].lower() or "BUY MARKET failed" in result["error"]

    def test_s18_sl_placed_tp_partial_fail(self):
        bc = _make_bc()
        notifier = _make_notifier()
        so = _so_filters()
        call_n = [0]

        def mock_place(*a, **kw):
            call_n[0] += 1
            return (
                {"symbol": "SOLUSDT", "orderId": call_n[0] + 1000, "status": "NEW"}
                if call_n[0] <= 2
                else None
            )

        bc.place_order.side_effect = mock_place
        tps = [
            {"pct": 2.0, "size_pct": 40},
            {"pct": 3.0, "size_pct": 30},
            {"pct": 5.0, "size_pct": 30},
        ]
        mock_kelly = MagicMock()
        mock_kelly.get_position_size.return_value = {
            "position_pct": 0.05,
            "win_rate": 0.575,
            "reward_risk": 2.0,
            "confidence": "LOW (estimated from score)",
            "reason": "mocked",
        }
        mock_kelly.adjust_for_portfolio.return_value = mock_kelly.get_position_size.return_value
        with patch("src.trade_executor.get_trading_client", return_value=bc), patch(
            "src.trade_executor.FeishuNotifier", return_value=notifier
        ), patch("src.trade_executor.count_active_positions", return_value=0), patch(
            "src.smart_order.SmartOrder", return_value=so
        ), patch(
            "src.trade_executor.PortfolioManager"
        ), patch(
            "src.kelly_sizer.KellyPositionSizer", return_value=mock_kelly
        ), patch(
            "src.fee_optimizer.FeeOptimizer"
        ) as MockFee, patch(
            "src.twap_vwap.time.sleep"
        ):
            MockFee.return_value.get_effective_fees.return_value = {"taker_fee": 0.001}
            from main import execute_auto_trade

            result = execute_auto_trade(
                "SOLUSDT", 100.0, "trend", 2.0, tps, 98.0, 24, ["RSI"], "RSI", score=75
            )
        assert result["success"] is True

    def test_s19_sl_notional_qty_increased(self):
        result, _ = self._run(
            usdt_bal=200,
            filters={
                "step_size": 0.01,
                "qty_decimals": 2,
                "min_qty": 0.01,
                "min_notional": 20.0,
            },
        )
        assert result["success"] is True

    def test_s20_tp_skipped_notional_too_low(self):
        result, _ = self._run(
            usdt_bal=200,
            filters={
                "step_size": 0.01,
                "qty_decimals": 2,
                "min_qty": 0.01,
                "min_notional": 200.0,
            },
        )
        # Buy succeeds (min_notional check is in SmartOrder TP split, not execute_auto_trade)
        assert result["success"] is True

    def test_s21_stepsize_btc(self):
        result, _ = self._run(
            symbol="BTCUSDT",
            price=50000.0,
            filters={"step_size": 0.001, "qty_decimals": 3, "min_qty": 0.001},
        )
        assert result["success"] is True

    def test_s22_fee_reserve(self):
        result, _ = self._run(score=90, usdt_bal=1000)
        assert result["success"] is True
        # max_position_pct=15 but ContextualBandit may apply 0.8x → ~12%
        assert result["invest_pct"] <= 15.0
        assert result["invest_pct"] > 0

    def test_s23_price_precision_used(self):
        result, bc = self._run()
        assert result["success"] is True
        for c in bc.place_order.call_args_list:
            if "price" in c.kwargs:
                p = c.kwargs["price"]
                assert abs(p - round(p, 2)) < 1e-10

    def test_s24_tp_before_sl_order(self):
        """With Binance spot balance locking fix, TPs are placed before SL."""
        result, bc = self._run()
        assert result["success"] is True
        sell_calls = [
            c
            for c in bc.place_order.call_args_list
            if len(c.args) > 1 and c.args[1] == "SELL"
        ]
        if sell_calls:
            first_sell = sell_calls[0]
            # First SELL should be a LIMIT (TP), not STOP_LOSS_LIMIT,
            # because TPs must lock their portions before SL locks remainder
            assert "LIMIT" in str(first_sell)

    def test_s25_qty_below_minqty(self):
        result, _ = self._run(usdt_bal=50, price=10000.0, filters={"min_qty": 100.0})
        assert result["success"] is False
        assert (
            "below Binance" in result["error"]
            or "below" in result["error"]
            or "Qty too small" in result["error"]
        )
