"""
bug#14: BGeometrics cross-process disk cache.

Hourly cron scans each spawn a fresh process; the old mem-only caches meant
~4 API calls/hour (~96/day) against a 15/day quota → daily 429s. The disk
layer (src/bge_cache.py) must:
  1. let a "new process" (cleared mem cache) reuse a fresh disk entry
  2. share one entry between surge_detector and dimension_scorer (/mvrv)
  3. tolerate corrupt/missing cache files without crashing
  4. never let cache I/O failures break the fetch path
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest


def _cache_file(tmp_path):
    return tmp_path / "bge_cache.json"


class TestDiskLayerCore:
    def test_fresh_disk_entry_skips_http(self, monkeypatch, tmp_path):
        """New process (mem empty) + fresh disk entry → no HTTP call."""
        from src import bge_cache
        from src.surge_detector import SurgeDetector, reset_bge_cache

        cf = _cache_file(tmp_path)
        cf.write_text(
            json.dumps({"entries": {"/mvrv": {"value": 1.32, "fetched_at": time.time()}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        reset_bge_cache()

        def _boom(*a, **kw):
            raise AssertionError("HTTP must not be called when disk entry is fresh")

        with patch("src.surge_detector.requests.get", side_effect=_boom):
            assert SurgeDetector()._fetch_bgeometrics("/mvrv") == 1.32

    def test_stale_disk_entry_refetches_and_rewrites(self, monkeypatch, tmp_path):
        from src import bge_cache
        from src.surge_detector import SurgeDetector, reset_bge_cache

        cf = _cache_file(tmp_path)
        stale_ts = time.time() - 10 * 3600  # older than 8h success TTL
        cf.write_text(
            json.dumps({"entries": {"/mvrv": {"value": 1.0, "fetched_at": stale_ts}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        reset_bge_cache()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"d": "2026-08-20", "mvrv": 1.41}]
        with patch("src.surge_detector.requests.get", return_value=resp) as mg:
            assert SurgeDetector()._fetch_bgeometrics("/mvrv") == 1.41
            assert mg.call_count == 1

        disk = bge_cache.load_entry("/mvrv")
        assert disk is not None and disk["value"] == 1.41
        assert disk["fetched_at"] > stale_ts

    def test_corrupt_cache_file_tolerated(self, monkeypatch, tmp_path):
        """Garbage file → treated as miss; after fetch the file is valid JSON."""
        from src import bge_cache
        from src.surge_detector import SurgeDetector, reset_bge_cache

        _cache_file(tmp_path).write_text("NOT JSON {{{", encoding="utf-8")
        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        reset_bge_cache()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"d": "2026-08-20", "mvrv": 1.33}]
        with patch("src.surge_detector.requests.get", return_value=resp):
            assert SurgeDetector()._fetch_bgeometrics("/mvrv") == 1.33

        data = json.loads(_cache_file(tmp_path).read_text(encoding="utf-8"))
        assert data["entries"]["/mvrv"]["value"] == 1.33

    def test_missing_file_is_silent_miss(self, tmp_path):
        from src import bge_cache

        assert bge_cache.load_entry("/mvrv") is None  # no file, no exception

    def test_unwritable_path_degrades_silently(self, monkeypatch, tmp_path):
        """Cache write failure must never raise into the fetch path."""
        from src import bge_cache

        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file", encoding="utf-8")
        # cache path INSIDE a regular file → mkdir/write fails
        monkeypatch.setenv("BGE_CACHE_PATH", str(blocker / "nested" / "c.json"))
        bge_cache.save_entry("/mvrv", 1.2)  # must not raise
        assert bge_cache.load_entry("/mvrv") is None


class TestCrossModuleSharing:
    def test_surge_write_feeds_dimension_scorer(self, monkeypatch, tmp_path):
        """Same scan or next hourly process: surge caches /mvrv on disk →
        dimension_scorer._fetch_mvrv serves from disk with zero HTTP."""
        import src.dimension_scorer as ds_mod
        from src.surge_detector import SurgeDetector, reset_bge_cache

        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        reset_bge_cache()
        ds_mod._reset_mvrv_cache()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"d": "2026-08-20", "mvrv": 1.28}]
        with patch("src.surge_detector.requests.get", return_value=resp):
            assert SurgeDetector()._fetch_bgeometrics("/mvrv") == 1.28

        # simulate a fresh process: mem caches gone, disk survives
        reset_bge_cache()
        ds_mod._reset_mvrv_cache()

        ds = ds_mod.DimensionScorer.__new__(ds_mod.DimensionScorer)

        def _boom(*a, **kw):
            raise AssertionError("dimension must serve from disk, not HTTP")

        with patch.object(ds_mod.requests, "get", side_effect=_boom):
            assert ds._fetch_mvrv() == 1.28

    def test_dimension_failure_cached_on_disk_surge_honours_fail_ttl(self, monkeypatch, tmp_path):
        """dimension's 429 failure persists to disk → surge skips HTTP while
        within its 2h fail TTL (no quota re-burn from the sibling module)."""
        import src.dimension_scorer as ds_mod
        from src.surge_detector import SurgeDetector, reset_bge_cache

        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        reset_bge_cache()
        ds_mod._reset_mvrv_cache()

        ds = ds_mod.DimensionScorer.__new__(ds_mod.DimensionScorer)
        bad = MagicMock()
        bad.raise_for_status.side_effect = Exception("429 Client Error")
        with patch.object(ds_mod.requests, "get", return_value=bad):
            assert ds._fetch_mvrv() is None

        # fresh process for surge
        reset_bge_cache()

        def _boom(*a, **kw):
            raise AssertionError("surge must honour disk fail entry, not re-burn quota")

        with patch("src.surge_detector.requests.get", side_effect=_boom):
            assert SurgeDetector()._fetch_bgeometrics("/mvrv") is None


class TestQuotaMath:
    def test_hourly_processes_daily_http_budget(self, monkeypatch, tmp_path):
        """24 fresh processes (hourly scans) × 3 endpoints must issue at most
        3 HTTP calls/day with 8h success TTL — the bug#14 regression guard."""
        from src.surge_detector import SurgeDetector, reset_bge_cache

        monkeypatch.setenv("BGEOMETRICS_API_KEY", "k")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"d": "2026-08-20", "mvrv": 1.32, "sopr": 1.001, "nupl": 0.24}]

        with patch("src.surge_detector.requests.get", return_value=resp) as mg:
            for _hour in range(24):  # 24 hourly processes
                reset_bge_cache()  # new process = mem cache gone
                sd = SurgeDetector()
                sd._fetch_bgeometrics("/mvrv")
                sd._fetch_bgeometrics("/sopr")
                sd._fetch_bgeometrics("/nupl")

        assert mg.call_count == 3, f"expected 3 HTTP calls/day, got {mg.call_count}"
