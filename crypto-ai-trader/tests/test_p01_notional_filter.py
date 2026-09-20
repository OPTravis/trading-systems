"""P0-1: get_symbol_filters must parse the new NOTIONAL filter type
(SUIUSDT-style, minNotional 5 + applyMinToMarket=true) alongside the
legacy MIN_NOTIONAL — the 6979198 fallback-blindspot root cause."""

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src import _binance_sdk_client as sdk  # noqa: E402


def _mk_client(monkeypatch, symbols_filters):
    """Build a BinanceClient whose _get_exchange_info returns canned filters."""
    cli = object.__new__(sdk.BinanceClient)
    exinfo = {
        "symbols": [
            {"symbol": sym, "filters": flts}
            for sym, flts in symbols_filters.items()
        ]
    }
    monkeypatch.setattr(cli, "_get_exchange_info", lambda: exinfo)
    return cli


def test_notional_filter_parsed_with_apply_min_to_market(monkeypatch):
    flts = [
        {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "100000",
         "stepSize": "1"},
        {"filterType": "NOTIONAL", "minNotional": "5.0",
         "applyMinToMarket": True},
    ]
    cli = _mk_client(monkeypatch, {"SUIUSDT": flts})
    out = cli.get_symbol_filters("SUIUSDT")
    assert out["minNotional"] == 5.0
    assert out["applyMinToMarket"] is True


def test_legacy_min_notional_still_parsed(monkeypatch):
    flts = [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000",
         "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "10.0"},
    ]
    cli = _mk_client(monkeypatch, {"BTCUSDT": flts})
    out = cli.get_symbol_filters("BTCUSDT")
    assert out["minNotional"] == 10.0
    assert out["applyMinToMarket"] is False  # legacy never gates market orders


def test_symbol_without_notional_filter(monkeypatch):
    flts = [
        {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "9",
         "stepSize": "1"},
    ]
    cli = _mk_client(monkeypatch, {"XXXUSDT": flts})
    out = cli.get_symbol_filters("XXXUSDT")
    assert "minNotional" not in out
    assert "applyMinToMarket" not in out
