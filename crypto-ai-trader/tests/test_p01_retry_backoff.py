"""P0-1: generic backoff wrapper + rate-event accounting (設計 v1.1 §五)."""

import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import requests  # noqa: E402

from src import _binance_sdk_client as sdk  # noqa: E402


class FakeClientError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"http {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sdk.time, "sleep", lambda s: None)
    # ClientError is referenced inside _retry_call's except clause — swap in
    # the fake so tests don't need the real SDK error class layout
    monkeypatch.setattr(sdk, "ClientError", FakeClientError)
    # isolate module-level counters so full-suite runs don't cross-pollinate
    monkeypatch.setattr(
        sdk, "RATE_STATS", {"429": 0, "418": 0, "5xx": 0, "network": 0},
        raising=False,
    )


def _err(code, retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after else {}
    return FakeClientError(code, headers)


def test_429_retries_then_succeeds_and_counts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _err(429, retry_after=1)
        return "ok"

    assert sdk._retry_call(flaky, label="t") == "ok"
    assert calls["n"] == 3
    assert sdk.RATE_STATS["429"] == 2


def test_429_gives_up_after_attempts_and_raises():
    def always():
        raise _err(429, retry_after=1)

    with pytest.raises(FakeClientError):
        sdk._retry_call(always, label="t", attempts=3)
    assert sdk.RATE_STATS["429"] >= 3


def test_5xx_backoff_then_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _err(503)
        return 42

    assert sdk._retry_call(flaky, label="t") == 42
    assert sdk.RATE_STATS["5xx"] == 1


def test_4xx_non_rate_error_propagates_immediately():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise _err(-1013)   # filter rejection — NOT retryable

    with pytest.raises(FakeClientError):
        sdk._retry_call(bad, label="t", attempts=3)
    assert calls["n"] == 1   # no retry consumed


def test_network_error_backoff_then_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("boom")
        return "fine"

    assert sdk._retry_call(flaky, label="t") == "fine"
    assert sdk.RATE_STATS["network"] == 1


def test_success_no_retry_no_count():
    assert sdk._retry_call(lambda: 7, label="t") == 7
