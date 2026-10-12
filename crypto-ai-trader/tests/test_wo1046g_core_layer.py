"""WO-1046g S3: core/satellite layering tests.

Spec (Phase 3, Travis 2026-10-13 00:41 P3 authorization):
  * BTC core channel: CONFIRMED_BULL + BTC over-line → BTC preferred entry
  * core_quota = equity × core_allocation_pct, hard-clamped to [0.40, 0.50]
  * executor: core uses core_max_pct cap (50%) instead of the satellite
    max_position_pct; cash-reserve and total-exposure caps still apply
  * kelly fallback path: core position_scale (1.5x)
  * portfolio: layer column (core/satellite); NULL = satellite
  * exits: core skips zombie exit, strategic hold 168h (satellite: 48h)
  * three-layer config defaults < risk_limits.yaml < kv core_layer_config;
    kv {"enabled": false} = one-key rollback to pure satellite semantics
  * bear veto still applies to BTC: vetoed core falls back to the natural
    best satellite candidate (core is a preference, never a gate override)
"""

import json
import os
import sqlite3
import time as _time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────── config loader ────

@pytest.fixture
def db():
    from src.state_db import get_state_db

    d = get_state_db()
    yield d
    d.kv_set("core_layer_config", None)


@pytest.fixture(autouse=True)
def _core_kv_enabled(request):
    """Mechanism tests run with the core channel ON via kv — the exact
    flip performed on WO-1046g acceptance (yaml ships enabled=false
    pending sign-off; kv {"enabled": true} turns it on with no push).
    TestCoreCfg manages its own kv state and is excluded."""
    if request.cls is not None and request.cls.__name__ in (
            "TestCorePreflight", "TestSelectBestCandidateCore",
            "TestResearchCoreChannel", "TestExecutorCoreQuota"):
        from src.state_db import get_state_db

        get_state_db().kv_set("core_layer_config", {"enabled": True})
        yield
        get_state_db().kv_set("core_layer_config", None)
    else:
        yield


class TestCoreCfg:
    def test_defaults_from_yaml(self):
        from src.core_layer import get_core_cfg

        # WO-1046g delivery: yaml ships enabled=false pending Travis
        # acceptance (CONFIRMED_BULL is live — no capital moves before
        # sign-off). Acceptance flip = yaml true OR kv {"enabled": true}.
        cfg = get_core_cfg()
        assert cfg["enabled"] is False
        assert cfg["core_symbol"] == "BTCUSDT"
        assert cfg["core_allocation_pct"] == 0.45
        assert cfg["core_max_pct"] == 50.0
        assert cfg["core_sl_pct"] == 3.5
        assert cfg["core_max_hold_hours"] == 168.0
        assert cfg["core_position_scale"] == 1.5
        assert cfg["core_zombie_exit"] is False

    def test_kv_override_pass_through(self, db):
        db.kv_set("core_layer_config", {"core_sl_pct": 2.0, "core_max_hold_hours": 240})
        from src.core_layer import get_core_cfg

        cfg = get_core_cfg()
        assert cfg["core_sl_pct"] == 2.0
        assert cfg["core_max_hold_hours"] == 240.0
        assert cfg["core_allocation_pct"] == 0.45  # untouched key

    def test_guardrail_clamps_hi(self, db):
        """kv tries 0.90 — the spec guardrail clamps to 0.50."""
        db.kv_set("core_layer_config", {"core_allocation_pct": 0.90})
        from src.core_layer import get_core_cfg

        assert get_core_cfg()["core_allocation_pct"] == 0.50

    def test_guardrail_clamps_lo(self, db):
        db.kv_set("core_layer_config", {"core_allocation_pct": 0.10})
        from src.core_layer import get_core_cfg

        assert get_core_cfg()["core_allocation_pct"] == 0.40

    def test_guardrail_bad_bounds_falls_back(self, db):
        """min > max (config typo) → spec defaults, never an inverted band."""
        db.kv_set(
            "core_layer_config",
            {"core_allocation_pct_min": 0.60, "core_allocation_pct_max": 0.50,
             "core_allocation_pct": 0.55},
        )
        from src.core_layer import get_core_cfg

        cfg = get_core_cfg()
        assert cfg["core_allocation_pct"] == 0.45
        assert cfg["core_allocation_pct_min"] == 0.40
        assert cfg["core_allocation_pct_max"] == 0.50

    def test_acceptance_flip_and_rollback(self, db):
        """yaml ships OFF; the acceptance flip is kv {"enabled": true}
        (no code push); clearing kv rolls back to the yaml default;
        kv {"enabled": false} is the live kill switch."""
        from src.core_layer import get_core_cfg

        assert get_core_cfg()["enabled"] is False      # yaml default
        db.kv_set("core_layer_config", {"enabled": True})   # flip
        assert get_core_cfg()["enabled"] is True
        db.kv_set("core_layer_config", {"enabled": False})  # kill switch
        assert get_core_cfg()["enabled"] is False
        db.kv_set("core_layer_config", None)                # back to yaml
        assert get_core_cfg()["enabled"] is False


# ─────────────────────────────────────────────────────── core preflight ────

def _client_no_holdings():
    c = MagicMock()
    c.get_account.return_value = {"balances": [{"asset": "USDT", "free": "400", "locked": "0"}]}
    c.get_24hr_stats.return_value = {"last_price": "84000"}
    return c


def _opps(scores=None):
    scores = scores or {"BTCUSDT": 82, "SOLUSDT": 78}
    return [{"symbol": s, "score": sc, "price": 100.0} for s, sc in scores.items()]


def _ctx(tier="CONFIRMED_BULL", positions=None):
    return {
        "bull_tier": tier,
        "portfolio": SimpleNamespace(positions=positions or {}),
    }


class TestCorePreflight:
    def test_all_conditions_pass(self, db):
        from src.core_layer import core_preflight

        ok, reason = core_preflight(_ctx(), _client_no_holdings(), _opps(), 78)
        assert ok is True
        assert "CONFIRMED_BULL" in reason

    def test_disabled(self, db):
        db.kv_set("core_layer_config", {"enabled": False})
        from src.core_layer import core_preflight

        ok, reason = core_preflight(_ctx(), _client_no_holdings(), _opps(), 78)
        assert ok is False and "disabled" in reason

    # NB: tier=None (missing ctx key) is NOT a wrong tier — it triggers the
    # direct _check_btc_trend fallback, covered by test_tier_fallback.
    @pytest.mark.parametrize("tier", ["BULL", "NEUTRAL", "BEARISH", "ACCUMULATION"])
    def test_wrong_tier(self, db, tier):
        from src.core_layer import core_preflight

        ok, _ = core_preflight(_ctx(tier=tier), _client_no_holdings(), _opps(), 78)
        assert ok is False

    def test_tier_fallback_direct_lookup(self, db, monkeypatch):
        """Missing ctx key → direct _check_btc_trend fallback (scan compat)."""
        monkeypatch.setattr(
            "src.trade_executor._check_btc_trend",
            lambda: (1.0, {"tier": "CONFIRMED_BULL"}),
        )
        from src.core_layer import core_preflight

        ok, _ = core_preflight({"portfolio": SimpleNamespace(positions={})},
                               _client_no_holdings(), _opps(), 78)
        assert ok is True

    def test_btc_not_in_opportunities(self, db):
        from src.core_layer import core_preflight

        opps = [{"symbol": "SOLUSDT", "score": 90, "price": 100.0}]
        ok, reason = core_preflight(_ctx(), _client_no_holdings(), opps, 78)
        assert ok is False and "not in opportunities" in reason

    def test_btc_below_threshold(self, db):
        from src.core_layer import core_preflight

        ok, reason = core_preflight(_ctx(), _client_no_holdings(),
                                    _opps({"BTCUSDT": 70, "SOLUSDT": 78}), 78)
        assert ok is False and "< threshold" in reason

    def test_btc_held(self, db):
        c = MagicMock()
        c.get_account.return_value = {
            "balances": [
                {"asset": "USDT", "free": "400", "locked": "0"},
                {"asset": "BTC", "free": "0.002", "locked": "0"},  # $168 — held
            ]
        }
        c.get_24hr_stats.return_value = {"last_price": "84000"}
        from src.core_layer import core_preflight

        ok, reason = core_preflight(_ctx(), c, _opps(), 78)
        assert ok is False and "already held" in reason

    def test_core_slot_occupied(self, db):
        positions = {"BTCUSDT": {"quantity": 0.002, "layer": "core"}}
        from src.core_layer import core_preflight

        ok, reason = core_preflight(_ctx(positions=positions),
                                    _client_no_holdings(), _opps(), 78)
        assert ok is False and "occupied" in reason

    def test_satellite_positions_do_not_block(self, db):
        positions = {"SOLUSDT": {"quantity": 1.0, "layer": "satellite"}}
        from src.core_layer import core_preflight

        ok, _ = core_preflight(_ctx(positions=positions),
                               _client_no_holdings(), _opps(), 78)
        assert ok is True

    def test_null_layer_is_satellite(self, db):
        """Pre-WO-1046g rows (layer NULL/missing) never count as core."""
        positions = {"BTCUSDT": {"quantity": 0.002, "layer": None}}
        from src.core_layer import core_active_holding

        assert core_active_holding(positions) is None


# ─────────────────────────────────────────── selection: core preference ───

def _rr(symbol, raw, adj):
    return {symbol: ({"symbol": symbol, "score": raw, "price": 100.0},
                    {"score_adjustment": adj - raw,
                     "sentiment_summary": "", "confidence": "HIGH",
                     "news": [], "onchain": {}})}


class TestSelectBestCandidateCore:
    def _client(self, held=()):
        c = MagicMock()
        bals = [{"asset": "USDT", "free": "400", "locked": "0"}]
        bals += [{"asset": a, "free": "1", "locked": "0"} for a in held]
        c.get_account.return_value = {"balances": bals}
        c.get_24hr_stats.return_value = {"last_price": "100"}
        return c

    def test_core_wins_over_higher_natural(self):
        from src.research_phase import _select_best_candidate

        rr = {**_rr("BTCUSDT", 80, 82), **_rr("SOLUSDT", 85, 90)}
        sym, adj, fb = _select_best_candidate(rr, self._client(), 78,
                                              core_prefer_symbol="BTCUSDT")
        assert sym == "BTCUSDT" and adj == 82 and fb is False

    def test_core_below_threshold_post_research(self):
        """BTC research adj drops below threshold → natural best wins."""
        from src.research_phase import _select_best_candidate

        rr = {**_rr("BTCUSDT", 80, 76), **_rr("SOLUSDT", 85, 90)}
        sym, adj, _ = _select_best_candidate(rr, self._client(), 78,
                                             core_prefer_symbol="BTCUSDT")
        assert sym == "SOLUSDT" and adj == 90

    def test_core_held_falls_back_to_natural(self):
        from src.research_phase import _select_best_candidate

        rr = {**_rr("BTCUSDT", 80, 82), **_rr("SOLUSDT", 85, 90)}
        c = self._client(held=["BTC"])
        c.get_24hr_stats.return_value = {"last_price": "84000"}
        sym, _, _ = _select_best_candidate(rr, c, 78,
                                           core_prefer_symbol="BTCUSDT")
        assert sym == "SOLUSDT"

    def test_no_core_param_is_legacy(self):
        """No core preference → pure natural best (legacy bit-identical)."""
        from src.research_phase import _select_best_candidate

        rr = {**_rr("BTCUSDT", 80, 82), **_rr("SOLUSDT", 85, 90)}
        sym, adj, _ = _select_best_candidate(rr, self._client(), 78)
        assert sym == "SOLUSDT" and adj == 90


# ─────────────────────────────────── research integration (core pipeline) ─

def _ctx_full(bull_tier="CONFIRMED_BULL"):
    client = MagicMock()
    client.get_account.return_value = {
        "balances": [{"asset": "USDT", "free": "400", "locked": "0"}]
    }
    client.get_24hr_stats.return_value = {"last_price": "84000"}
    client.get_klines.side_effect = Exception("offline")  # → klines_data=[]
    return {
        "client": client,
        "researcher": MagicMock(),
        "risk_mgr": MagicMock(),
        "notifier": MagicMock(),
        "opportunities": [
            {"symbol": "BTCUSDT", "score": 82, "price": 84000,
             "signals": ["BTC trend"], "fear_mode": False},
            {"symbol": "SOLUSDT", "score": 80, "price": 100.0,
             "signals": ["momentum"], "fear_mode": False},
        ],
        "dynamic_threshold": 78,
        "pre_time_decay_threshold": 78,
        "adapted": {
            "regime": "GREED",
            "strategies": {
                "trend": {"enabled": True, "sl_pct": 5.0,
                          "tp_levels": [{"pct": 4.0, "size_pct": 50}],
                          "max_hold_hours": 48, "size_multiplier": 1.0},
            },
            "global": {},
            "dca_params": {}, "rsi_params": {}, "bandit_sltp": {},
        },
        "regime": "GREED",
        "fng": 70,
        "btc_trend": "BULLISH",
        "acct": {"balances": [{"asset": "USDT", "free": "400", "locked": "0"}]},
        "surge_result": {"alert_level": "SILENCE"},
        "bull_tier": bull_tier,
        "portfolio": SimpleNamespace(positions={}),
    }


def _research_results(ctx, btc_adj=2, sol_adj=5):
    opps = {o["symbol"]: o for o in ctx["opportunities"]}
    adjs = {"BTCUSDT": btc_adj, "SOLUSDT": sol_adj}
    # research only covers the symbols actually injected into top_n
    return {s: (o, {"score_adjustment": adjs[s], "sentiment_summary": "",
                    "confidence": "HIGH", "news": [], "onchain": {}})
            for s, o in opps.items() if s in adjs}


def _bear(veto=False, score=10):
    from src.bear_analyst import BearResult

    return BearResult(bear_score=score, veto=veto,
                      reasons=["test"], confidence="LOW")


class TestResearchCoreChannel:
    def _run(self, monkeypatch, ctx, bear_side_effect=None, btc_adj=2, sol_adj=5):
        import src.research_phase as rp

        # _filter_by_risk(opps, client, risk_mgr, acct, adapted, regime,
        #                 btc_trend, dynamic_threshold, ...) → (opps, thr, {})
        monkeypatch.setattr(rp, "_filter_by_risk",
                            lambda *a, **k: (a[0], a[7], {}))
        monkeypatch.setattr(rp, "_run_deep_research",
                            lambda *a, **k: _research_results(ctx, btc_adj, sol_adj))
        monkeypatch.setattr(rp, "clear_pending", lambda: None)
        monkeypatch.setattr(rp, "count_active_positions", lambda c: 0)

        class _J:
            def record_decision(self, *a, **k):
                pass

        monkeypatch.setattr(rp, "TradeJournal", _J)

        if bear_side_effect is None:
            bear_side_effect = [  # BTC first (selected), then SOL if fallback
                _bear(False, 10), _bear(False, 10), _bear(False, 10),
                _bear(False, 10),
            ]

        class _B:
            def __init__(self, *a, **k):
                self._calls = []

            def analyze(self, sym, *a, **k):
                r = bear_side_effect[len(self._calls) % len(bear_side_effect)]
                self._calls.append(1)
                return r

        # research_phase binds BearAnalyst at module import — patch in ITS
        # namespace or the real analyst (network deps) runs instead.
        monkeypatch.setattr(rp, "BearAnalyst", _B)
        return rp._step_research_top_n(ctx)

    def test_core_happy_path(self, monkeypatch, db):
        """CONFIRMED_BULL + BTC over-line → BTC selected, core params applied."""
        ctx = _ctx_full()
        out = self._run(monkeypatch, ctx)
        assert out is not None
        assert out["symbol"] == "BTCUSDT"
        assert out["core_layer"] is True
        assert out["stop_loss_pct"] == 3.5      # core narrow SL
        assert out["max_hold"] == 168           # 7d strategic hold

    def test_bear_veto_falls_back_to_satellite(self, monkeypatch, db):
        """Bear veto still effective on core BTC → natural best satellite."""
        ctx = _ctx_full()
        # BTC selected first → vetoed; fallback SOL → clean
        out = self._run(monkeypatch, ctx,
                        bear_side_effect=[_bear(True, 70), _bear(False, 10)])
        assert out is not None
        assert out["symbol"] == "SOLUSDT"
        assert out["core_layer"] is False
        # satellite semantics (FIX-11 safe default sl=4.0 in this harness —
        # the invariant is: NOT the core 3.5/168 override)
        assert out["stop_loss_pct"] != 3.5
        assert out["max_hold"] == 48

    def test_bear_veto_no_fallback_blocks(self, monkeypatch, db):
        """BTC vetoed and no other candidate qualifies → BLOCKED (None)."""
        ctx = _ctx_full()
        monkeypatch.setitem(ctx, "opportunities", [ctx["opportunities"][0]])
        out = self._run(monkeypatch, ctx,
                        bear_side_effect=[_bear(True, 70)])
        assert out is None

    def test_disabled_kv_is_pure_satellite(self, monkeypatch, db):
        db.kv_set("core_layer_config", {"enabled": False})
        ctx = _ctx_full()
        # SOL adj 85 > BTC adj 82 → natural best is SOL anyway
        out = self._run(monkeypatch, ctx, btc_adj=2, sol_adj=5)
        assert out is not None
        assert out["symbol"] == "SOLUSDT"
        assert out["core_layer"] is False

    def test_wrong_tier_is_pure_satellite(self, monkeypatch, db):
        ctx = _ctx_full(bull_tier="BULL")
        out = self._run(monkeypatch, ctx)
        assert out is not None
        assert out["symbol"] == "SOLUSDT"   # natural best (85 > 82)
        assert out["core_layer"] is False


# ─────────────────────────────────────────────── executor: quota and caps ──

def _mk_client(usdt=400.0, symbol="BTCUSDT"):
    c = MagicMock()
    bals = [{"asset": "USDT", "free": str(usdt), "locked": "0"}]
    c.get_account.return_value = {"balances": bals}
    c.get_free_balance.return_value = usdt
    c.get_24hr_stats.return_value = {"last_price": "84000"}
    c.get_ticker_price.return_value = 84000.0
    c.get_price_precision.return_value = 2
    c.place_market_buy.return_value = {
        "symbol": symbol, "orderId": 1, "status": "FILLED",
        "fills": [{"price": "84000.00", "qty": "0.002", "commission": "0.01"}],
    }
    c.place_order.return_value = {"symbol": symbol, "orderId": 2, "status": "NEW"}
    return c


def _exec(client, monkeypatch, core_layer=False, **kw):
    import src.trade_executor as te

    args = dict(
        symbol="BTCUSDT",
        price=84000.0,
        strategy="trend",
        stop_loss_pct=3.5,
        tp_levels=[{"pct": 4.0, "size_pct": 50}],
        stop_price=81000.0,
        max_hold=168,
        signals=["trend"],
        reason="test",
        score=80,
        cash_reserve_pct=10,
        max_position_pct=30,      # satellite cap (WO-1044-E)
        max_total_exposure_pct=95,
        core_layer=core_layer,
    )
    args.update(kw)

    class _P:
        def __init__(self, *a, **k):
            self.positions = {}
            self._db = None

        def update_balance(self, b):
            pass

        def add_position(self, **k):
            self.positions[k["symbol"]] = {"quantity": k["quantity"]}

    with patch("src.trade_executor.get_trading_client", return_value=client), \
         patch("src.trade_executor.FeishuNotifier"), \
         patch("src.trade_executor.count_active_positions", return_value=0), \
         patch("src.trade_executor.PortfolioManager", _P), \
         patch("src.circuit_breaker.CircuitBreaker") as cb, \
         patch("src.daily_loss_breaker.get_daily_loss_breaker") as dlb, \
         patch("src.drawdown_breaker.DrawdownBreaker") as ddb, \
         patch("src.kelly_sizer.KellyPositionSizer") as kelly, \
         patch("src.fee_optimizer.FeeOptimizer") as fee, \
         patch("src.smart_order.SmartOrder") as so, \
         patch("src.twap_vwap.should_use_twap", return_value=False), \
         patch("src.contextual_bandit.get_contextual_bandit") as bandit_mock:
        cb.return_value.is_tripped.return_value = False
        d = dlb.return_value
        d.check_daily_loss.return_value = {"tier": 0}
        d.should_close_all.return_value = False
        d.should_block_new_trades.return_value = False
        d.get_position_size_multiplier.return_value = 1.0
        ddb.return_value.check_drawdown.return_value = {"drawdown_pct": 0}
        fee.return_value.get_effective_fees.return_value = {"taker_fee": 0.001}
        kelly.return_value.get_position_size.return_value = {
            "position_pct": 0.10, "confidence": "estimated",
            "is_exploration": False, "win_rate": 0.5, "reward_risk": 1.0,
            "reason": "t",
        }
        kelly.return_value.adjust_for_portfolio.side_effect = lambda r, *a, **k: r
        so.return_value.get_symbol_filters.return_value = {
            "stepSize": 0.00001, "qty_decimals": 5,
            "minQty": 0.00001, "minNotional": 5.0,
        }
        bandit_mock.return_value.recommend_size.return_value = 1.0
        result = te.execute_auto_trade(**args)
    return result


class TestExecutorCoreQuota:
    def test_core_quota_45pct(self, monkeypatch, tmp_path):
        """core: order_value = equity × 0.45 — NOT the satellite 30% cap."""
        client = _mk_client(400.0)
        # executor records executed_qty from exchange fills — align the
        # mock fill with the computed 400×0.45/84000 = 0.0021428
        client.place_market_buy.return_value["fills"][0]["qty"] = "0.00214"
        r = _exec(client, monkeypatch, core_layer=True)
        assert r["success"] is True
        assert r["invest_pct"] == pytest.approx(45.0, abs=1.5)
        assert r["qty"] == pytest.approx(180.0 / 84000.0, rel=0.02)

    def test_satellite_keeps_30pct_cap(self, monkeypatch):
        """Control: satellite path is clamped by max_position_pct=30 —
        the diff proves the core exemption is real, not a global raise."""
        r = _exec(_mk_client(400.0), monkeypatch, core_layer=False)
        assert r["success"] is True
        # kelly "estimated" → tier fallback: 0.30 × 1.0 × 0.99 fee-reserve
        # = 29.7% ($118.8) — under the 30% cap; the core path reaches 45%.
        assert r["invest_pct"] == pytest.approx(29.7, abs=1.0)

    def test_core_guardrail_clamps_to_50(self, monkeypatch, db):
        """kv allocation 0.90 → guardrail 0.50 → 50% of equity."""
        db.kv_set("core_layer_config", {"core_allocation_pct": 0.90})
        r = _exec(_mk_client(400.0), monkeypatch, core_layer=True)
        assert r["success"] is True
        assert r["invest_pct"] == pytest.approx(50.0, abs=1.5)

    def test_core_cash_reserve_cap_not_waived(self, monkeypatch):
        """Systemic cushion check: reserve 60% clamps core 200 → 160."""
        r = _exec(_mk_client(400.0), monkeypatch, core_layer=True,
                  cash_reserve_pct=60)
        assert r["success"] is True
        assert r["invest_pct"] == pytest.approx(40.0, abs=1.5)  # 160/400

    def test_kelly_fallback_core_scale(self, monkeypatch):
        """_compute_kelly_sizing fallback path: pct × core_position_scale."""
        import src.trade_executor as te

        kelly = MagicMock()
        kelly.get_position_size.return_value = {
            "position_pct": 0.10, "confidence": "high",
            "is_exploration": False, "win_rate": 0.55,
            "reward_risk": 1.5, "reason": "t",
        }
        kelly.adjust_for_portfolio.side_effect = lambda r, **kw: r
        fee = MagicMock()
        fee.get_effective_fees.return_value = {"taker_fee": 0.001}
        with patch("src.kelly_sizer.KellyPositionSizer",
                   MagicMock(return_value=kelly)), \
             patch("src.fee_optimizer.FeeOptimizer",
                   MagicMock(return_value=fee)), \
             patch("src.state_db.get_state_db", lambda: MagicMock()):
            s = te._compute_kelly_sizing(
                client=None, symbol="BTCUSDT", usdt_bal=400.0, score=80,
                stop_loss_pct=3.5, tp_levels=[{"pct": 8.0}],
                active_positions=1, max_positions=4,
                surge_alert_level="SILENCE", core_layer=True,
            )
        assert s["invest_pct"] == pytest.approx(0.15, rel=0.02)  # 0.10 × 1.5


# ─────────────────────────────────────────────── state layer persistence ──

class TestStateLayer:
    def test_roundtrip_and_coalesce(self, db):
        db.portfolio_set("BTCUSDT", {"quantity": 0.002, "entry_price": 84000,
                                     "strategy": "trend", "layer": "core"})
        db.portfolio_set("ETHUSDT", {"quantity": 0.05, "entry_price": 2100,
                                     "strategy": "trend"})
        assert db.portfolio_get("BTCUSDT")["layer"] == "core"
        assert db.portfolio_get("ETHUSDT")["layer"] is None
        # a later layer-less write must NOT clobber the marker (COALESCE)
        db.portfolio_set("BTCUSDT", {"quantity": 0.004, "entry_price": 85000,
                                     "strategy": "trend"})
        assert db.portfolio_get("BTCUSDT")["layer"] == "core"
        # explicit layer update wins
        db.portfolio_set("BTCUSDT", {"quantity": 0.004, "entry_price": 85000,
                                     "strategy": "trend", "layer": "satellite"})
        assert db.portfolio_get("BTCUSDT")["layer"] == "satellite"

    def test_legacy_schema_migration(self, tmp_path):
        """A pre-WO-1046g database (no layer column) migrates in place."""
        legacy = tmp_path / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.execute(
            """CREATE TABLE portfolio (
                 symbol TEXT PRIMARY KEY, quantity REAL DEFAULT 0,
                 entry_price REAL DEFAULT 0, strategy TEXT DEFAULT '',
                 opened_at TEXT, updated_at TEXT,
                 stop_loss REAL, take_profit REAL,
                 invest_pct REAL DEFAULT 0)"""
        )
        conn.execute("INSERT INTO portfolio (symbol, quantity) VALUES ('BTCUSDT', 0.001)")
        conn.commit()
        conn.close()

        from src.state_db import StateDB

        sdb = StateDB(str(legacy))
        cols = {r[1] for r in sdb._get_conn().execute(
            "PRAGMA table_info(portfolio)").fetchall()}
        assert "layer" in cols
        row = sdb.portfolio_get("BTCUSDT")
        assert row is not None and row["quantity"] == 0.001  # data intact
        assert row["layer"] is None                            # NULL=satellite

    def test_load_state_from_db_carries_layer(self, db):
        from src.portfolio import PortfolioManager

        db.portfolio_set("BTCUSDT", {"quantity": 0.002, "entry_price": 84000,
                                     "strategy": "trend", "layer": "core"})
        pm = PortfolioManager(binance_client=None)
        assert pm.positions["BTCUSDT"]["layer"] == "core"


# ────────────────────────────────────────────────────── exit core semantics

def _pos(symbol="BTCUSDT", layer=None, opened_hours_ago=0, entry=100.0):
    import datetime as _dt

    opened = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(hours=opened_hours_ago)).isoformat()
    return {"symbol": symbol, "quantity": 1.0, "entry_price": entry,
            "opened_at": opened, "layer": layer}


class TestExitCoreSemantics:
    def _db(self):
        from src.state_db import get_state_db

        return get_state_db()

    def test_satellite_zombie_fires_core_skips(self):
        from src.exit_check import evaluate_one

        common = dict(band_position=None, include_momentum=False,
                      tp_pct=8.0, sl_pct=6.0, tp_partial_r=0.0,
                      zombie_pnl_pct=0.5, zombie_band_pos_max=0.5,
                      hold_hours=48.0)
        # satellite: shell feeds zombie_hours=24 → fires at 30h held
        # (price 100.4 → pnl +0.4% < zombie_pnl_pct 0.5%, strictly less)
        sat = evaluate_one(_pos("SOLUSDT"), 100.4, 30.0,
                           zombie_hours=24.0, **common)
        assert sat is not None and sat["kind"] == "zombie_exit"
        # core: shell feeds zombie_hours=0 (WO-1046g) → strategic hold
        core = evaluate_one(_pos("BTCUSDT", layer="core"), 100.4, 30.0,
                            zombie_hours=0.0, **common)
        assert core is None  # zombie disabled for core (strategic hold)

    def test_core_hold_168(self):
        from src.exit_check import evaluate_one

        common = dict(band_position=None, include_momentum=False,
                      tp_pct=8.0, sl_pct=6.0, tp_partial_r=0.0,
                      zombie_hours=0.0, zombie_pnl_pct=0.5,
                      zombie_band_pos_max=0.5)
        # satellite cap 48h expires at 50h; core cap 168h does not
        sat = evaluate_one(_pos("SOLUSDT"), 100.5, 50.0,
                           hold_hours=48.0, **common)
        assert sat is not None and sat["kind"] == "hold_expiry"
        core = evaluate_one(_pos("BTCUSDT", layer="core"), 100.5, 50.0,
                            hold_hours=168.0, **common)
        assert core is None
        # core expires at 168h
        core2 = evaluate_one(_pos("BTCUSDT", layer="core"), 100.5, 170.0,
                             hold_hours=168.0, **common)
        assert core2 is not None and core2["kind"] == "hold_expiry"

    def test_evaluate_exits_reads_layer_from_db(self):
        """Integration: the live shell feeds core hold/zombie overrides."""
        import datetime as _dt

        from src.exit_check import evaluate_exits

        db = self._db()
        opened = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(hours=50)).isoformat()
        db.portfolio_set("BTCUSDT", {"quantity": 0.001, "entry_price": 100.0,
                                     "strategy": "trend", "layer": "core",
                                     "opened_at": opened})
        db.portfolio_set("SOLUSDT", {"quantity": 1.0, "entry_price": 100.0,
                                     "strategy": "trend",
                                     "opened_at": opened})
        decs = evaluate_exits(None, db, prices={"BTCUSDT": 100.3, "SOLUSDT": 100.3},
                             include_momentum=False)
        kinds = {d["symbol"]: d["kind"] for d in decs}
        assert kinds.get("SOLUSDT") == "zombie_exit"   # satellite 50h flat
        assert "BTCUSDT" not in kinds                   # core: no zombie, no 48h hold

    def test_scan_exit_triggers_mirrors_core_hold(self):
        """Fast path must not fire a 48h hold_expiry on a core position."""
        import datetime as _dt

        from src.exit_check import scan_exit_triggers

        db = self._db()
        opened = (_dt.datetime.now(_dt.timezone.utc)
                  - _dt.timedelta(hours=50)).isoformat()
        db.portfolio_set("BTCUSDT", {"quantity": 0.001, "entry_price": 100.0,
                                     "strategy": "trend", "layer": "core",
                                     "opened_at": opened})
        db.portfolio_set("SOLUSDT", {"quantity": 1.0, "entry_price": 100.0,
                                     "strategy": "trend",
                                     "opened_at": opened})
        trig = scan_exit_triggers(db, ["BTCUSDT", "SOLUSDT"],
                                  {"BTCUSDT": 100.3, "SOLUSDT": 100.3})
        assert trig is not None
        assert "SOLUSDT" in trig and "BTCUSDT" not in trig


# ──────────────────────────────────────────────────────── source pins ─────

def test_source_pins():
    """Wiring pins: execute_phases threads core_layer; record writes layer."""
    import pathlib

    repo = pathlib.Path(__file__).parent.parent
    ep = (repo / "src" / "execute_phases.py").read_text()
    assert 'core_layer=ctx.get("core_layer", False)' in ep
    te = (repo / "src" / "trade_executor.py").read_text()
    assert 'layer="core" if core_layer else None' in te
    ex = (repo / "src" / "exit_check.py").read_text()
    assert "core_max_hold_hours" in ex
    assert "core_zombie_exit" in ex
