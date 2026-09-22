"""WO-0922-017-vii: circuit_tiers equity snapshot race.

Incident (9/22 21:51:55): right after the TRUMP buy ($36.54), the DB cash
table was already debited while the positions table did not yet carry the
row — the snapshot-assembled equity dropped by the full buy notional
(424.86 -> ~388, phantom dd -8.22%) and tier-1 TRIPped, blocking entries
for 4h until a manual override. Equity is now computed from EXCHANGE
balances (atomically consistent at read time); the snapshot path remains
as fallback. These tests pin the race contract.
"""

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import circuit_tiers as ct  # noqa: E402


class FakeDB:
    def __init__(self, kv=None, cash=0.0):
        self.kv = dict(kv or {})
        self._cash = cash

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value

    def portfolio_get_cash_balance(self):
        return self._cash


class FakePortfolio:
    def __init__(self, positions, db):
        self.positions = positions
        self._db = db

    def get_all_positions(self):
        return self.positions


class BalancesClient:
    """Client exposing get_account() (balances path) + tickers."""

    def __init__(self, balances, prices, fail_symbols=()):
        self._balances = balances  # [(asset, free, locked)]
        self.prices = prices
        self.fail_symbols = set(fail_symbols)

    def get_account(self):
        return {"balances": [
            {"asset": a, "free": str(f), "locked": str(l)}
            for a, f, l in self._balances
        ]}

    def get_ticker_price(self, symbol):
        if symbol in self.fail_symbols:
            raise RuntimeError("ticker down")
        return self.prices.get(symbol)


def _anchored_db(day_start_equity):
    return FakeDB(kv={
        ct.KV_STATE: {
            "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "day_start_equity": day_start_equity,
            "tier": 0,
        },
    })


def _no_alerts(monkeypatch):
    emitted = []
    monkeypatch.setattr("src.live_alerts.emit",
                        lambda et, sym=None, d=None: emitted.append(
                            {"event_type": et}) or True)
    return emitted


# 9/22 live-fire numbers: anchor 424.86, USDT after the buy 388.32,
# TRUMP notional 36.54 — snapshot assembly saw only 388.32 (-8.6%).
_TRUMP_PRICE = 2.8435
_TRUMP_QTY = 12.85          # * 2.8435 = 36.54
_ANCHOR = 424.86
_CASH_AFTER_BUY = 388.32


class TestEquityRace:
    def test_buy_then_immediate_eval_no_phantom_trip(self, monkeypatch):
        """Acceptance: evaluating within 5s of a buy must NOT trip. The
        position is live on the exchange but absent from the (still
        unsynced) DB positions table; cash is already debited."""
        _no_alerts(monkeypatch)
        db = _anchored_db(_ANCHOR)
        # DB snapshot: cash debited, TRUMP row MISSING (the race window)
        portfolio = FakePortfolio([], db)
        db._cash = _CASH_AFTER_BUY
        client = BalancesClient(
            balances=[("USDT", _CASH_AFTER_BUY, 0.0),
                      ("TRUMP", _TRUMP_QTY, 0.0)],
            prices={"TRUMPUSDT": _TRUMP_PRICE})
        res = ct.evaluate_and_act(client, portfolio)
        assert res["tier"] == 0, res  # dd ~0 — no phantom trip
        assert ct.entry_blocked(db) is None

    def test_real_drawdown_still_trips(self, monkeypatch):
        """Acceptance: a genuine -8% equity drop must still TRIP T1."""
        _no_alerts(monkeypatch)
        db = _anchored_db(_ANCHOR)
        portfolio = FakePortfolio([], db)
        client = BalancesClient(  # equity 390 = -8.2% vs anchor
            balances=[("USDT", 390.0, 0.0)], prices={})
        res = ct.evaluate_and_act(client, portfolio)
        assert res["tier"] == 1 and res["action"] == "STOP_NEW"
        assert ct.entry_blocked(db) is not None

    def test_dust_without_market_excluded_not_fails_round(self, monkeypatch):
        """Off-strategy assets without a market (airdrop/retired dust)
        are excluded from equity instead of blinding the round."""
        _no_alerts(monkeypatch)
        db = _anchored_db(_ANCHOR)
        portfolio = FakePortfolio([], db)
        client = BalancesClient(
            balances=[("USDT", _ANCHOR, 0.0), ("NULLDUST", 99999.0, 0.0)],
            prices={})  # no ticker for NULLDUSTUSDT
        res = ct.evaluate_and_act(client, portfolio)
        assert res["tier"] == 0  # round evaluated, dust ignored

    def test_held_symbol_missing_mark_skips_round(self, monkeypatch):
        """A symbol the DB still lists as held MUST be markable; missing
        mark -> fail-open SKIP (never fabricate a drawdown)."""
        _no_alerts(monkeypatch)
        db = _anchored_db(_ANCHOR)
        portfolio = FakePortfolio(
            [{"symbol": "BTCUSDT", "quantity": 0.005, "price_is_stale": True}],
            db)
        client = BalancesClient(
            balances=[("USDT", 300.0, 0.0), ("BTC", 0.005, 0.0)],
            prices={}, fail_symbols=("BTCUSDT",))
        res = ct.evaluate_and_act(client, portfolio)
        assert res["action"] == "SKIP_NO_MARKS"
        assert ct.entry_blocked(db) is None  # no phantom trip either

    def test_client_without_get_account_falls_back(self, monkeypatch):
        """Legacy clients (no get_account) keep the snapshot behavior —
        the p02 suite pins its semantics; this pins the dispatch."""
        _no_alerts(monkeypatch)
        db = _anchored_db(100.0)

        class LegacyClient:
            def __init__(self):
                self.prices = {"BTCUSDT": 0.94}

            def get_ticker_price(self, symbol):
                return self.prices.get(symbol)

        portfolio = FakePortfolio(
            [{"symbol": "BTCUSDT", "quantity": 100,
              "price_is_stale": True}], db)
        db._cash = 6.0  # 100*0.94 + 6 = 100 -> dd 0
        res = ct.evaluate_and_act(LegacyClient(), portfolio)
        assert res["tier"] == 0

    def test_empty_balances_falls_back_not_zero_equity(self, monkeypatch):
        """An empty balances payload must fall back, NOT compute equity=0
        (which would instantly phantom-trip every tier)."""
        _no_alerts(monkeypatch)
        db = _anchored_db(_ANCHOR)
        portfolio = FakePortfolio(
            [{"symbol": "BTCUSDT", "quantity": 0.1,
              "price_is_stale": True}], db)
        db._cash = 415.0  # snapshot path: 0.1*98 + 415 ~= anchor
        client = BalancesClient(
            balances=[], prices={"BTCUSDT": 98.0})
        res = ct.evaluate_and_act(client, portfolio)
        assert res["tier"] == 0, res
