"""WO-1042 (10/11): protective-SELL qty calibration + validator SWITCH fix.

Incident replay (10/11 08:02, real production ATOM→OGN):
  176 OGN bought market — SPOT BUY taker fee settles in BASE, so only
  175.824 landed. Every protective path sized its SELL from the nominal
  DB qty 176 → OCO -2010, full-SL -2010, guardian emergency-SL -2010;
  the position sat naked until Travis manually re-placed at 175.

Bug-①: all "place a protective SELL after a fill" paths must size from
  what the account actually holds:
    L1 calibrate_sell_qty      — cap at exchange free (floor stepSize)
    L2 place_sell_with_downgrade — on a -2010-shaped rejection retry ONCE
  plus: book the NET qty (not nominal) and write back the BUY orderId.

Bug-②: report_validator blocked SWITCH notifications 5× because the
  balance-claim regex swallowed "OGNUSDT @ $0.0340" prices. Two layers:
  verb list += SWITCH; USDT anchor must be a standalone token.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.protect_qty import (
    base_asset, calibrate_sell_qty, floor_step, net_buy_qty,
    place_sell_with_downgrade,
)

OGN_BUY_ORDER = {          # real 10/11 08:02 fill shape (gross 176)
    "orderId": 1283838739,
    "executedQty": "176",
    "fills": [{"commission": "0.176", "commissionAsset": "OGN",
               "price": "0.03402", "qty": "176"}],
}


# ── A. unit: pure helpers ─────────────────────────────────────────────

class TestBaseAsset:
    def test_common_pairs(self):
        assert base_asset("OGNUSDT") == "OGN"
        assert base_asset("BTCUSDC") == "BTC"
        assert base_asset("ETHFDUSD") == "ETH"

    def test_unknown_returns_as_is(self):
        assert base_asset("OGNBTC") == "OGNBTC"


class TestFloorStep:
    def test_incident_values(self):
        # Travis' manual re-placement used 175 — same answer here
        assert floor_step(175.824, 1.0) == 175.0
        assert floor_step(176.0, 1.0) == 176.0

    def test_float_rep_noise_snaps_up(self):
        # a hair BELOW an exact multiple is float representation noise
        # (qty*rate arithmetic) — epsilon snaps it up to the multiple
        assert floor_step(2.99999999999, 1.0) == 3.0
        assert floor_step(175.999999999999, 1.0) == 176.0

    def test_real_remainder_stays_down(self):
        # 175.824 is a genuine remainder, not noise — must floor to 175
        assert floor_step(175.824, 1.0) == 175.0

    def test_zero_step_identity(self):
        assert floor_step(175.824, 0.0) == 175.824


class TestNetBuyQty:
    def test_incident_fill(self):
        assert net_buy_qty("OGNUSDT", OGN_BUY_ORDER, 176.0) == \
            pytest.approx(175.824)

    def test_commission_in_quote_is_gross(self):
        order = {"executedQty": "100",
                 "fills": [{"commission": "0.034", "commissionAsset": "USDT"}]}
        assert net_buy_qty("OGNUSDT", order, 100.0) == pytest.approx(100.0)

    def test_no_fill_detail_falls_back(self):
        assert net_buy_qty("OGNUSDT", {"executedQty": "176"}, 99.0) == \
            pytest.approx(176.0)
        assert net_buy_qty("OGNUSDT", None, 98.0) == pytest.approx(98.0)


# ── B. unit: L1 / L2 ──────────────────────────────────────────────────

class FakeBalClient:
    """client.get_free_balance(asset) with scriptable answers."""

    def __init__(self, free=None, raise_on=()):
        self.free = dict(free or {})
        self.raise_on = set(raise_on)
        self.queries = []

    def get_free_balance(self, asset):
        self.queries.append(asset)
        if asset in self.raise_on:
            raise ConnectionError("balance api down")
        return self.free.get(asset, 0.0)


class TestCalibrateSellQty:
    def test_free_cap_incident(self):
        c = FakeBalClient({"OGN": 175.824})
        qty, trace = calibrate_sell_qty(
            c, "OGNUSDT", 176.0, 1.0, context="switch_protection")
        assert qty == 175.0
        assert trace["source"] == "free_balance"

    def test_free_covers_request(self):
        c = FakeBalClient({"OGN": 200.0})
        qty, trace = calibrate_sell_qty(c, "OGNUSDT", 176.0, 1.0)
        assert qty == 176.0
        assert trace["source"] == "free_balance_ok"

    def test_balance_api_down_uses_fill_net(self):
        c = FakeBalClient(raise_on={"OGN"})
        qty, trace = calibrate_sell_qty(
            c, "OGNUSDT", 176.0, 1.0,
            fill_order=OGN_BUY_ORDER, context="switch_protection")
        assert qty == 175.0
        assert trace["source"] == "fill_net"

    def test_never_exceeds_request(self):
        c = FakeBalClient({"OGN": 9999.0})
        qty, _ = calibrate_sell_qty(c, "OGNUSDT", 176.0, 1.0)
        assert qty == 176.0

    def test_no_client_balance_method_keeps_request(self):
        qty, trace = calibrate_sell_qty(
            object(), "OGNUSDT", 176.0, 1.0, fill_order=None)
        assert qty == 176.0
        assert trace["source"] == "requested"

    def test_calibrated_row_in_audit_log(self):
        from src.state_db import get_state_db
        c = FakeBalClient({"OGN": 175.824})
        calibrate_sell_qty(c, "OGNUSDT", 176.0, 1.0, context="t_audit")
        rows = get_state_db()._get_conn().execute(
            "SELECT action, details FROM audit_log "
            "WHERE action='PROTECT_QTY_CALIBRATED'").fetchall()
        assert rows, "calibration must leave an audit row"
        det = json.loads(rows[-1]["details"])
        assert det["context"] == "t_audit"
        assert det["requested"] == pytest.approx(176.0)
        assert det["calibrated_qty"] == pytest.approx(175.0)


class TestPlaceSellWithDowngrade:
    def test_first_try_success_no_retry(self):
        c = FakeBalClient({"OGN": 175.824})
        calls = []

        def place(q):
            calls.append(q)
            return {"orderListId": 1}

        res, used = place_sell_with_downgrade(
            place, c, "OGNUSDT", 175.0, 1.0, kind="switch_oco")
        assert res == {"orderListId": 1} and used == 175.0
        assert calls == [175.0]

    def test_rejection_downgrades_once(self):
        # -2010-shaped: request 176, exchange holds 175.824 → retry @175
        c = FakeBalClient({"OGN": 175.824})
        calls = []

        def place(q):
            calls.append(q)
            if q > 175.824:
                return None          # business error → None contract
            return {"orderListId": 25259247975}

        res, used = place_sell_with_downgrade(
            place, c, "OGNUSDT", 176.0, 1.0,
            kind="switch_oco", price=0.034, min_notional=5.0)
        # 175 × 0.034 = 5.95 ≥ OGNUSDT minNotional 5 — retry goes out
        assert res == {"orderListId": 25259247975}
        assert used == 175.0
        assert calls == [176.0, 175.0]

    def test_free_zero_no_retry(self):
        c = FakeBalClient({"OGN": 0.0})
        calls = []

        def place(q):
            calls.append(q)
            return None

        res, used = place_sell_with_downgrade(
            place, c, "OGNUSDT", 176.0, 1.0, kind="switch_oco")
        assert res is None and used == 176.0
        assert calls == [176.0]      # nothing to sell — no blind retry

    def test_free_covers_qty_no_retry(self):
        # rejection NOT -2010-shaped (bad params etc.) → as-is
        c = FakeBalClient({"OGN": 500.0})
        calls = []

        def place(q):
            calls.append(q)
            return None

        res, _ = place_sell_with_downgrade(
            place, c, "OGNUSDT", 176.0, 1.0, kind="switch_oco")
        assert res is None
        assert calls == [176.0]

    def test_min_qty_gate_blocks_retry(self):
        c = FakeBalClient({"OGN": 0.4})
        res, used = place_sell_with_downgrade(
            lambda q: None, c, "OGNUSDT", 176.0, 0.1,
            kind="switch_oco", min_qty=1.0)
        assert res is None and used == 176.0

    def test_min_notional_gate_blocks_retry(self):
        c = FakeBalClient({"OGN": 175.0})
        res, used = place_sell_with_downgrade(
            lambda q: None, c, "OGNUSDT", 176.0, 1.0,
            kind="switch_oco", price=0.034, min_notional=10.0)
        # 175 * 0.034 = 5.95 < 10 → downgrade would be pointless
        assert res is None and used == 176.0

    def test_place_raising_is_caught(self):
        c = FakeBalClient({"OGN": 175.824})

        def place(q):
            if q > 175.824:
                raise RuntimeError("-2010")
            return {"orderId": 1}

        res, used = place_sell_with_downgrade(
            place, c, "OGNUSDT", 176.0, 1.0, kind="switch_oco")
        assert res == {"orderId": 1} and used == 175.0

    def test_downgrade_row_in_audit_log(self):
        from src.state_db import get_state_db
        c = FakeBalClient({"OGN": 175.824})

        def place(q):
            return None if q > 175.824 else {"orderListId": 2}

        place_sell_with_downgrade(place, c, "OGNUSDT", 176.0, 1.0,
                                  kind="t_l2", context="t_ctx")
        rows = get_state_db()._get_conn().execute(
            "SELECT action, details FROM audit_log "
            "WHERE action='PROTECT_QTY_DOWNGRADE'").fetchall()
        assert rows, "downgrade must leave an audit row (留痕)"
        det = json.loads(rows[-1]["details"])
        assert det["kind"] == "t_l2"
        assert det["downgraded_qty"] == pytest.approx(175.0)


# ── C. integration: switch protections (the incident path) ───────────

class SwitchFakeClient:
    """Binance client stub reproducing the 10/11 OGN exchange state."""

    def __init__(self, free, reject_above, filters=None):
        self.free = free                  # {"OGN": 175.824}
        self.reject_above = reject_above  # qty > this → None (-2010)
        self.filters = filters or {
            "stepSize": "1.0", "tickSize": "0.0001",
            "minQty": "1.0", "minNotional": "5"}
        self.oco_calls = []
        self.sl_calls = []
        self.tp_calls = []

    def get_symbol_filters(self, sym):
        return self.filters

    def get_free_balance(self, asset):
        v = self.free.get(asset)
        if v is None:
            raise ConnectionError("balance blip")
        return v

    def place_oco(self, sym, qty, tp_px, sl_px):
        if qty > self.reject_above:
            return None                    # -2010 contract: None
        self.oco_calls.append((sym, qty, tp_px, sl_px))
        return {"orderListId": 25259247975}

    def place_stop_loss_limit(self, sym, qty, lim, stop):
        if qty > self.reject_above:
            return None
        self.sl_calls.append((sym, qty, lim, stop))
        return {"orderId": 902}

    def place_limit_sell(self, sym, qty, px):
        if qty > self.reject_above:
            return None
        self.tp_calls.append((sym, qty, px))
        return {"orderId": 903}


@pytest.fixture
def quiet_optimizer_alerts(monkeypatch):
    import src.position_optimizer as po
    fired = []
    monkeypatch.setattr(po, "emit_alert",
                        lambda kind, sym, payload=None, **k: fired.append(
                            (kind, sym, payload)))
    return fired


def _make_optimizer(bc):
    import src.position_optimizer as po
    opt = object.__new__(po.PositionOptimizer)
    opt.bc = bc
    return opt


class TestSwitchProtections:
    def test_l1_sizes_oco_from_free_balance(self, quiet_optimizer_alerts):
        """176 nominal / 175.824 landed → OCO placed ONCE for 175."""
        bc = SwitchFakeClient({"OGN": 175.824}, reject_above=175.9)
        opt = _make_optimizer(bc)
        opt._place_switch_protections(
            "OGNUSDT", 176, OGN_BUY_ORDER, 0.03402)
        assert len(bc.oco_calls) == 1
        sym, qty, tp_px, sl_px = bc.oco_calls[0]
        assert sym == "OGNUSDT" and qty == 175.0     # Travis' manual qty
        assert not any(k == "PROTECTION_FAILED"
                       for k, _, _ in quiet_optimizer_alerts)
        assert not bc.sl_calls

    def test_l2_downgrade_when_balance_blips_at_l1(
            self, quiet_optimizer_alerts):
        """L1 can't read the balance → keeps 176; exchange rejects →
        L2 re-queries (up now), downgrades to 175, OCO goes live."""
        bc = SwitchFakeClient({"OGN": 175.824}, reject_above=175.9)
        # L1: first balance read fails; L2 retry: read succeeds
        state = {"n": 0}
        real = bc.get_free_balance

        def flaky(asset):
            if asset == "OGN" and state["n"] == 0:
                state["n"] += 1
                raise ConnectionError("transient")
            return real(asset)
        bc.get_free_balance = flaky

        opt = _make_optimizer(bc)
        opt._place_switch_protections(
            "OGNUSDT", 176, OGN_BUY_ORDER, 0.03402)
        assert len(bc.oco_calls) == 1
        assert bc.oco_calls[0][1] == 175.0
        assert not any(k == "PROTECTION_FAILED"
                       for k, _, _ in quiet_optimizer_alerts)

    def test_fee_in_quote_keeps_gross(self, quiet_optimizer_alerts):
        """BNB-discounted buy (fee NOT in base) → full 176 is placeable."""
        order = {"orderId": 7, "executedQty": "176",
                 "fills": [{"commission": "0.0006",
                            "commissionAsset": "BNB",
                            "price": "0.03402", "qty": "176"}]}
        bc = SwitchFakeClient({"OGN": 176.0}, reject_above=176.1)
        opt = _make_optimizer(bc)
        opt._place_switch_protections("OGNUSDT", 176, order, 0.03402)
        assert bc.oco_calls[0][1] == 176.0


# ── D. booking: net qty + BUY orderId writeback ──────────────────────

class TestBookingWriteback:
    def test_add_position_records_client_order_id(self):
        from src.portfolio import PortfolioManager
        from src.state_db import get_state_db
        pm = PortfolioManager()
        pm.add_position("OGNUSDT", 175.824, 0.03402, strategy="switch",
                        deduct_cash=False, _skip_validation=True,
                        client_order_id="1283838739")
        row = get_state_db()._get_conn().execute(
            "SELECT client_order_id, qty FROM trades "
            "WHERE client_order_id='1283838739'").fetchone()
        assert row is not None, "BUY orderId must land in trades"
        assert row["qty"] == pytest.approx(175.824)

    def test_record_trade_portfolio_forwards_order_id(self, monkeypatch):
        """trade_executor._record_trade_portfolio passes buy orderId."""
        import src.trade_executor as te
        captured = {}

        class _FakePM:
            def __init__(self):
                self.positions = {}

            def update_balance(self, *a, **k):
                pass

            def add_position(self, **kw):
                captured.update(kw)

        monkeypatch.setattr(te, "PortfolioManager", _FakePM)

        class _C:  # balance fetch fails → fallback branch still forwards oid
            def get_free_balance(self, a):
                raise ConnectionError("x")

        te._record_trade_portfolio(
            _C(), "OGNUSDT", 175.824, 0.03402, "bandit_entry",
            usdt_bal=400.0, invest_amount=6.0, fee_rate=0.001,
            invest_pct=0.015, bandit_context=None, bandit_multiplier=1.0,
            client_order_id="1283838739")
        assert captured["client_order_id"] == "1283838739"
        assert captured["quantity"] == pytest.approx(175.824)


# ── E. guardian: emergency paths calibrate too ───────────────────────

class GuardianFakeClient:
    def __init__(self, free, filters=None):
        self.free = free
        self.filters = filters or {
            "stepSize": "0.001", "tickSize": "0.0001",
            "minQty": "0.001", "minNotional": "5"}
        self.sl_calls = []
        self.tp_calls = []

    def get_open_orders(self, sym):
        return []

    def get_symbol_filters(self, sym):
        return self.filters

    def get_free_balance(self, asset):
        return self.free.get(asset, 0.0)

    def place_stop_loss_limit(self, sym, qty, lim, stop):
        if qty > self.free.get(sym.replace("USDT", ""), 0.0):
            return None                       # -2010
        self.sl_calls.append((sym, qty, lim, stop))
        return {"orderId": 902}

    def place_limit_sell(self, sym, qty, px):
        if qty > self.free.get(sym.replace("USDT", ""), 0.0):
            return None
        self.tp_calls.append((sym, qty, px))
        return {"orderId": 903}


class FakePortfolio:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


@pytest.fixture
def quiet_guardian(monkeypatch):
    import src.protection_guardian as pg
    monkeypatch.setattr(pg, "emit_alert", lambda *a, **k: None)


class TestGuardianCalibration:
    def test_free_slice_tp_capped_at_exchange_free(self, quiet_guardian):
        """DB says 100, exchange holds 99.9 → guardian TP sells 99.9."""
        import src.protection_guardian as pg
        c = GuardianFakeClient({"NEAR": 99.9})
        pos = {"symbol": "NEARUSDT", "quantity": 100.0,
               "entry_price": 5.0, "current_price": 5.0,
               "take_profit": 5.5}
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert c.tp_calls, "TP must be placed (calibrated, not rejected)"
        assert c.tp_calls[0][1] == pytest.approx(99.9)

    def test_naked_tp_breach_emergency_sl_calibrated(self, quiet_guardian):
        """TP already breached + naked → emergency SL sized from free."""
        import src.protection_guardian as pg
        c = GuardianFakeClient({"WLD": 12.6})
        pos = {"symbol": "WLDUSDT", "quantity": 12.7,
               "entry_price": 1.0, "current_price": 1.4,   # TP(1.04) gone
               "take_profit": 1.04}
        res = pg.run(c, FakePortfolio([pos]))
        assert res["healed"] == 1
        assert c.sl_calls
        assert c.sl_calls[0][1] == pytest.approx(12.6)


# ── F. paper_trader: BUY fee settles in base ─────────────────────────

class TestPaperBuyFeeAlignment:
    @pytest.fixture
    def pt(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("TESTING", "1")
        from src.paper_trader import PaperTrader, PAPER_FEE_RATE
        monkeypatch.setattr(PaperTrader, "get_current_price",
                            lambda self, s: 0.34)
        monkeypatch.setattr(PaperTrader, "get_klines",
                            lambda self, s, i="1h", limit=500, **kw: [])
        trader = PaperTrader()
        trader._set_sim_balance(400.0)
        trader._FEE_RATE = PAPER_FEE_RATE
        return trader

    def test_buy_fee_lands_in_base(self, pt):
        from src.paper_trader import PAPER_FEE_RATE
        r = pt.place_market_buy("OGNUSDT", 176.0)
        assert r is not None
        # executedQty stays GROSS — same as the real exchange response
        assert float(r["executedQty"]) == pytest.approx(176.0)
        f = r["fills"][0]
        assert f["commissionAsset"] == "OGN"
        assert float(f["commission"]) == pytest.approx(176.0 * PAPER_FEE_RATE)
        # cash pays the notional ONLY (fee no longer drains USDT)
        assert pt._get_sim_balance() == pytest.approx(
            400.0 - float(r["cummulativeQuoteQty"]))
        # position books the NET qty
        pos = json.loads(pt._get_sim_value("positions", "{}"))
        assert pos["OGN"]["qty"] == pytest.approx(
            176.0 * (1 - PAPER_FEE_RATE))

    def test_sell_fee_stays_in_quote(self, pt):
        from src.paper_trader import PAPER_FEE_RATE
        pt.place_market_buy("OGNUSDT", 176.0)
        r = pt.place_market_sell("OGNUSDT", 176.0 * (1 - PAPER_FEE_RATE))
        assert r is not None
        assert r["fills"][0]["commissionAsset"] == "USDT"

    def test_sell_over_net_rejected(self, pt):
        pt.place_market_buy("OGNUSDT", 176.0)
        assert pt.place_market_sell("OGNUSDT", 176.0) is None


# ── G. Bug-②: validator SWITCH fix ────────────────────────────────────

class TestValidatorSwitchFix:
    @pytest.fixture(autouse=True)
    def _v(self):
        from scripts import report_validator as rv
        self.v = rv
        return rv

    def test_incident_body_ogn(self):
        body = ("🔴 SWITCH OGNUSDT @ $0.0340 x176.0\n"
                "原因: ATOMUSDT -> OGNUSDT: score gap=12 > 10.0 (65→77)\n"
                "策略: switch")
        assert self.v._balance_claim_value(body) is None

    def test_five_historical_switch_bodies(self):
        """The 5 real intercepted bodies (09/26→10/11) all pass now."""
        bodies = [
            "🔴 SWITCH WLDUSDT @ $0.4702 x12.7\n原因: INJUSDT -> WLDUSDT",
            "🔴 SWITCH PENGUUSDT @ $0.0102 x614.0\n原因: 0GUSDT -> PENGUUSDT",
            "🔴 SWITCH NEARUSDT @ $5.5040 x1.0\n原因: ICPUSDT -> NEARUSDT",
            "🔴 SWITCH WLDUSDT @ $0.5616 x10.9\n原因: ZROUSDT -> WLDUSDT",
            "🔴 SWITCH OGNUSDT @ $0.0340 x176.0\n原因: ATOMUSDT -> OGNUSDT",
        ]
        for b in bodies:
            assert self.v._balance_claim_value(b) is None, b

    def test_future_verb_only_hits_layer2(self):
        """A verb NOT in the L1 list still can't false-claim: the USDT
        anchor is a standalone token now, so 'XYZUSDT @ $p' never
        anchors (layer-2 defense against the next new signal verb)."""
        assert self.v._balance_claim_value(
            "🌀 ROTATE NEARUSDT @ $5.50 x1.0") is None

    def test_true_balance_claims_still_caught(self):
        assert self.v._balance_claim_value("当前余额 $393.5") == 393.5
        assert self.v._balance_claim_value("USDT 余额: $400.05") == 400.05
        assert self.v._balance_claim_value("可用USDT: $400.05") == 400.05
        assert self.v._balance_claim_value(
            "BUY NEARUSDT @ $2.133\n当前余额 $393.5") == 393.5
