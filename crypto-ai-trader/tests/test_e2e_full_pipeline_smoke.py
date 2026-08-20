"""
Smoke Test — E2E 掃描→研究→執行全鏈路

Validates the complete cron-scan pipeline as a single continuous flow:
  Phase 1: Scan — portfolio sync, sentiment, strategy adaptation, market scan, filtering
  Phase 2: Research — risk checks, parallel research on top N, bear analysis, strategy selection
  Phase 3: Execute — auto-trade execution with layered risk management

All external dependencies (Binance API, LLM, news) are mocked.
Verifies data flows correctly between stages.
"""

import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch


def _submodule_patches(bc=None, scanner=None, notifier=None, sa=None,
                       bear_analyst=None, mock_exec=None):
    """Generate patches for scan_phases / execute_phases / research_phase.

    Since cmd_cron_scan delegates to sub-modules, objects (BinanceClient etc.)
    are instantiated there, not in scan_orchestrator. Tests must patch both.
    """
    subs = []
    if bc is not None:
        subs.append(("src.scan_phases.BinanceClient", {"return_value": bc}))
    if scanner is not None:
        subs.append(("src.scan_phases.MarketScanner", {"return_value": scanner}))
    if notifier is not None:
        subs.append(("src.scan_phases.FeishuNotifier", {"return_value": notifier}))
        subs.append(("src.research_phase.FeishuNotifier", {"return_value": notifier}))
    if sa is not None:
        subs.append(("src.scan_phases.SentimentAnalyzer", {"return_value": sa}))
    if bear_analyst is not None:
        subs.append(("src.scan_phases.BearAnalyst", {"return_value": bear_analyst}))
        subs.append(("src.research_phase.BearAnalyst", {"return_value": bear_analyst}))
    else:
        subs.append(("src.research_phase.BearAnalyst", {}))
    subs.append(("src.scan_phases.PortfolioManager", {}))
    subs.append(("src.scan_phases.PositionOptimizer", {}))
    subs.append(("src.scan_phases.clear_pending", {}))
    subs.append(("src.scan_phases.save_pending", {}))
    subs.append(("src.execute_phases.clear_pending", {}))
    subs.append(("src.execute_phases.save_pending", {}))
    subs.append(("src.execute_phases.execute_auto_trade", {}))
    return subs

# ── Helpers ──────────────────────────────────────────────────────


def _make_bc(usdt_free=1000, extra_balances=None):
    """Create a mock BinanceClient with configurable balances."""
    bc = MagicMock()
    balances = [{"asset": "USDT", "free": str(usdt_free), "locked": "0"}]
    if extra_balances:
        balances.extend(extra_balances)
    bc.get_account.return_value = {"balances": balances}
    bc.get_24hr_stats.return_value = {"last_price": "100.0", "price_change_pct": "2.5"}
    bc.get_free_balance.return_value = usdt_free
    bc.get_klines.return_value = [
        {"open": "99", "high": "101", "low": "98", "close": "100", "volume": "1000"}
    ] * 100
    bc.place_market_buy.return_value = {
        "symbol": "SOLUSDT",
        "orderId": 999,
        "status": "FILLED",
        "fills": [{"price": "100.00", "qty": "10", "commission": "0.01"}],
    }
    bc.place_order.return_value = {
        "symbol": "SOLUSDT",
        "orderId": 1000,
        "status": "NEW",
    }
    return bc


def _make_opportunity(symbol="SOLUSDT", price=100.0, score=75, signals=None):
    """Create a mock opportunity dict as returned by MarketScanner.scan_all()."""
    return {
        "symbol": symbol,
        "price": price,
        "score": score,
        "signals": signals or ["RSI Oversold"],
        "atr": 5.0,
        "volume_24h": 5e8,
        "price_change_24h": 3.5,
        "technical_score": 72,
        "trend_score": 68,
        "volume_surge": True,
        "funding_rate": 0.0001,
        "factor_scores": {
            "technical": 72,
            "trend": 68,
            "volume": 75,
            "sentiment": 60,
            "price_action": 70,
            "onchain": 55,
        },
        "analysis": {
            "1h": {
                "rsi": 35,
                "macd_histogram": 150,
                "current_price": price,
                "bb_lower": 95,
                "bb_upper": 105,
                "vwap": 99.5,
                "ma7": 101,
                "ma25": 100,
                "ma99": 98,
            },
        },
    }


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


def _make_risk_manager(allowed=True, size_multiplier=1.0):
    rm = MagicMock()
    rm.pre_trade_check.return_value = {
        "allowed": allowed,
        "reasons": [] if allowed else ["blocked by risk"],
        "adjustments": {"size_multiplier": size_multiplier},
    }
    rm.trend_filter.check_trend.return_value = {
        "trend": "BULLISH",
        "score": 65,
        "adx": 30,
        "allow_long": True,
        "size_multiplier": 1.0,
        "factors": {
            "ema_cross": 70,
            "rsi": 60,
            "macd": 65,
            "price_structure": 55,
            "volume": 70,
        },
    }
    return rm


def _make_researcher(adj=5.0, confidence=0.7):
    mr = MagicMock()
    mr.research.return_value = {
        "score_adjustment": adj,
        "confidence": confidence,
        "sentiment_summary": "Bullish outlook with strong volume",
        "news": [{"title": "SOL ecosystem growth", "sentiment": 0.8, "url": ""}],
        "catalysts": ["Partnership announcement"],
        "onchain": {"whale_activity": "ACCUMULATING"},
    }
    return mr


def _make_bear_analyst(veto=False, bear_score=30):
    ba = MagicMock()
    bear_result = MagicMock()
    bear_result.veto = veto
    bear_result.bear_score = bear_score
    bear_result.reasons = [] if not veto else ["High RSI divergence"]
    ba.analyze.return_value = bear_result
    return ba, bear_result


# ── Tests ────────────────────────────────────────────────────────


class TestE2EPipeline:

    """End-to-end smoke tests for the full scan→research→execute pipeline."""

    def _run_full_pipeline(
        self,
        opportunities=None,
        fng=50,
        fng_label="Neutral",
        risk_allowed=True,
        research_adj=5.0,
        bear_veto=False,
        bear_score=30,
        auto_execute="true",
        usdt_free=1000,
        strategy_regime="NEUTRAL",
        threshold=60,
        risk_manager=None,
    ):
        """Run the full cron-scan pipeline with all mocks wired up.

        Returns dict of mock objects for assertions.
        """
        if opportunities is None:
            opportunities = [_make_opportunity()]

        bc = _make_bc(usdt_free=usdt_free)
        scanner = MagicMock()
        scanner.get_top_movers.return_value = []
        scanner.scan_all.return_value = opportunities

        sa = _make_sentiment(fng, fng_label)
        notifier = _make_notifier()
        rm = risk_manager or _make_risk_manager(risk_allowed)
        researcher = _make_researcher(research_adj)
        bear_analyst, bear_result = _make_bear_analyst(bear_veto, bear_score)

        mock_exec_result = {
            "success": True,
            "qty": 10.0,
            "price": 100.0,
            "tier": "MEDIUM-HIGH",
            "invest_pct": 29.7,
            "error": None,
            "kelly": {"win_rate": 0.6, "confidence": "medium"},
        }

        from contextlib import ExitStack
        patches = [
            ("src.scan_orchestrator.BinanceClient", {"return_value": bc}),
            ("src.scan_orchestrator.MarketScanner", {"return_value": scanner}),
            ("src.scan_orchestrator.FeishuNotifier", {"return_value": notifier}),
            ("src.scan_orchestrator.SentimentAnalyzer", {"return_value": sa}),
            ("src.scan_orchestrator.PortfolioManager", {}),
            ("src.risk_manager.RiskManager", {"return_value": rm}),
            ("src.market_researcher.MarketResearcher", {"return_value": researcher}),
            ("src.scan_orchestrator.BearAnalyst", {"return_value": bear_analyst}),
            ("src.strategy_adaptor.StrategyAdaptor", {}),
            ("src.strategy_registry.StrategyRegistry", {}),
            ("src.dimension_scorer.DimensionScorer", {}),
            ("src.scan_orchestrator.PositionOptimizer", {}),
            ("src.scan_orchestrator.clear_pending", {}),
            ("src.scan_orchestrator.save_pending", {}),
            ("src.scan_orchestrator.execute_auto_trade", {}),
            ("src.trade_outcome_recorder.TradeOutcomeRecorder", {}),
            # scan_phases / execute_phases / research_phase — objects created there directly
            ("src.scan_phases.BinanceClient", {"return_value": bc}),
            ("src.scan_phases.MarketScanner", {"return_value": scanner}),
            ("src.scan_phases.FeishuNotifier", {"return_value": notifier}),
            ("src.scan_phases.SentimentAnalyzer", {"return_value": sa}),
            ("src.scan_phases.PortfolioManager", {}),
            ("src.scan_phases.BearAnalyst", {"return_value": bear_analyst}),
            ("src.research_phase.BearAnalyst", {"return_value": bear_analyst}),
            ("src.scan_phases.PositionOptimizer", {}),
            ("src.scan_phases.clear_pending", {}),
            ("src.scan_phases.save_pending", {}),
            ("src.execute_phases.clear_pending", {}),
            ("src.execute_phases.save_pending", {}),
            ("src.execute_phases.execute_auto_trade", {}),
            ("src.research_phase.FeishuNotifier", {"return_value": notifier}),
        ]
        with ExitStack() as stack:
            mocks = {}
            for target, kwargs in patches:
                mocks[target] = stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.dict(
                os.environ, {"AUTO_EXECUTE": auto_execute, "TRADING_MODE": "paper"}
            ))

            mock_pm = mocks["src.scan_orchestrator.PortfolioManager"]
            mock_adaptor = mocks["src.strategy_adaptor.StrategyAdaptor"]
            mock_registry = mocks["src.strategy_registry.StrategyRegistry"]
            mock_dim = mocks["src.dimension_scorer.DimensionScorer"]
            mock_opt = mocks["src.scan_orchestrator.PositionOptimizer"]
            mock_clear = mocks["src.scan_phases.clear_pending"]
            mock_save = mocks["src.execute_phases.save_pending"]
            mock_exec = mocks["src.execute_phases.execute_auto_trade"]
            mock_recorder = mocks["src.trade_outcome_recorder.TradeOutcomeRecorder"]
            # Strategy adaptor
            mock_adaptor.return_value.adapt.return_value = {
                "regime": strategy_regime,
                "global": {
                    "score_threshold": threshold,
                    "funding_signal": "N/A",
                    "cash_reserve_pct": 30,
                    "max_position_pct": 15,
                    "max_total_exposure_pct": 70,
                },
                "strategies": {
                    "trend": {
                        "enabled": True,
                        "size_multiplier": 1.0,
                        "sl_pct": 2.0,
                        "tp_levels": [{"pct": 2.0, "size_pct": 33}],
                        "max_hold_hours": 24,
                        "reason": "",
                    },
                    "rsi": {
                        "enabled": True,
                        "size_multiplier": 0.8,
                        "sl_pct": 1.5,
                        "tp_levels": [{"pct": 1.5, "size_pct": 50}],
                        "max_hold_hours": 12,
                        "reason": "",
                    },
                    "dca": {
                        "enabled": True,
                        "size_multiplier": 0.5,
                        "sl_pct": 3.0,
                        "tp_levels": [{"pct": 3.0, "size_pct": 50}],
                        "max_hold_hours": 48,
                        "reason": "",
                    },
                },
            }

            # Dimension scorer
            mock_dim.return_value.score_all.return_value = {
                "resonance": "NEUTRAL",
                "score": 50,
            }
            mock_dim.return_value.format_report.return_value = "Dimension: NEUTRAL"

            # Strategy registry
            mock_registry.return_value.select_best.return_value = (
                "trend",
                0.7,
                "strong signals",
                {"weight": 1.0},
            )

            # Position optimizer
            mock_opt.return_value.analyze_and_switch.return_value = []

            # Trade execution
            mock_exec.return_value = mock_exec_result

            # Recorder
            mock_recorder.return_value.record_entry.return_value = 42

            from main import cmd_cron_scan

            cmd_cron_scan()

        return {
            "bc": bc,
            "scanner": scanner,
            "sa": sa,
            "rm": rm,
            "researcher": researcher,
            "bear_analyst": bear_analyst,
            "bear_result": bear_result,
            "mock_exec": mock_exec,
            "mock_clear": mock_clear,
            "mock_save": mock_save,
            "mock_pm": mock_pm,
        }

    # ── Phase 1: Scan ────────────────────────────────────────────

    def test_scan_opportunities_pass_threshold(self, capsys):
        """Opportunities above dynamic threshold flow into research phase."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=75)],
            threshold=60,
        )
        output = capsys.readouterr().out
        # Should NOT print NO_OPPORTUNITIES
        assert "NO_OPPORTUNITIES" not in output

    def test_scan_filters_below_threshold(self, capsys):
        """Opportunities below threshold are filtered out, pipeline stops."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=40)],
            threshold=60,
        )
        output = capsys.readouterr().out
        assert "NO_OPPORTUNITIES" in output
        ctx["mock_clear"].assert_called_once()

    def test_scan_empty_market(self, capsys):
        """No market opportunities → pipeline stops cleanly."""
        ctx = self._run_full_pipeline(opportunities=[])
        output = capsys.readouterr().out
        assert "NO_OPPORTUNITIES" in output

    def test_scan_sentiment_failure_graceful(self, capsys):
        """Sentiment API failure doesn't crash pipeline — uses fallback F&G=50."""
        sa = MagicMock()
        sa.get_market_sentiment.side_effect = Exception("API down")

        bc = _make_bc()
        scanner = MagicMock()
        scanner.scan_all.return_value = [_make_opportunity(score=75)]
        scanner.get_top_movers.return_value = []
        rm = _make_risk_manager()
        notifier = _make_notifier()
        bear_analyst = MagicMock()
        bear_result = MagicMock()
        bear_result.veto = False
        bear_result.bear_score = 25
        bear_result.reasons = []
        bear_analyst.analyze.return_value = bear_result

        from contextlib import ExitStack
        patches = [
            ("src.scan_orchestrator.BinanceClient", {"return_value": bc}),
            ("src.scan_orchestrator.MarketScanner", {"return_value": scanner}),
            ("src.scan_orchestrator.FeishuNotifier", {"return_value": notifier}),
            ("src.scan_orchestrator.SentimentAnalyzer", {"return_value": sa}),
            ("src.scan_orchestrator.PortfolioManager", {}),
            ("src.risk_manager.RiskManager", {"return_value": rm}),
            ("src.market_researcher.MarketResearcher", {}),
            ("src.scan_orchestrator.BearAnalyst", {"return_value": bear_analyst}),
            ("src.strategy_adaptor.StrategyAdaptor", {}),
            ("src.strategy_registry.StrategyRegistry", {}),
            ("src.dimension_scorer.DimensionScorer", {}),
            ("src.scan_orchestrator.PositionOptimizer", {}),
            ("src.scan_orchestrator.clear_pending", {}),
            ("src.scan_orchestrator.save_pending", {}),
            ("src.scan_orchestrator.execute_auto_trade", {}),
            ("src.trade_outcome_recorder.TradeOutcomeRecorder", {}),
        ] + _submodule_patches(bc=bc, scanner=scanner, notifier=notifier, sa=sa, bear_analyst=bear_analyst)
        with ExitStack() as stack:
            mocks = {}
            for target, kwargs in patches:
                mocks[target] = stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.dict(os.environ, {"AUTO_EXECUTE": "true", "TRADING_MODE": "paper"}))

            mock_mr = mocks["src.market_researcher.MarketResearcher"]
            mock_adaptor = mocks["src.strategy_adaptor.StrategyAdaptor"]
            mock_reg = mocks["src.strategy_registry.StrategyRegistry"]
            mock_dim = mocks["src.dimension_scorer.DimensionScorer"]
            mock_exec = mocks["src.execute_phases.execute_auto_trade"]
            mock_mr.return_value.research.return_value = {
                "score_adjustment": 0,
                "confidence": 0.5,
                "sentiment_summary": "neutral",
                "news": [],
                "catalysts": [],
                "onchain": {},
            }
            mock_adaptor.return_value.adapt.return_value = {
                "regime": "NEUTRAL",
                "global": {
                    "score_threshold": 60,
                    "funding_signal": "N/A",
                    "cash_reserve_pct": 30,
                    "max_position_pct": 15,
                    "max_total_exposure_pct": 70,
                },
                "strategies": {
                    "trend": {
                        "enabled": True,
                        "size_multiplier": 1.0,
                        "sl_pct": 2.0,
                        "tp_levels": [{"pct": 2.0, "size_pct": 33}],
                        "max_hold_hours": 24,
                        "reason": "",
                    }
                },
            }
            mock_dim.return_value.score_all.return_value = {
                "resonance": "NEUTRAL",
                "score": 50,
            }
            mock_dim.return_value.format_report.return_value = ""
            mock_reg.return_value.select_best.return_value = (
                "trend",
                0.7,
                "ok",
                {"weight": 1.0},
            )
            mock_exec.return_value = {
                "success": True,
                "qty": 10.0,
                "price": 100.0,
                "tier": "MED",
                "invest_pct": 15,
                "error": None,
                "kelly": {"win_rate": 0.6, "confidence": "medium"},
            }

            from main import cmd_cron_scan

            cmd_cron_scan()

        # Pipeline should still complete successfully despite sentiment failure
        mock_exec.assert_called_once()

    # ── Phase 2: Research ────────────────────────────────────────

    def test_research_adjusts_score_up(self, capsys):
        """Positive research adjustment flows through to execution."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=70)],
            research_adj=10.0,
            threshold=60,
        )
        output = capsys.readouterr().out
        # Should show RESEARCH with positive adjustment
        assert "RESEARCH" in output
        # Should proceed to execution since 70+10=80 > 60
        ctx["mock_exec"].assert_called_once()

    def test_research_adjusts_score_below_threshold(self, capsys):
        """Research lowering score below threshold blocks execution."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=62)],
            research_adj=-10.0,  # 62 - 10 = 52 < 60
            threshold=60,
        )
        output = capsys.readouterr().out
        assert "SCORE_BELOW_THRESHOLD" in output
        ctx["mock_exec"].assert_not_called()

    def test_bear_veto_blocks_execution(self, capsys):
        """Bear analyst veto blocks the trade."""
        ctx = self._run_full_pipeline(
            bear_veto=True,
            bear_score=80,
        )
        output = capsys.readouterr().out
        assert "BEAR_VETO" in output
        ctx["mock_exec"].assert_not_called()

    def test_bear_penalty_reduces_score(self, capsys):
        """High bear score penalizes adjusted score."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=70)],
            research_adj=5.0,  # 70+5=75
            bear_score=65,  # penalty = (65-50)*0.3 = 4.5 → 75-4.5 = 70.5
            threshold=60,
        )
        output = capsys.readouterr().out
        assert "Bear penalty" in output
        # 70.5 > 60, so execution should proceed
        ctx["mock_exec"].assert_called_once()

    def test_risk_check_blocks_trade(self, capsys):
        """Risk manager blocking prevents research and execution."""
        ctx = self._run_full_pipeline(risk_allowed=False)
        output = capsys.readouterr().out
        assert "RISK_BLOCKED" in output
        ctx["mock_exec"].assert_not_called()

    # ── Phase 3: Execute ─────────────────────────────────────────

    def test_auto_execute_triggered(self):
        """AUTO_EXECUTE=true triggers trade execution."""
        ctx = self._run_full_pipeline(auto_execute="true")
        ctx["mock_exec"].assert_called_once()

    def test_auto_execute_disabled_saves_pending(self, capsys):
        """AUTO_EXECUTE=false saves pending instead of executing."""
        ctx = self._run_full_pipeline(auto_execute="false")
        ctx["mock_exec"].assert_not_called()
        ctx["mock_save"].assert_called_once()
        output = capsys.readouterr().out
        assert "YES SOLUSDT" in output

    # ── Regime Guards ────────────────────────────────────────────

    def test_extreme_fear_regime_raises_threshold(self, capsys):
        """EXTREME_FEAR + surge SILENCE raises threshold to 85 (full guard).

        Guard is surge-aware since 4b63560 (Plan A): EXTREME_FEAR + SILENCE
        -> max(threshold, 85). Score 84 < 85 -> blocked.
        """
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=78)],
            strategy_regime="EXTREME_FEAR",
            threshold=60,
        )
        output = capsys.readouterr().out
        assert "REGIME_GUARD" in output
        # Final 78+5(research)=83 < 85 (extreme fear full guard) → blocked
        ctx["mock_exec"].assert_not_called()

    def test_fear_regime_non_bullish_raises_threshold(self, capsys):
        """FEAR + non-BULLISH BTC + surge SILENCE raises threshold to 80."""
        # Override risk manager to return NEUTRAL trend (not BULLISH)
        rm = _make_risk_manager()
        rm.trend_filter.check_trend.return_value = {
            "trend": "NEUTRAL",
            "score": 45,
            "adx": 20,
            "allow_long": True,
            "size_multiplier": 1.0,
            "factors": {},
        }
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(score=73)],
            strategy_regime="FEAR",
            threshold=60,
            risk_manager=rm,
        )
        output = capsys.readouterr().out
        assert "REGIME_GUARD" in output
        ctx["mock_exec"].assert_not_called()

    # ── Data Flow Verification ───────────────────────────────────

    def test_execution_receives_correct_params(self):
        """Verify execute_auto_trade receives the right parameters from upstream."""
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(symbol="ETHUSDT", price=3500.0, score=80)],
            research_adj=5.0,
            threshold=60,
        )
        call_kwargs = ctx["mock_exec"].call_args
        assert call_kwargs[1]["symbol"] == "ETHUSDT"
        assert call_kwargs[1]["price"] == 3500.0
        assert call_kwargs[1]["score"] > 60  # adjusted score above threshold

    def test_research_receives_top_opportunities(self):
        """Verify researcher.research is called for top opportunities."""
        opps = [_make_opportunity(f"SOL{i}USDT", score=75 - i) for i in range(3)]
        ctx = self._run_full_pipeline(opportunities=opps, threshold=60)
        # Researcher should be called (at least once for the top opportunity)
        assert ctx["researcher"].research.call_count >= 1

    # ── Edge Cases ───────────────────────────────────────────────

    def test_multiple_opportunities_picks_best(self, capsys):
        """Pipeline selects highest-scored opportunity after research."""
        opps = [
            _make_opportunity("LOWUSDT", price=10, score=65),
            _make_opportunity("HIGHUSDT", price=200, score=85),
        ]
        ctx = self._run_full_pipeline(opportunities=opps, threshold=60)
        output = capsys.readouterr().out
        # Should select HIGHUSDT (higher score)
        if ctx["mock_exec"].called:
            call_sym = ctx["mock_exec"].call_args[1]["symbol"]
            assert call_sym == "HIGHUSDT"

    def test_pipeline_handles_research_timeout(self, capsys):
        """Research timeout doesn't crash pipeline."""
        mr = MagicMock()
        mr.research.side_effect = Exception("Research timeout")

        bc = _make_bc()
        scanner = MagicMock()
        scanner.scan_all.return_value = [_make_opportunity(score=75)]
        scanner.get_top_movers.return_value = []
        rm = _make_risk_manager()
        notifier = _make_notifier()

        from contextlib import ExitStack
        sa = _make_sentiment()
        patches = [
            ("src.scan_orchestrator.BinanceClient", {"return_value": bc}),
            ("src.scan_orchestrator.MarketScanner", {"return_value": scanner}),
            ("src.scan_orchestrator.FeishuNotifier", {"return_value": notifier}),
            ("src.scan_orchestrator.SentimentAnalyzer", {"return_value": sa}),
            ("src.scan_orchestrator.PortfolioManager", {}),
            ("src.risk_manager.RiskManager", {"return_value": rm}),
            ("src.market_researcher.MarketResearcher", {"return_value": mr}),
            ("src.scan_orchestrator.BearAnalyst", {}),
            ("src.strategy_adaptor.StrategyAdaptor", {}),
            ("src.strategy_registry.StrategyRegistry", {}),
            ("src.dimension_scorer.DimensionScorer", {}),
            ("src.scan_orchestrator.PositionOptimizer", {}),
            ("src.scan_orchestrator.clear_pending", {}),
            ("src.scan_orchestrator.save_pending", {}),
            ("src.scan_orchestrator.execute_auto_trade", {}),
        ] + _submodule_patches(bc=bc, scanner=scanner, notifier=notifier, sa=sa)
        with ExitStack() as stack:
            mocks = {}
            for target, kwargs in patches:
                mocks[target] = stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.dict(os.environ, {"AUTO_EXECUTE": "true", "TRADING_MODE": "paper"}))

            mock_adaptor = mocks["src.strategy_adaptor.StrategyAdaptor"]
            mock_dim = mocks["src.dimension_scorer.DimensionScorer"]
            mock_adaptor.return_value.adapt.return_value = {
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
            mock_dim.return_value.score_all.return_value = {
                "resonance": "NEUTRAL",
                "score": 50,
            }
            mock_dim.return_value.format_report.return_value = ""

            from main import cmd_cron_scan

            cmd_cron_scan()

        output = capsys.readouterr().out
        # Pipeline should handle gracefully — either NO_OPPORTUNITIES or SCORE_BELOW_THRESHOLD
        assert "NO_OPPORTUNITIES" in output or "SCORE_BELOW_THRESHOLD" in output

    def test_execution_failure_handled(self, capsys):
        """Trade execution failure doesn't crash the pipeline."""
        bc = _make_bc()
        scanner = MagicMock()
        scanner.scan_all.return_value = [_make_opportunity(score=75)]
        scanner.get_top_movers.return_value = []
        rm = _make_risk_manager()
        notifier = _make_notifier()

        from contextlib import ExitStack
        sa = _make_sentiment()
        patches = [
            ("src.scan_orchestrator.BinanceClient", {"return_value": bc}),
            ("src.scan_orchestrator.MarketScanner", {"return_value": scanner}),
            ("src.scan_orchestrator.FeishuNotifier", {"return_value": notifier}),
            ("src.scan_orchestrator.SentimentAnalyzer", {"return_value": sa}),
            ("src.scan_orchestrator.PortfolioManager", {}),
            ("src.risk_manager.RiskManager", {"return_value": rm}),
            ("src.market_researcher.MarketResearcher", {}),
            ("src.scan_orchestrator.BearAnalyst", {}),
            ("src.strategy_adaptor.StrategyAdaptor", {}),
            ("src.strategy_registry.StrategyRegistry", {}),
            ("src.dimension_scorer.DimensionScorer", {}),
            ("src.scan_orchestrator.PositionOptimizer", {}),
            ("src.scan_orchestrator.clear_pending", {}),
            ("src.scan_orchestrator.save_pending", {}),
            ("src.scan_orchestrator.execute_auto_trade", {}),
            ("src.trade_outcome_recorder.TradeOutcomeRecorder", {}),
            ("src.self_healer.diagnose_and_fix", {}),
        ] + _submodule_patches(bc=bc, scanner=scanner, notifier=notifier, sa=sa)
        with ExitStack() as stack:
            mocks = {}
            for target, kwargs in patches:
                mocks[target] = stack.enter_context(patch(target, **kwargs))
            stack.enter_context(patch.dict(os.environ, {"AUTO_EXECUTE": "true", "TRADING_MODE": "paper"}))

            mock_mr = mocks["src.market_researcher.MarketResearcher"]
            mock_ba = mocks["src.scan_orchestrator.BearAnalyst"]
            # Link research_phase's BearAnalyst to the same mock
            mocks["src.research_phase.BearAnalyst"].return_value = mock_ba.return_value
            mock_adaptor = mocks["src.strategy_adaptor.StrategyAdaptor"]
            mock_reg = mocks["src.strategy_registry.StrategyRegistry"]
            mock_dim = mocks["src.dimension_scorer.DimensionScorer"]
            mock_exec = mocks["src.execute_phases.execute_auto_trade"]
            mock_heal = mocks["src.self_healer.diagnose_and_fix"]
            mock_mr.return_value.research.return_value = {
                "score_adjustment": 5,
                "confidence": 0.7,
                "sentiment_summary": "ok",
                "news": [],
                "catalysts": [],
                "onchain": {},
            }
            mock_adaptor.return_value.adapt.return_value = {
                "regime": "NEUTRAL",
                "global": {
                    "score_threshold": 60,
                    "funding_signal": "N/A",
                    "cash_reserve_pct": 30,
                    "max_position_pct": 15,
                    "max_total_exposure_pct": 70,
                },
                "strategies": {
                    "trend": {
                        "enabled": True,
                        "size_multiplier": 1.0,
                        "sl_pct": 2.0,
                        "tp_levels": [{"pct": 2.0, "size_pct": 33}],
                        "max_hold_hours": 24,
                        "reason": "",
                    }
                },
            }
            mock_dim.return_value.score_all.return_value = {
                "resonance": "NEUTRAL",
                "score": 50,
            }
            mock_dim.return_value.format_report.return_value = ""
            mock_reg.return_value.select_best.return_value = (
                "trend",
                0.7,
                "ok",
                {"weight": 1.0},
            )
            bear_result = MagicMock()
            bear_result.veto = False
            bear_result.bear_score = 30
            bear_result.reasons = []
            mock_ba.return_value.analyze.return_value = bear_result
            # Execution fails
            mock_exec.return_value = {
                "success": False,
                "qty": 0,
                "price": 0,
                "tier": None,
                "invest_pct": 0,
                "error": "API error",
            }
            mock_heal.return_value = {"diagnosed": False, "fixed": False}

            from main import cmd_cron_scan

            cmd_cron_scan()

        output = capsys.readouterr().out
        # Should not crash — failure is handled gracefully
        assert (
            "Execution failed" in output
            or "execution failed" in output.lower()
            or "❌" in output
        )

    # ── Full Happy Path ──────────────────────────────────────────

    def test_full_happy_path_e2e(self, capsys):
        """Complete happy path: scan finds opportunity → research confirms → trade executes.

        This is the definitive E2E smoke test validating all three phases work together.
        """
        ctx = self._run_full_pipeline(
            opportunities=[_make_opportunity(symbol="SOLUSDT", price=150.0, score=78)],
            fng=55,
            fng_label="Neutral",
            risk_allowed=True,
            research_adj=8.0,  # 78+8=86
            bear_veto=False,
            bear_score=25,
            auto_execute="true",
            usdt_free=2000,
            strategy_regime="NEUTRAL",
            threshold=60,
        )

        output = capsys.readouterr().out

        # Phase 1: Scan should produce strategy adaptation output
        assert "STRATEGY_ADAPT" in output

        # Phase 2: Research should show results
        assert "RESEARCH" in output
        assert "SELECTED" in output

        # Phase 3: Execution should fire
        ctx["mock_exec"].assert_called_once()
        exec_call = ctx["mock_exec"].call_args[1]
        assert exec_call["symbol"] == "SOLUSDT"
        assert exec_call["price"] == 150.0
        assert exec_call["score"] > 60

        # Should print opportunity details
        assert "OPPORTUNITY" in output
        assert "SOLUSDT" in output
