"""WO-1046c S4: core-majors correlation-gate exemption (kv whitelist).

9/21 P0 replay: BTC entry blocked by alt-holding pairwise correlations at
0.7+ — the gate exists to stop same-beta alt stacking, not to block
structurally diversifying core majors. S4 waives the pairwise (and
portfolio-avg) gate for the whitelist (default {BTC, ETH}), still
computing and returning correlation data as the info layer.
"""

import pytest

from src.correlation_risk import CorrelationRiskManager


@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    yield d
    d.kv_set("corr_core_exempt_symbols", None)


@pytest.fixture
def m(monkeypatch):
    """Manager with deterministic correlation matrix + threshold 0.70."""
    mgr = CorrelationRiskManager.__new__(CorrelationRiskManager)
    monkeypatch.setattr(
        CorrelationRiskManager, "_build_correlation_matrix",
        lambda self, syms: (
            {a: {b: 0.85 for b in syms if b != a} for a in syms},
            {},
        ),
    )
    monkeypatch.setattr(
        CorrelationRiskManager, "_pairwise_threshold",
        lambda self: 0.70,
    )
    return mgr


# ── 9/21 P0 replay: whitelist entry passes the gate ─────────────────────

def test_p0_replay_btc_entry_allowed_despite_alt_correlation(m):
    """BTC vs SOL corr 0.85 > 0.70 limit — pre-1046c blocked, S4 allows."""
    r = m.check_new_position("BTC", ["SOL"])
    assert r["allowed"] is True
    assert r["core_exempt"] is True
    assert "core-exempt" in r["reason"]
    assert r["correlations"] == {"SOL": 0.85}  # info layer still returned
    assert r["size_multiplier"] == 1.0


def test_eth_also_exempt(m):
    r = m.check_new_position("ETH", ["SOL", "AVAX"])
    assert r["allowed"] is True and r["core_exempt"] is True


def test_alt_still_blocked_by_gate(m):
    """Non-whitelist symbol keeps the full gate (WO-016 anchor intact)."""
    r = m.check_new_position("SOL", ["BTC"])
    assert r["allowed"] is False
    assert "High correlation" in r["reason"]


def test_lowercase_symbol_normalised(m):
    r = m.check_new_position("btc", ["SOL"])
    assert r["allowed"] is True and r["core_exempt"] is True


def test_no_positions_unchanged(m):
    r = m.check_new_position("BTC", [])
    assert r["allowed"] is True and r["reason"] == "No existing positions"


# ── DCA path ─────────────────────────────────────────────────────────────

def test_dca_add_of_btc_with_high_corr_others_allowed(m):
    """BTC held + SOL held, BTC-SOL corr 0.85: DCA add previously blocked
    by the other-holdings pairwise check, now exempt."""
    r = m.check_new_position("BTC", ["BTC", "SOL"])
    assert r["allowed"] is True
    assert r.get("core_exempt") is True
    assert r["size_multiplier"] == 0.8  # DCA safety reduction preserved


def test_dca_add_of_alt_still_blocked(m):
    r = m.check_new_position("SOL", ["SOL", "AVAX"])
    assert r["allowed"] is False


# ── fail-open path unaffected ────────────────────────────────────────────

def test_insufficient_history_fail_open_unchanged(m, monkeypatch):
    """Whitelist symbol with no correlation data still fail-opens ×0.5 —
    the size caution for unknown data is a separate concern from the
    stacking gate."""
    monkeypatch.setattr(
        CorrelationRiskManager, "_build_correlation_matrix",
        lambda self, syms: ({}, {}),
    )
    r = m.check_new_position("BTC", ["SOL"])
    assert r["allowed"] is True
    assert "fail-open" in r["reason"]
    assert r["size_multiplier"] == 0.5


# ── three-layer config: defaults <- yaml <- kv ───────────────────────────

def test_kv_narrows_whitelist_to_btc_only(m, db):
    db.kv_set("corr_core_exempt_symbols", ["BTC"])
    r = m.check_new_position("ETH", ["SOL"])
    assert r["allowed"] is False  # ETH no longer whitelisted
    r2 = m.check_new_position("BTC", ["SOL"])
    assert r2["allowed"] is True


def test_kv_empty_list_rolls_back_to_pre_1046c(m, db):
    db.kv_set("corr_core_exempt_symbols", [])
    r = m.check_new_position("BTC", ["SOL"])
    assert r["allowed"] is False  # pre-1046c behavior restored


def test_kv_accepts_json_string_form(m, db):
    db.kv_set("corr_core_exempt_symbols", '["BTC", "ETH"]')
    r = m.check_new_position("BTC", ["SOL"])
    assert r["allowed"] is True


def test_yaml_layer_overrides_defaults(m, db, monkeypatch):
    import src.risk_config as rc
    monkeypatch.setattr(
        rc, "get_section",
        lambda name: ({"core_exempt_symbols": ["BTC"]}
                      if name == "correlation_risk" else None),
    )
    # kv not set -> yaml wins over defaults
    r = m.check_new_position("ETH", ["SOL"])
    assert r["allowed"] is False
    r2 = m.check_new_position("BTC", ["SOL"])
    assert r2["allowed"] is True


def test_live_yaml_defaults_contain_btc_eth(m, db):
    """Live yaml section actually parsed (no kv override) -> BTC+ETH pass."""
    r = m.check_new_position("BTC", ["SOL"])
    assert r["allowed"] is True and r["core_exempt"] is True
    r2 = m.check_new_position("ETH", ["SOL"])
    assert r2["allowed"] is True and r2["core_exempt"] is True
