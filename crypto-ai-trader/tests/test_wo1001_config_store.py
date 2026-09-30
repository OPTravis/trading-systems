"""WO-1001-4: config SSOT layered resolver — kv > env > yaml > default."""
import time

import pytest

from src.config_store import (
    DEFAULTS, clear_override, cfg_get, cfg_get_bool, describe,
    invalidate_cache, set_override,
)


@pytest.fixture(autouse=True)
def _fresh():
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def db():
    from src.state_db import get_state_db
    d = get_state_db()
    d.kv_set("ledger:shadow:bootstrap_ts", time.time() - 86400)
    yield d


def test_layer_precedence_kv_beats_env_beats_yaml(monkeypatch, db):
    # yaml layer (repo default) says false; env says true; kv says false again
    monkeypatch.setenv("AUTO_EXECUTE", "true")
    set_override("AUTO_EXECUTE", "false")
    invalidate_cache()
    assert cfg_get("AUTO_EXECUTE") == "false"       # kv wins
    clear_override("AUTO_EXECUTE")
    invalidate_cache()
    assert cfg_get("AUTO_EXECUTE") == "true"        # env wins over yaml


def test_env_absent_falls_to_yaml_layer(monkeypatch):
    monkeypatch.delenv("NEW_POSITIONS_HALTED", raising=False)
    # repo yaml ships "1"
    assert cfg_get("NEW_POSITIONS_HALTED") == "1"


def test_default_layer_and_fallback(monkeypatch):
    monkeypatch.delenv("SOME_UNKNOWN_KEY", raising=False)
    assert cfg_get("SOME_UNKNOWN_KEY", "fb") == "fb"
    assert DEFAULTS["TRADING_MODE"] == "spot"


def test_cfg_get_bool_variants(monkeypatch, db):
    monkeypatch.setenv("X_FLAG_A", "1")
    assert cfg_get_bool("X_FLAG_A") is True
    monkeypatch.setenv("X_FLAG_A", "off")
    assert cfg_get_bool("X_FLAG_A") is False
    set_override("X_FLAG_A", True)                  # bool passes through kv
    invalidate_cache()
    assert cfg_get_bool("X_FLAG_A") is True
    clear_override("X_FLAG_A")


def test_describe_reports_source_per_layer(monkeypatch, db):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    d = describe("TRADING_MODE")
    assert d["source"] == "yaml" and d["value"] == "spot"
    set_override("TRADING_MODE", "spot-test")
    invalidate_cache()
    d2 = describe("TRADING_MODE")
    assert d2["source"] == "kv" and d2["value"] == "spot-test"
    clear_override("TRADING_MODE")


def test_clear_override_falls_back_and_reports(monkeypatch, db):
    set_override("USE_TESTNET", "true")
    invalidate_cache()
    assert clear_override("USE_TESTNET") is True
    assert clear_override("USE_TESTNET") is False   # nothing left to clear


def test_migrated_call_sites_use_store(monkeypatch, db):
    # behaviour parity: NEW_POSITIONS_HALTED honoured via store layers
    from src.trade_executor import _new_positions_halted
    monkeypatch.setenv("NEW_POSITIONS_HALTED", "0")
    assert _new_positions_halted() is False
    monkeypatch.setenv("NEW_POSITIONS_HALTED", "1")
    assert _new_positions_halted() is True
    set_override("NEW_POSITIONS_HALTED", "0")       # kv beats env even here
    invalidate_cache()
    assert _new_positions_halted() is False
    clear_override("NEW_POSITIONS_HALTED")


def test_kv_db_failure_degrades_to_env_yaml(monkeypatch):
    """Real failure path: StateDB unavailable -> kv layer returns {} inside
    its own try/except; cfg_get must still resolve env/yaml."""
    import src.state_db as sdb
    import src.config_store as cs
    monkeypatch.setattr(sdb, "get_state_db",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(cs, "_KV_TTL", 0.0)          # force cache refresh
    monkeypatch.setenv("TRADING_MODE", "spot-env")
    invalidate_cache()
    assert cfg_get("TRADING_MODE") == "spot-env"     # env layer served it


def test_yaml_missing_file_keeps_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SETTINGS_YAML", str(tmp_path / "nonexistent.yaml"))
    import src.config_store as cs
    invalidate_cache()
    monkeypatch.setattr(cs, "_load_kv_layer", lambda: {})
    monkeypatch.delenv("TRADING_MODE", raising=False)
    assert cs.cfg_get("TRADING_MODE") == "spot"     # code default layer
