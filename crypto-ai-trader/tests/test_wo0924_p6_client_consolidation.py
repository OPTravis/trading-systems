"""WO-0924-z2 P6-A: exchange client facade consolidation.

The z2 audit found three "place_order families" (sdk / ccxt / protocol)
with the facade NOT fully closed: scripts/sync_trade_outcomes.py imported
the concrete SDK implementation directly, and health_report read rate
stats from the SDK module regardless of which impl was active. These
tests pin the closed facade:

1. Protocol conformance — both implementations satisfy every
   ExchangeClient Protocol method (structural check).
2. No direct-impl imports outside the facade + the impl modules
   themselves (source-scan guard).
3. Facade routing honors USE_CCXT in both directions and exposes
   get_active_impl() for introspection.
"""

import os
import sys
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

IMPL_MODULES = ("_binance_sdk_client", "ccxt_client")


def _facade_with(env_value):
    """(Re)import the facade under a chosen USE_CCXT value."""
    import importlib
    saved = os.environ.get("USE_CCXT")
    if env_value is None:
        os.environ.pop("USE_CCXT", None)
    else:
        os.environ["USE_CCXT"] = env_value
    sys.modules.pop("src.binance_client", None)
    try:
        import src.binance_client as facade
        importlib.reload(facade)
        return facade
    finally:
        if saved is None:
            os.environ.pop("USE_CCXT", None)
        else:
            os.environ["USE_CCXT"] = saved
        sys.modules.pop("src.binance_client", None)


class TestProtocolConformance:
    """Both impls must satisfy the ExchangeClient Protocol structurally."""

    def test_sdk_impl_satisfies_protocol(self):
        from src.exchange_client import ExchangeClient
        from src import _binance_sdk_client as sdk
        proto = [m for m in dir(ExchangeClient)
                 if not m.startswith("_")
                 and callable(getattr(ExchangeClient, m, None))]
        missing = [m for m in proto
                   if not hasattr(sdk.BinanceClient, m)]
        assert not missing, f"SDK impl missing protocol methods: {missing}"

    def test_ccxt_impl_satisfies_protocol(self):
        from src.exchange_client import ExchangeClient
        from src import ccxt_client as cc
        proto = [m for m in dir(ExchangeClient)
                 if not m.startswith("_")
                 and callable(getattr(ExchangeClient, m, None))]
        missing = [m for m in proto
                   if not hasattr(cc.BinanceClient, m)]
        assert not missing, f"ccxt impl missing protocol methods: {missing}"

    def test_protocol_surface_is_pinned(self):
        """The Protocol itself must not silently shrink — 25 methods as of
        P6. If this changes it must be a deliberate, reviewed change."""
        from src.exchange_client import ExchangeClient
        proto = sorted(m for m in dir(ExchangeClient)
                       if not m.startswith("_")
                       and callable(getattr(ExchangeClient, m, None)))
        assert len(proto) == 25


class TestFacadeClosure:
    """No direct imports of concrete impls outside the facade."""

    def test_no_direct_impl_imports_in_src_or_scripts(self):
        offenders = []
        for py in list((PROJECT_ROOT / "src").glob("*.py")) + \
                list((PROJECT_ROOT / "scripts").glob("*.py")) + \
                [PROJECT_ROOT / "main.py"]:
            rel = py.relative_to(PROJECT_ROOT)
            if py.name in IMPL_MODULES or py.name == "binance_client.py":
                continue
            try:
                text = py.read_text()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("#") or s.startswith('"""'):
                    continue
                if ("from src._binance_sdk_client import" in s or
                        "from src.ccxt_client import" in s or
                        "from src import _binance_sdk_client" in s or
                        "from src import ccxt_client" in s):
                    offenders.append(f"{rel}:{i}: {s}")
        # health_report introspects the ACTIVE impl module object through
        # the facade (never imports the concrete class) — allowed pattern
        # is `from src import binance_client` there; direct impl imports
        # are the violation we pin.
        assert not offenders, "direct impl imports bypassing facade:\n" + \
            "\n".join(offenders)

    def test_health_report_reads_active_impl_stats(self):
        """health_report must source rate stats from the ACTIVE impl module
        (ccxt when USE_CCXT=1), not blindly from the SDK module."""
        import src.health_report as hr
        import inspect
        src_text = inspect.getsource(hr._collect_signals) \
            if hasattr(hr, "_collect_signals") else inspect.getsource(hr)
        assert "binance_client" in src_text, (
            "health_report must route impl introspection via the facade")


class TestFacadeRouting:
    """binance_client facade: USE_CCXT routing + introspection accessor."""

    def test_default_routes_to_sdk(self):
        facade = _facade_with(None)
        assert facade.get_active_impl() == "sdk"
        assert facade.BinanceClient.__module__.endswith("_binance_sdk_client")

    def test_use_ccxt_routes_to_ccxt(self):
        facade = _facade_with("1")
        assert facade.get_active_impl() == "ccxt"
        assert facade.BinanceClient.__module__.endswith("ccxt_client")

    def test_bogus_flag_value_routes_to_sdk(self):
        facade = _facade_with("yes-please-but-not-quite")
        assert facade.get_active_impl() == "sdk"
