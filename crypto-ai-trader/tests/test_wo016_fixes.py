"""WO-0921-016 fixes: corr gate de-risk exemption, BULL threshold
layering (read + detector wiring), bollinger ATR-anchored SL.

1. CorrelationRiskManager._pairwise_threshold reads bull_regime_state kv:
   BULL regimes (CONFIRMED_BULL / MILD_BULL) relax 0.70 -> 0.85
   (8/26 red-line spec). Fail-closed to 0.70.
2. is_de_risk_switch: BTC/ETH whitelist + clearly-lower-beta detection —
   the switch corr gate must not block risk-reduction moves
   (9/21 ENA->ETH @ corr 0.711 was wrongly blocked).
3. BullRegimeDetector.update_from_market: evaluates the latest BTC 4H
   bar, idempotent per bar, never raises.
4. _bollinger_atr_sl: SL% = ATR14(1h)/price × mult, clamp [4, 15].
"""

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    import src.binance_client  # noqa: F401
except Exception:
    sys.modules["src.binance_client"] = types.SimpleNamespace(
        BinanceClient=object)

from src.correlation_risk import (  # noqa: E402
    BULL_PAIRWISE_CORR,
    MAX_PAIRWISE_CORR,
    CorrelationRiskManager,
)


def _mk_corr():
    return CorrelationRiskManager(SimpleNamespace())


def _set_regime(monkeypatch, regime):
    """Patch state_db.get_state_db with a kv holding the regime."""
    st = json.dumps({"regime": regime}) if regime else None
    fake_db = SimpleNamespace(
        kv_get=lambda key, _st=st: _st,
        kv_set=lambda key, val: None,
        audit_log=lambda *a, **k: None)
    monkeypatch.setattr(
        "src.state_db.get_state_db", lambda: fake_db)
    return fake_db


# ── WO-016-2: pairwise threshold layering ────────────────────────────

class TestPairwiseThreshold:
    def test_default_without_state(self, monkeypatch):
        _set_regime(monkeypatch, None)
        assert _mk_corr()._pairwise_threshold() == MAX_PAIRWISE_CORR

    def test_confirmed_bull_relaxes(self, monkeypatch):
        _set_regime(monkeypatch, "CONFIRMED_BULL")
        assert _mk_corr()._pairwise_threshold() == BULL_PAIRWISE_CORR

    def test_mild_bull_relaxes(self, monkeypatch):
        _set_regime(monkeypatch, "MILD_BULL")
        assert _mk_corr()._pairwise_threshold() == BULL_PAIRWISE_CORR

    def test_neutral_stays_tight(self, monkeypatch):
        _set_regime(monkeypatch, "NEUTRAL")
        assert _mk_corr()._pairwise_threshold() == MAX_PAIRWISE_CORR

    def test_bad_json_fails_closed(self, monkeypatch):
        fake_db = SimpleNamespace(
            kv_get=lambda key: "{corrupt",
            kv_set=lambda k, v: None)
        monkeypatch.setattr(
            "src.state_db.get_state_db", lambda: fake_db)
        assert _mk_corr()._pairwise_threshold() == MAX_PAIRWISE_CORR


class TestThresholdAppliedToCheck:
    """corr 0.75 must block at NEUTRAL but pass under BULL."""

    def _patch_matrix(self, monkeypatch, m):
        matrix = {"SOL": {"BTC": 0.75}, "BTC": {"SOL": 0.75}}
        monkeypatch.setattr(
            m, "_build_correlation_matrix",
            lambda symbols: (matrix, {}))

    def test_075_blocks_neutral(self, monkeypatch):
        _set_regime(monkeypatch, "NEUTRAL")
        m = _mk_corr()
        self._patch_matrix(monkeypatch, m)
        r = m.check_new_position("SOL", ["BTC"])
        assert r["allowed"] is False
        assert "Limit=0.7" in r["reason"]

    def test_075_passes_bull(self, monkeypatch):
        _set_regime(monkeypatch, "CONFIRMED_BULL")
        m = _mk_corr()
        self._patch_matrix(monkeypatch, m)
        r = m.check_new_position("SOL", ["BTC"])
        assert r["allowed"] is True


# ── WO-016-1: de-risk switch detection ──────────────────────────────

def _hist_from_returns(returns):
    prices = [100.0]
    for r in returns:
        prices.append(prices[-1] * (1.0 + r))
    return prices


def _alt_returns(scale, n=44):
    return [0.01 * scale if i % 2 == 0 else -0.01 * scale
            for i in range(n)]


class TestDeRiskSwitch:
    def test_whitelist_eth_from_alt(self):
        ok, why = _mk_corr().is_de_risk_switch("ETH", "ENA")
        assert ok and "low-beta" in why

    def test_whitelist_btc_from_alt(self):
        ok, _ = _mk_corr().is_de_risk_switch("BTC", "FET")
        assert ok

    def test_lowbeta_source_not_whitelisted(self, monkeypatch):
        m = _mk_corr()
        monkeypatch.setattr(
            m, "_get_price_history",
            lambda sym, days=45: {
                "BTC": _hist_from_returns(_alt_returns(1.0)),
                "ETH": _hist_from_returns(_alt_returns(1.1)),
            }.get(sym, []))
        ok, _ = m.is_de_risk_switch("ETH", "BTC")
        assert not ok

    def test_beta_clear_reduction_exempt(self, monkeypatch):
        m = _mk_corr()
        monkeypatch.setattr(
            m, "_get_price_history",
            lambda sym, days=45: {
                "BTC": _hist_from_returns(_alt_returns(1.0)),
                "TGT": _hist_from_returns(_alt_returns(0.5)),
                "SRC": _hist_from_returns(_alt_returns(1.5)),
            }.get(sym, []))
        ok, why = m.is_de_risk_switch("TGT", "SRC")
        assert ok and "beta" in why

    def test_beta_similar_not_exempt(self, monkeypatch):
        m = _mk_corr()
        monkeypatch.setattr(
            m, "_get_price_history",
            lambda sym, days=45: {
                "BTC": _hist_from_returns(_alt_returns(1.0)),
                "AAA": _hist_from_returns(_alt_returns(1.0)),
                "BBB": _hist_from_returns(_alt_returns(1.02)),
            }.get(sym, []))
        ok, _ = m.is_de_risk_switch("AAA", "BBB")
        assert not ok

    def test_beta_unknown_not_exempt(self, monkeypatch):
        m = _mk_corr()
        monkeypatch.setattr(
            m, "_get_price_history", lambda sym, days=45: [])
        ok, _ = m.is_de_risk_switch("XYZ", "ABC")
        assert not ok, "unknown beta must fail closed (no exemption)"


# ── WO-016-1: gate wiring in position_optimizer ──────────────────────

from src.position_optimizer import PositionOptimizer  # noqa: E402


def _mk_opt(risk_manager, sell_called):
    opt = object.__new__(PositionOptimizer)
    opt.bc = SimpleNamespace(
        get_symbol_filters=lambda s: {
            "minQty": 0.0, "minNotional": 0.0, "stepSize": 1.0},
        get_ticker_price=lambda symbol=0, **k: 100.0,
        cancel_all_orders=lambda s: [],
        get_account=lambda: {"balances": []},
        place_market_sell=lambda symbol, quantity: (
            sell_called.append(symbol) or {"orderId": 9,
                                           "status": "FILLED"}),
    )
    opt.risk_manager = risk_manager
    return opt


DECISION = {
    "from_symbol": "BCHUSDT", "to_symbol": "SUIUSDT", "from_value": 50.0,
}


def _portfolio():
    p = SimpleNamespace()
    p.get_all_positions = lambda: [
        {"symbol": "BCHUSDT", "quantity": 0.05, "entry_price": 500.0},
        {"symbol": "NEARUSDT", "quantity": 2.0, "entry_price": 3.0},
    ]
    return p


class TestGateDeRiskExemption:
    def _audit_patch(self, monkeypatch, audited):
        fake_db = SimpleNamespace(
            audit_log=lambda action, details, source="":
                audited.append(action))
        monkeypatch.setitem(
            sys.modules, "src.state_db",
            SimpleNamespace(get_state_db=lambda: fake_db))

    def test_blocked_but_derisk_proceeds(self, monkeypatch):
        audited = []
        self._audit_patch(monkeypatch, audited)
        sell_calls = []
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": False, "reason": "corr 0.711",
                    "max_correlation": 0.711},
                is_de_risk_switch=lambda t, f: (
                    True, "low-beta major ETH"))), sell_calls)
        opt.portfolio = _portfolio()
        opt._last_switch_time = {}
        opt._save_switch_times = lambda: None
        opt._execute_switch(dict(DECISION))
        assert sell_calls == ["BCHUSDT"], "de-risk switch must proceed"
        assert "SWITCH_RISK_DERISK_PASS" in audited

    def test_blocked_not_derisk_still_blocks(self, monkeypatch):
        audited = []
        self._audit_patch(monkeypatch, audited)
        sell_calls = []
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": False, "reason": "corr 0.81",
                    "max_correlation": 0.81},
                is_de_risk_switch=lambda t, f: (False, ""))),
            sell_calls)
        opt.portfolio = _portfolio()
        assert opt._execute_switch(dict(DECISION)) is False
        assert sell_calls == []
        assert audited == ["SWITCH_RISK_BLOCK"]

    def test_legacy_corr_stub_without_derisk_fn_still_blocks(self):
        """Old mocks / older corr modules lack is_de_risk_switch — the
        gate must fall back to the plain reject path (no crash)."""
        sell_calls = []
        opt = _mk_opt(SimpleNamespace(
            correlation_risk=SimpleNamespace(
                check_new_position=lambda new, held: {
                    "allowed": False, "reason": "corr 0.9",
                    "max_correlation": 0.9})), sell_calls)
        opt.portfolio = _portfolio()
        assert opt._execute_switch(dict(DECISION)) is False
        assert sell_calls == []


# ── WO-016-2: detector wiring ────────────────────────────────────────

class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return []

    def commit(self):
        pass


class _FakeDB:
    def __init__(self):
        self.kv = {}

    def _get_conn(self):
        return _FakeConn()

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, val):
        self.kv[key] = val

    def audit_log(self, *a, **k):
        pass


class _FakeBTCClient:
    """BTC klines: strong uptrend so close > SMA200*1.05."""

    def __init__(self, daily_close=60000.0, bars=210):
        self.daily_close = daily_close
        self.bars = bars

    def get_klines(self, symbol, interval, limit=0):
        assert symbol == "BTCUSDT"
        if interval == "1d":
            n = min(limit, self.bars)
            k = []
            start = self.daily_close * 0.5
            price = start
            ratio = (self.daily_close / start) ** (1 / n)
            for i in range(n):
                price *= ratio
                k.append({
                    "open_time": 1_700_000_000_000 + i * 86_400_000,
                    "open": price, "high": price * 1.001,
                    "low": price * 0.999, "close": price,
                    "volume": 100.0,
                    "close_time": 1_700_000_000_000 + (i + 1) * 86_400_000,
                })
            return k
        if interval == "4h":
            k = []
            for i in range(30):
                ts = 1_790_000_000_000 + i * 4 * 3_600_000
                px = self.daily_close * (1 + 0.001 * i)
                k.append({
                    "open_time": ts, "open": px, "high": px * 1.002,
                    "low": px * 0.998, "close": px, "volume": 100.0,
                    "close_time": ts + 4 * 3_600_000,
                })
            return k
        return []


class _ShiftedBarClient:
    """Shifts every 4H bar ts forward by `bars` positions (a new bar)."""

    def __init__(self, inner, hours):
        self.inner = inner
        self.hours = hours

    def get_klines(self, symbol, interval, limit=0):
        k = self.inner.get_klines(symbol, interval, limit)
        if interval == "4h":
            shift = self.hours * 3_600_000
            return [dict(bar, open_time=bar["open_time"] + shift,
                         close_time=bar["close_time"] + shift)
                    for bar in k]
        return k


class TestUpdateFromMarket:
    def _mk_detector(self):
        from src.bull_regime import BullRegimeDetector
        return BullRegimeDetector(db=_FakeDB(), client=None)

    def _patch_fng(self, monkeypatch, det, value=70):
        base_day = 1_790_000_000 // 86400
        hist = {}
        for d in range(base_day - 8, base_day + 2):
            hist[d * 86400] = value
        monkeypatch.setattr(det, "_load_fng_history", lambda: hist)

    def _patch_adx(self, monkeypatch, value=30.0):
        monkeypatch.setattr(
            "src.indicators.Indicators.adx",
            staticmethod(lambda klines, period=14: value))

    def test_bull_confirm_after_two_bars(self, monkeypatch):
        det = self._mk_detector()
        self._patch_fng(monkeypatch, det)
        self._patch_adx(monkeypatch)
        st1 = det.update_from_market(_FakeBTCClient())
        assert st1.regime != "CONFIRMED_BULL"  # first bar: count=1
        st2 = det.update_from_market(_ShiftedBarClient(
            _FakeBTCClient(), hours=8))
        assert st2.regime == "CONFIRMED_BULL"
        assert det.db.kv.get("bull_regime_state") is not None

    def test_same_bar_idempotent(self, monkeypatch):
        det = self._mk_detector()
        self._patch_fng(monkeypatch, det)
        self._patch_adx(monkeypatch)
        client = _FakeBTCClient()
        st1 = det.update_from_market(client)
        count1 = st1.confirm_count
        st2 = det.update_from_market(client)
        assert st2.confirm_count == count1
        assert st1.last_4h_ts == st2.last_4h_ts

    def test_insufficient_daily_keeps_state(self, monkeypatch):
        det = self._mk_detector()
        self._patch_fng(monkeypatch, det)

        class _Short(_FakeBTCClient):
            def get_klines(self, symbol, interval, limit=0):
                if interval == "1d":
                    return super().get_klines(symbol, interval, 100)
                return super().get_klines(symbol, interval, limit)

        st = det.update_from_market(_Short())
        assert st.regime == "NEUTRAL"

    def test_never_raises_on_client_error(self):
        det = self._mk_detector()

        class _Boom:
            def get_klines(self, *a, **k):
                raise RuntimeError("api down")

        st = det.update_from_market(_Boom())
        assert st.regime == "NEUTRAL"


# ── WO-016-3: bollinger ATR SL ──────────────────────────────────────

from src.research_phase import _bollinger_atr_sl  # noqa: E402


def _kl(range_pct, n=40, base=100.0):
    k = []
    for i in range(n):
        px = base
        hi = px * (1 + range_pct / 2 / 100)
        lo = px * (1 - range_pct / 2 / 100)
        k.append({"open": px, "high": hi, "low": lo, "close": px,
                  "volume": 1.0})
    return k


class TestBollingerAtrSl:
    # expectations track risk_params.yaml bollinger.atr_sl_mult (2.5
    # per the 90d backtest grid; update together with the config)

    def test_floor_clamp(self):
        assert _bollinger_atr_sl(_kl(1.0), 100.0) == 4.0

    def test_mid_range(self):
        # ATR 5% × 2.5 = 12.5 (within [4, 15])
        assert _bollinger_atr_sl(_kl(5.0), 100.0) == 12.5

    def test_cap_clamp(self):
        assert _bollinger_atr_sl(_kl(10.0), 100.0) == 15.0

    def test_empty_klines_none(self):
        assert _bollinger_atr_sl([], 100.0) is None

    def test_zero_price_none(self):
        assert _bollinger_atr_sl(_kl(5.0), 0.0) is None

    def test_short_history_none(self):
        assert _bollinger_atr_sl(_kl(5.0, n=10), 100.0) is None
