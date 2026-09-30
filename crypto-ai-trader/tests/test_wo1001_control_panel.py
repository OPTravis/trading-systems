"""WO-1001-1: control panel — auth matrix, confirm flow, kill-switch
override, audit trail, rate limit. Handler tested transport-free."""
import json
import time

import pytest

import src.control_panel as cp
from src.control_panel import handle_command


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setenv("CTRL_RO_TOKEN", "ro-secret-token")
    monkeypatch.setenv("CTRL_RW_TOKEN", "rw-secret-token")
    monkeypatch.setattr(cp, "RO_TOKEN", "ro-secret-token")
    monkeypatch.setattr(cp, "RW_TOKEN", "rw-secret-token")
    cp._rate.clear()
    cp._pending_force.clear()
    yield
    cp._rate.clear()
    cp._pending_force.clear()


@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


ACTOR = "test:rw"


def _rw(cmd, db, confirm=None):
    return handle_command(cmd, role="rw", actor=ACTOR, db=db, confirm=confirm)


# ── auth / permission matrix ────────────────────────────────────────

def test_ro_token_cannot_run_write_commands(db):
    out = handle_command("/pause", role="ro", actor="t:ro", db=db)
    assert not out["ok"] and out["http"] == 403
    out = handle_command("/forceexit ALL", role="ro", actor="t:ro", db=db)
    assert not out["ok"] and out["http"] == 403


def test_ro_token_can_read_status(db):
    out = handle_command("/status", role="ro", actor="t:ro", db=db)
    assert out["ok"] and out["http"] == 200


def test_bad_role_rejected(db):
    out = handle_command("/status", role="anon", actor="x", db=db)
    assert not out["ok"] and out["http"] == 401


def test_unknown_command_rejected(db):
    out = _rw("/rm -rf /", db)
    assert not out["ok"] and out["http"] == 400


# ── pause / resume semantics ────────────────────────────────────────

def test_pause_resume_roundtrip(db):
    out = _rw("/pause", db)
    assert out["ok"] and "exits/trailing unaffected" in out["msg"]
    from src.config_store import cfg_get, invalidate_cache
    invalidate_cache()
    assert str(cfg_get("NEW_POSITIONS_HALTED")) == "1"
    # executor gate actually honours it
    from src.trade_executor import _new_positions_halted
    assert _new_positions_halted() is True
    out2 = _rw("/resume", db)
    assert out2["ok"]
    invalidate_cache()
    assert _new_positions_halted() is False
    from src.config_store import clear_override
    clear_override("NEW_POSITIONS_HALTED")


def test_exit_mode_switch(db):
    for m in ("notify", "off", "auto"):
        out = _rw(f"/exit-mode {m}", db)
        assert out["ok"] and db.kv_get("exit:mode") == m
    assert not _rw("/exit-mode nonsense", db)["ok"]


# ── forceexit: confirm flow + kill-switch override ──────────────────

def _seed_position(db, sym="XUSDT", qty=10.0):
    db._get_conn().execute(
        "INSERT OR REPLACE INTO portfolio (symbol, quantity, entry_price, "
        "strategy, opened_at) VALUES (?,?,?,?,?)",
        (sym, qty, 100.0, "test", time.time() - 7200))
    db._get_conn().commit()


class _StubExitClient:
    def __init__(self):
        self.sold = []
        self.orders = [{"orderId": i, "side": "SELL"} for i in (1, 2)]
    def get_open_orders(self, sym):
        return self.orders
    def cancel_order(self, sym, oid):
        return True
    def get_symbol_filters(self, sym):
        return {"stepSize": "0.0001", "minNotional": "5"}
    def get_free_balance(self, asset):
        return 100.0
    def get_ticker_price(self, sym):
        return 100.0
    def place_market_sell(self, sym, qty):
        self.sold.append((sym, qty))
        return {"orderId": 9001}
    def get_my_trades(self, sym, limit=100):
        return []
    def get_account(self):
        return {"balances": [{"asset": "X", "free": "100", "locked": "0"}]}


def test_forceexit_requires_confirm_then_overrides_off(db, monkeypatch):
    _seed_position(db, "XUSDT", 10.0)
    db.kv_set("exit:mode", "off")  # kill-switch OFF — must not block humans
    stub = _StubExitClient()
    import src.paper_trader as pt
    monkeypatch.setattr(pt, "get_trading_client", lambda: stub)

    # stage 1: no confirm -> echo blast radius
    out1 = _rw("/forceexit XUSDT", db)
    assert out1["ok"] and out1.get("confirm_required")
    assert out1["will_exit"] == [{"symbol": "XUSDT", "qty": 10.0}]

    # wrong confirm text does not fire
    out_bad = _rw("/forceexit XUSDT", db, confirm="YES")
    assert out_bad.get("confirm_required")

    # stage 2: CONFIRM -> executes despite exit-mode=off, audit carries override
    out2 = _rw("/forceexit XUSDT", db, confirm="CONFIRM")
    assert out2["ok"] and out2["override_exit_mode"] is True
    assert stub.sold and stub.sold[0][0] == "XUSDT"
    row = db._get_conn().execute(
        "SELECT payload_json FROM ledger_events WHERE type='CTRL_CMD' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    payload = json.loads(row[0])
    assert payload["override_exit_mode"] is True


def test_forceexit_confirm_expiry(monkeypatch, db):
    _seed_position(db, "XUSDT", 10.0)
    import src.paper_trader as pt
    monkeypatch.setattr(pt, "get_trading_client", lambda: _StubExitClient())
    _rw("/forceexit XUSDT", db)
    # age the pending ticket past the window
    with cp._pending_lock:
        ts, spec = cp._pending_force[ACTOR]
        cp._pending_force[ACTOR] = (ts - 61.0, spec)
    out = _rw("/forceexit XUSDT", db, confirm="CONFIRM")
    assert out.get("confirm_required")  # expired -> back to stage 1


def test_forceexit_bad_symbol_rejected(db):
    out = _rw("/forceexit NOTASYMBOL", db)
    assert not out["ok"] and out["http"] == 400


# ── audit + rate limit ──────────────────────────────────────────────

def test_rw_commands_are_audited(db):
    _rw("/exit-mode notify", db)
    row = db._get_conn().execute(
        "SELECT type, source FROM ledger_events WHERE type='CTRL_CMD' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row and row[1] == "control_panel"


def test_rate_limit_kicks_in(db):
    for _ in range(cp.RATE_LIMIT_PER_MIN):
        assert handle_command("/status", role="ro", actor="rl:x", db=db)["ok"]
    out = handle_command("/status", role="ro", actor="rl:x", db=db)
    assert not out["ok"] and out["http"] == 429


# ── serve() refusal guarantees ──────────────────────────────────────

def test_serve_refuses_without_tokens(monkeypatch):
    monkeypatch.setattr(cp, "RO_TOKEN", "")
    monkeypatch.setattr(cp, "RW_TOKEN", "")
    with pytest.raises(SystemExit):
        cp.serve()


def test_serve_refuses_public_bind_without_allowlist(monkeypatch):
    monkeypatch.setattr(cp, "BIND_PUBLIC", False)
    monkeypatch.setattr(cp, "ALLOWED_IPS", {"127.0.0.1"})
    with pytest.raises(SystemExit):
        cp.serve(host="0.0.0.0", port=18787)


def test_token_fingerprint_no_leak(db):
    _rw("/pause", db)
    row = db._get_conn().execute(
        "SELECT payload_json FROM ledger_events WHERE type='CTRL_CMD' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert "rw-secret-token" not in row[0]  # full token never persisted
