"""bug#25 (2026-08-24): weekly_backtest must mirror live deployment.

Live trading is protected by BTC 200SMA trend filter since 2026-08-03.
The weekly backtest used to simulate trend-OFF only, alerting on
bear-market segments live would never trade (false alarms 8/10-8/24).

Fix: run BOTH modes; degradation judged on trend-ON (live) mode with a
min-trades floor; trend-OFF kept as alpha reference only.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from weekly_backtest import evaluate_degradation, MIN_TRADES_FOR_DEGRADATION


def _r(ret, dd, trades=50, **kw):
    d = {"total_return_pct": ret, "max_drawdown_pct": dd, "total_trades": trades}
    d.update(kw)
    return d


class TestEvaluateDegradation:
    def test_bear_segment_trend_off_style_all_degraded(self):
        """Classic false-alarm pattern: all symbols deep negative with high DD."""
        ind = {
            "SOLUSDT": _r(-6.9, 26.6, 263),
            "ETHUSDT": _r(-14.4, 26.6, 225),
            "AVAXUSDT": _r(-18.1, 34.5, 227),
        }
        degradation, low_sample = evaluate_degradation(ind)
        assert len(degradation) == 3
        assert low_sample == []

    def test_trend_gate_period_low_sample_exempt(self):
        """Trend-gated window: few/no trades must NOT be judged (the bug#25 core).

        BTC only crossed above 200SMA on 2026-08-19, so a 90d window yields
        near-zero trades in trend-ON mode — these must be exempt, not alerts.
        """
        ind = {
            "SOLUSDT": _r(0.0, 0.0, 0),
            "ETHUSDT": _r(-8.0, 20.0, 3),
            "AVAXUSDT": _r(-12.0, 25.0, MIN_TRADES_FOR_DEGRADATION - 1),
        }
        degradation, low_sample = evaluate_degradation(ind)
        assert degradation == []
        assert len(low_sample) == 3

    def test_boundary_exactly_min_trades_judged(self):
        """Exactly min_trades trades ARE judged."""
        ind = {"SOLUSDT": _r(-6.0, 20.0, MIN_TRADES_FOR_DEGRADATION)}
        degradation, low_sample = evaluate_degradation(ind)
        assert len(degradation) == 1
        assert low_sample == []

    def test_criteria_requires_both_ret_and_dd(self):
        """Negative return but low DD (or vice versa) must not alert."""
        ind = {
            "SOLUSDT": _r(-6.0, 14.9),   # DD below threshold
            "ETHUSDT": _r(-4.9, 30.0),   # return above threshold
            "BNBUSDT": _r(+5.0, 20.0),   # positive return
        }
        degradation, _ = evaluate_degradation(ind)
        assert degradation == []

    def test_error_symbols_skipped(self):
        ind = {"SOLUSDT": {"error": "klines unavailable"}, "ETHUSDT": _r(-6.0, 20.0, 50)}
        degradation, low_sample = evaluate_degradation(ind)
        assert len(degradation) == 1
        assert all("SOL" not in s for s in degradation + low_sample)

    def test_mixed_realistic_week(self):
        """Realistic post-fix week: 2 gated symbols exempt, 1 healthy, 1 degraded."""
        ind = {
            "SOLUSDT": _r(0.0, 0.0, 0),          # gated whole window
            "ETHUSDT": _r(-1.2, 3.0, 22),        # traded, healthy-ish
            "AVAXUSDT": _r(-9.5, 22.0, 40),      # traded, degraded
            "BNBUSDT": _r(2.1, 4.0, 18),         # traded, fine
            "LINKUSDT": _r(-2.0, 8.0, 7),        # gated mostly, exempt
        }
        degradation, low_sample = evaluate_degradation(ind)
        assert degradation == ["AVAX: -9.5% (DD 22.0%)"]
        assert len(low_sample) == 2
        assert any("SOL" in s for s in low_sample)
        assert any("LINK" in s for s in low_sample)


class TestScriptStructure:
    def test_main_runs_both_modes(self, monkeypatch, tmp_path, capsys):
        """main() must call run_multi twice: trend ON then OFF; status reflects live mode."""
        import weekly_backtest as wb

        calls = []

        class FakeEngine:
            def __init__(self, **kw):
                pass

            def run_multi(self, symbols, interval, days, enable_trend_filter, enable_trailing_stop):
                calls.append(enable_trend_filter)
                if enable_trend_filter:
                    # LIVE mode: mostly gated (0 trades), one degraded symbol with trades
                    individual = {
                        "SOLUSDT": _r(0.0, 0.0, 0),
                        "ETHUSDT": _r(-9.0, 25.0, 30),
                    }
                else:
                    # ALPHA mode: everything traded, deep negative — must NOT alert
                    individual = {
                        "SOLUSDT": _r(-6.9, 26.6, 263),
                        "ETHUSDT": _r(-14.4, 26.6, 225),
                    }
                summary = {"total_return_pct": -5.0, "total_pnl_usdt": -500.0,
                           "win_rate": 55, "profit_factor": 0.7}
                return {"summary": summary, "individual": individual}

        class FakeClient:
            def __init__(self, **kw):
                pass

        monkeypatch.setattr(wb, "BacktestEngine", FakeEngine)
        monkeypatch.setattr(wb, "BinanceClient", FakeClient)

        status_path = tmp_path / "trading-systems" / "crypto-ai-trader" / "logs" / "weekly_backtest_status.json"
        monkeypatch.setattr(
            "pathlib.Path.home", lambda *a, **k: tmp_path
        )
        # wb writes to Path.home()/trading-systems/crypto-ai-trader/logs/...
        (tmp_path / "trading-systems" / "crypto-ai-trader" / "logs").mkdir(parents=True)

        import json
        exit_code = None
        try:
            wb.main()
        except SystemExit as e:
            exit_code = e.code

        # Both modes invoked, trend ON first
        assert calls == [True, False]
        out = capsys.readouterr().out
        # LIVE section present with trend-ON marker
        assert "趨勢過濾 ON" in out
        # ALPHA section present, reference only
        assert "趨勢過濾 OFF" in out
        # ETH degraded in live mode (30 trades) -> alert + exit 1
        assert "ETH: -9.0% (DD 25.0%)" in out
        assert exit_code == 1
        # SOL gated -> low sample note, not alert
        assert "低樣本" in out
        assert any("SOL" in s for s in out.splitlines())

        status = json.loads(status_path.read_text())
        assert status["mode"] == "trend_on_live"
        assert status["has_degradation"] is True
        assert len(status["low_sample"]) == 1
        assert status["alpha_reference"]["total_return_pct"] == -5.0
